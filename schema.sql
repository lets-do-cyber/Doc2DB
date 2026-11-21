-- =============================================================================
-- Doc2DB-AI · schema.sql
-- SQLite DDL for the document-to-database automation engine.
-- Run via: python app.py --init-db
-- =============================================================================

PRAGMA foreign_keys = ON;
PRAGMA journal_mode = WAL;

-- -----------------------------------------------------------------------------
-- TABLE: vendors
-- Stores unique vendor master records.
-- -----------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS vendors (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    name        TEXT    NOT NULL UNIQUE,
    tax_id      TEXT,                          -- NPWP / Tax ID
    address     TEXT,
    created_at  TEXT    NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%SZ', 'now'))
);

-- -----------------------------------------------------------------------------
-- TABLE: invoices
-- One row per invoice document. Linked to vendors via FK.
-- UNIQUE constraint on (vendor_id, invoice_number) prevents duplicates.
-- -----------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS invoices (
    id                 INTEGER PRIMARY KEY AUTOINCREMENT,
    vendor_id          INTEGER NOT NULL REFERENCES vendors(id) ON DELETE RESTRICT,
    invoice_number     TEXT    NOT NULL,
    invoice_date       TEXT,                   -- ISO-8601: YYYY-MM-DD
    due_date           TEXT,                   -- ISO-8601: YYYY-MM-DD
    currency           TEXT    NOT NULL DEFAULT 'IDR',
    subtotal           REAL    NOT NULL DEFAULT 0,
    tax_amount         REAL    NOT NULL DEFAULT 0,
    total_amount       REAL    NOT NULL DEFAULT 0,
    validation_status  TEXT    NOT NULL DEFAULT 'PENDING'
                           CHECK(validation_status IN ('VERIFIED','NEEDS_REVIEW','PENDING')),
    validation_notes   TEXT,
    source_name        TEXT,                   -- original filename or 'paste'
    created_at         TEXT    NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%SZ', 'now')),
    UNIQUE(vendor_id, invoice_number)
);

-- -----------------------------------------------------------------------------
-- TABLE: invoice_items
-- Line-item detail rows. Cascade-deleted when parent invoice is deleted.
-- -----------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS invoice_items (
    id           INTEGER PRIMARY KEY AUTOINCREMENT,
    invoice_id   INTEGER NOT NULL REFERENCES invoices(id) ON DELETE CASCADE,
    description  TEXT    NOT NULL,
    quantity     REAL    NOT NULL DEFAULT 1,
    unit_price   REAL    NOT NULL DEFAULT 0,
    line_total   REAL    NOT NULL DEFAULT 0
);

-- -----------------------------------------------------------------------------
-- TABLE: ingestion_logs
-- Full audit trail for every ingestion attempt (success, duplicate, error).
-- -----------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS ingestion_logs (
    id             INTEGER PRIMARY KEY AUTOINCREMENT,
    source_name    TEXT,
    invoice_number TEXT,
    status         TEXT NOT NULL
                       CHECK(status IN ('SUCCESS','DUPLICATE_SKIPPED','NEEDS_REVIEW','ERROR')),
    message        TEXT,
    processed_at   TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%SZ', 'now'))
);

-- -----------------------------------------------------------------------------
-- Indexes for common query patterns
-- -----------------------------------------------------------------------------
CREATE INDEX IF NOT EXISTS idx_invoices_vendor       ON invoices(vendor_id);
CREATE INDEX IF NOT EXISTS idx_invoices_status       ON invoices(validation_status);
CREATE INDEX IF NOT EXISTS idx_invoices_date         ON invoices(invoice_date);
CREATE INDEX IF NOT EXISTS idx_items_invoice         ON invoice_items(invoice_id);
CREATE INDEX IF NOT EXISTS idx_logs_source           ON ingestion_logs(source_name);
CREATE INDEX IF NOT EXISTS idx_logs_status           ON ingestion_logs(status);
