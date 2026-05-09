import base64
import json
import os
import re
import tempfile

import anthropic
import psycopg2
import psycopg2.extras
from dotenv import load_dotenv
from flask import Flask, flash, redirect, render_template, request, url_for

load_dotenv()

SCHEMA_PATH = os.path.join(os.path.dirname(__file__), "schema.sql")

EXTRACTION_PROMPT = """This is a photo of a chalkboard tap list at a brewery. The board may be rotated or hard to read.

Extract every beer listed. Return ONLY a JSON array (no markdown, no explanation) where each item has:
- tap_number: integer or null
- name: string (beer name)
- brewery: string or null
- brewery_url: string or null (the brewery's official website URL, based on your knowledge — e.g. "https://www.sierranevada.com". Use null if you are not confident.)
- style: string or null (e.g. IPA, Stout, Lager)
- abv: float or null (e.g. 6.3)
- notes: string or null (any extra info like origin, awards, description)
- prices: array of {"size_oz": integer or null, "price": float} objects

If a field is unreadable, use null. Include every beer you can identify."""

MEDIA_TYPES = {
    "jpg": "image/jpeg",
    "jpeg": "image/jpeg",
    "png": "image/png",
    "webp": "image/webp",
    "gif": "image/gif",
    "heic": "image/jpeg",
}


def load_image_base64(path: str) -> tuple[str, str]:
    ext = path.rsplit(".", 1)[-1].lower()
    media_type = MEDIA_TYPES.get(ext, "image/jpeg")
    with open(path, "rb") as f:
        return base64.standard_b64encode(f.read()).decode("utf-8"), media_type


def extract_beers_from_image(image_b64: str, media_type: str = "image/jpeg") -> list[dict]:
    client = anthropic.Anthropic(api_key=os.environ["ANTHROPIC_API_KEY"])
    message = client.messages.create(
        model="claude-sonnet-4-6",
        max_tokens=2048,
        messages=[
            {
                "role": "user",
                "content": [
                    {
                        "type": "image",
                        "source": {
                            "type": "base64",
                            "media_type": media_type,
                            "data": image_b64,
                        },
                    },
                    {"type": "text", "text": EXTRACTION_PROMPT},
                ],
            }
        ],
    )
    raw = message.content[0].text.strip()
    raw = re.sub(r"^```(?:json)?\s*", "", raw)
    raw = re.sub(r"\s*```$", "", raw)
    return json.loads(raw)


def backfill_brewery_urls(conn):
    with conn.cursor() as cur:
        cur.execute("""
            SELECT DISTINCT brewery FROM beers
            WHERE brewery IS NOT NULL AND brewery_url IS NULL
        """)
        breweries = [row[0] for row in cur.fetchall()]

    if not breweries:
        return

    with conn.cursor() as cur:
        cur.execute("""
            SELECT DISTINCT ON (brewery) brewery, notes
            FROM beers
            WHERE brewery = ANY(%s)
        """, (breweries,))
        brewery_notes = {row[0]: row[1] for row in cur.fetchall()}

    client = anthropic.Anthropic(api_key=os.environ["ANTHROPIC_API_KEY"])
    brewery_lines = "\n".join(
        f"- {b}" + (f" ({brewery_notes[b]})" if brewery_notes.get(b) else "")
        for b in breweries
    )
    prompt = (
        "For each brewery below, provide its official website URL. "
        "Use your knowledge of real breweries, and also infer likely URLs from the name "
        "(e.g. 'Hugger Mugger Brewing' → 'https://www.huggermuggerbrewing.com', "
        "'Sierra Nevada' → 'https://www.sierranevada.com'). "
        "Make a best guess rather than returning null — breweries almost always use "
        "some form of their name as a .com domain. Only return null if the name is "
        "genuinely too ambiguous to guess.\n"
        "Return ONLY a JSON object mapping brewery name to URL string.\n\n"
        "Breweries:\n" + brewery_lines
    )
    message = client.messages.create(
        model="claude-sonnet-4-6",
        max_tokens=1024,
        messages=[{"role": "user", "content": prompt}],
    )
    raw = message.content[0].text
    match = re.search(r"\{.*\}", raw, re.DOTALL)
    if not match:
        return
    url_map = json.loads(match.group())
    with conn.cursor() as cur:
        for brewery, url in url_map.items():
            if url:
                cur.execute(
                    "UPDATE beers SET brewery_url = %s WHERE brewery = %s AND brewery_url IS NULL",
                    (url, brewery),
                )
    conn.commit()


def ensure_schema(conn):
    with open(SCHEMA_PATH) as f:
        sql = f.read()
    with conn.cursor() as cur:
        cur.execute(sql)
    conn.commit()


