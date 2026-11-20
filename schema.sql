CREATE TABLE IF NOT EXISTS sellers (
    id INTEGER PRIMARY KEY,
    login TEXT NOT NULL UNIQUE COLLATE NOCASE,
    password_hash TEXT NOT NULL,
    role TEXT NOT NULL CHECK (role IN ('admin', 'seller')),
    legal_name TEXT NOT NULL,
    tax_id TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS sites (
    id INTEGER PRIMARY KEY,
    seller_id INTEGER NOT NULL REFERENCES sellers(id),
    slug TEXT NOT NULL UNIQUE,
    name TEXT NOT NULL,
    kind TEXT NOT NULL CHECK (kind IN ('romashka', 'vasilek')),
    active INTEGER NOT NULL DEFAULT 1 CHECK (active IN (0, 1))
);

CREATE TABLE IF NOT EXISTS payments (
    id INTEGER PRIMARY KEY,
    site_id INTEGER NOT NULL REFERENCES sites(id),
    total INTEGER NOT NULL CHECK (total > 0),
    created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
);

CREATE TABLE IF NOT EXISTS bank_transactions (
    id INTEGER PRIMARY KEY,
    payment_id INTEGER NOT NULL UNIQUE REFERENCES payments(id),
    bank TEXT NOT NULL,
    card_mask TEXT NOT NULL,
    result_code TEXT NOT NULL,
    created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
);

CREATE TABLE IF NOT EXISTS refunds (
    id INTEGER PRIMARY KEY,
    payment_id INTEGER NOT NULL REFERENCES payments(id),
    amount INTEGER NOT NULL CHECK (amount > 0),
    bank_code TEXT NOT NULL,
    request_key TEXT NOT NULL UNIQUE,
    created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
);

CREATE TABLE IF NOT EXISTS receipts (
    id INTEGER PRIMARY KEY,
    payment_id INTEGER NOT NULL REFERENCES payments(id),
    refund_id INTEGER UNIQUE REFERENCES refunds(id),
    kind TEXT NOT NULL CHECK (kind IN ('initial', 'settlement', 'refund')),
    total INTEGER NOT NULL CHECK (total > 0),
    cashless INTEGER NOT NULL CHECK (cashless >= 0),
    advance INTEGER NOT NULL CHECK (advance >= 0),
    kkt_status TEXT NOT NULL CHECK (kkt_status IN ('ok', 'error')),
    kkt_code TEXT NOT NULL,
    fiscal_context TEXT NOT NULL,
    created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
    CHECK (total = cashless + advance),
    CHECK ((kind = 'refund' AND refund_id IS NOT NULL) OR
           (kind <> 'refund' AND refund_id IS NULL))
);

CREATE UNIQUE INDEX IF NOT EXISTS one_initial
ON receipts(payment_id) WHERE kind = 'initial';

CREATE UNIQUE INDEX IF NOT EXISTS one_successful_settlement
ON receipts(payment_id) WHERE kind = 'settlement' AND kkt_status = 'ok';

CREATE TABLE IF NOT EXISTS receipt_items (
    id INTEGER PRIMARY KEY,
    receipt_id INTEGER NOT NULL REFERENCES receipts(id),
    source_item_id INTEGER REFERENCES receipt_items(id),
    name TEXT NOT NULL,
    qty INTEGER NOT NULL CHECK (qty > 0),
    price INTEGER NOT NULL CHECK (price > 0),
    vat TEXT NOT NULL,
    unit INTEGER NOT NULL,
    marked INTEGER NOT NULL CHECK (marked IN (0, 1)),
    mark_code TEXT NOT NULL DEFAULT '',
    method INTEGER NOT NULL CHECK (method IN (1, 4)),
    line_total INTEGER NOT NULL,
    vat_amount INTEGER,
    CHECK (line_total = qty * price),
    CHECK (marked = 0 OR qty = 1)
);

CREATE INDEX IF NOT EXISTS receipt_items_source ON receipt_items(source_item_id);
CREATE INDEX IF NOT EXISTS receipts_payment ON receipts(payment_id);
CREATE INDEX IF NOT EXISTS refunds_payment ON refunds(payment_id);

CREATE TABLE IF NOT EXISTS carts (
    browser_key TEXT NOT NULL,
    site_id INTEGER NOT NULL REFERENCES sites(id),
    items_json TEXT NOT NULL,
    PRIMARY KEY (browser_key, site_id)
);

CREATE TABLE IF NOT EXISTS shop.orders (
    payment_id INTEGER PRIMARY KEY,
    method INTEGER NOT NULL CHECK (method IN (1, 4)),
    closed_receipt_id INTEGER,
    created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
);

CREATE TABLE IF NOT EXISTS shop.order_items (
    source_item_id INTEGER PRIMARY KEY,
    payment_id INTEGER NOT NULL REFERENCES orders(payment_id),
    name TEXT NOT NULL,
    qty INTEGER NOT NULL CHECK (qty > 0),
    price INTEGER NOT NULL CHECK (price > 0),
    vat TEXT NOT NULL,
    unit INTEGER NOT NULL,
    marked INTEGER NOT NULL CHECK (marked IN (0, 1)),
    mark_code TEXT NOT NULL DEFAULT '',
    refunded INTEGER NOT NULL DEFAULT 0 CHECK (refunded BETWEEN 0 AND qty)
);