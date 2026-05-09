CREATE TABLE IF NOT EXISTS uploads (
    id SERIAL PRIMARY KEY,
    brewpub TEXT,
    uploaded_at TIMESTAMPTZ DEFAULT now()
);

CREATE TABLE IF NOT EXISTS beers (
    id SERIAL PRIMARY KEY,
    upload_id INTEGER REFERENCES uploads(id) ON DELETE CASCADE,
    tap_number INTEGER,
    name TEXT NOT NULL,
    brewery TEXT,
    brewery_url TEXT,
    brewpub TEXT,
    style TEXT,
    abv NUMERIC(4,2),
    notes TEXT,
    created_at TIMESTAMPTZ DEFAULT now()
);

CREATE TABLE IF NOT EXISTS beer_prices (
    id SERIAL PRIMARY KEY,
    beer_id INTEGER REFERENCES beers(id) ON DELETE CASCADE,
    size_oz INTEGER,
    price NUMERIC(5,2)
);