def insert_beers(conn, beers: list[dict], brewpub: str | None = None) -> int:
    inserted = 0
    with conn.cursor() as cur:
        cur.execute(
            "INSERT INTO uploads (brewpub) VALUES (%s) RETURNING id, uploaded_at",
            (brewpub,),
        )
        row = cur.fetchone()
        upload_id, uploaded_at = row[0], row[1]
        for beer in beers:
            cur.execute(
                """
                INSERT INTO beers (upload_id, tap_number, name, brewery, brewery_url, brewpub, style, abv, notes, created_at)
                VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
                RETURNING id
                """,
                (
                    upload_id,
                    beer.get("tap_number"),
                    beer.get("name"),
                    beer.get("brewery"),
                    beer.get("brewery_url"),
                    brewpub,
                    beer.get("style"),
                    beer.get("abv"),
                    beer.get("notes"),
                    uploaded_at,
                ),
            )
            beer_id = cur.fetchone()[0]
            for price in beer.get("prices") or []:
                cur.execute(
                    "INSERT INTO beer_prices (beer_id, size_oz, price) VALUES (%s, %s, %s)",
                    (beer_id, price.get("size_oz"), price.get("price")),
                )
            inserted += 1
    conn.commit()
    return inserted


app = Flask(__name__)
app.secret_key = os.environ.get("SECRET_KEY", "beerview-dev-secret")

_boot_conn = psycopg2.connect(os.environ.get("DATABASE_URL", "postgresql://localhost/beerview"))
ensure_schema(_boot_conn)
backfill_brewery_urls(_boot_conn)
_boot_conn.close()

ALLOWED_EXTENSIONS = {"jpg", "jpeg", "png", "webp", "heic"}


def allowed_file(filename):
    return "." in filename and filename.rsplit(".", 1)[1].lower() in ALLOWED_EXTENSIONS


def get_conn():
    return psycopg2.connect(os.environ.get("DATABASE_URL", "postgresql://localhost/beerview"))


def get_brewpubs():
    """Return each known brewpub with its most recent upload timestamp."""
    conn = get_conn()
    with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
        cur.execute("""
            SELECT DISTINCT ON (brewpub)
                brewpub, uploaded_at
            FROM uploads
            WHERE brewpub IS NOT NULL
            ORDER BY brewpub, uploaded_at DESC
        """)
        result = [dict(r) for r in cur.fetchall()]
    conn.close()
    return result


def get_beers(brewpub=None):
    """Return current beers for a brewpub (most recent upload), or all current beers."""
    conn = get_conn()
    with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
        if brewpub:
            cur.execute("""
                SELECT b.*
                FROM beers b
                JOIN uploads u ON b.upload_id = u.id
                WHERE u.brewpub = %s
                  AND u.id = (
                      SELECT id FROM uploads
                      WHERE brewpub = %s
                      ORDER BY uploaded_at DESC
                      LIMIT 1
                  )
                ORDER BY b.tap_number NULLS LAST, b.name
            """, (brewpub, brewpub))
        else:
            cur.execute("""
                SELECT b.*
                FROM beers b
                JOIN uploads u ON b.upload_id = u.id
                WHERE u.id IN (
                    SELECT DISTINCT ON (brewpub) id
                    FROM uploads
                    ORDER BY brewpub, uploaded_at DESC
                )
                ORDER BY u.brewpub NULLS LAST, b.tap_number NULLS LAST, b.name
            """)

        beers = [dict(r) for r in cur.fetchall()]

        if beers:
            ids = tuple(b["id"] for b in beers)
            cur.execute(
                "SELECT * FROM beer_prices WHERE beer_id = ANY(%s) ORDER BY size_oz",
                (list(ids),)
            )
            price_map = {}
            for p in cur.fetchall():
                price_map.setdefault(p["beer_id"], []).append(p)
            for beer in beers:
                beer["prices"] = price_map.get(beer["id"], [])

    conn.close()
    return beers


@app.route("/")
def index():
    selected = request.args.get("brewpub", "").strip() or None
    brewpubs = get_brewpubs()
    beers = get_beers(brewpub=selected)
    return render_template("index.html", beers=beers, brewpubs=brewpubs, selected=selected)


@app.route("/upload", methods=["POST"])
def upload():
    file = request.files.get("image")
    if not file or file.filename == "":
        flash("No file selected.", "error")
        return redirect(url_for("index"))

    if not allowed_file(file.filename):
        flash("Unsupported file type. Use JPG, PNG, WEBP, or HEIC.", "error")
        return redirect(url_for("index"))

    suffix = "." + file.filename.rsplit(".", 1)[1].lower()
    with tempfile.NamedTemporaryFile(suffix=suffix, delete=False) as tmp:
        file.save(tmp.name)
        tmp_path = tmp.name

    brewpub = request.form.get("brewpub", "").strip() or None

    try:
        image_b64, media_type = load_image_base64(tmp_path)
        beers = extract_beers_from_image(image_b64, media_type)

        conn = get_conn()
        ensure_schema(conn)
        count = insert_beers(conn, beers, brewpub=brewpub)
        backfill_brewery_urls(conn)
        conn.close()

        flash(f"Added {count} beer{'s' if count != 1 else ''} from the menu.", "success")
        if brewpub:
            return redirect(url_for("index", brewpub=brewpub))
    except Exception as e:
        flash(f"Extraction failed: {e}", "error")
    finally:
        os.unlink(tmp_path)

    return redirect(url_for("index"))


if __name__ == "__main__":
    app.run(debug=True)
