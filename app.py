#!/usr/bin/env python3
"""
=============================================================================
Doc2DB-AI · app.py
Unified CLI + HTTP API server for document-to-database automation.

Usage:
  python app.py --init-db              Initialize / migrate finance.db
  python app.py --ingest <file>        Ingest a .txt or .json file from CLI
  python app.py --serve                Start HTTP server on localhost:8080
  python app.py --init-db --serve      Init DB then start server (recommended)
=============================================================================
"""

# ── Standard-library imports only ──────────────────────────────────────────
import sqlite3
import json
import re
import csv
import io
import sys
import os
import argparse
import pathlib
import traceback
from datetime import datetime, timezone
from http.server import HTTPServer, BaseHTTPRequestHandler
from urllib.parse import urlparse, parse_qs

# ── Project paths ───────────────────────────────────────────────────────────
BASE_DIR   = pathlib.Path(__file__).parent.resolve()
SCHEMA_SQL = BASE_DIR / "schema.sql"
DB_PATH    = BASE_DIR / "finance.db"
HTML_PATH  = BASE_DIR / "index.html"
SAMPLES_DIR = BASE_DIR / "samples"

# =============================================================================
# SECTION 1 — DATABASE HELPERS
# =============================================================================

def get_db() -> sqlite3.Connection:
    """Return a WAL-mode connection with row_factory and FK enforcement."""
    conn = sqlite3.connect(str(DB_PATH))
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    conn.execute("PRAGMA journal_mode = WAL")
    return conn


def init_db() -> None:
    """Create / migrate the database using schema.sql."""
    if not SCHEMA_SQL.exists():
        print(f"[ERROR] schema.sql not found at {SCHEMA_SQL}")
        sys.exit(1)
    sql = SCHEMA_SQL.read_text(encoding="utf-8")
    with get_db() as conn:
        conn.executescript(sql)
    print(f"[OK] Database initialized -> {DB_PATH}")


# =============================================================================
# SECTION 2 — EXTRACTION & NORMALIZATION ENGINE
# =============================================================================

# ── Date normalization ───────────────────────────────────────────────────────
_MONTH_MAP = {
    "jan": "01", "january": "01", "januari": "01",
    "feb": "02", "february": "02", "februari": "02",
    "mar": "03", "march": "03", "maret": "03",
    "apr": "04", "april": "04",
    "may": "05", "mei": "05",
    "jun": "06", "june": "06", "juni": "06",
    "jul": "07", "july": "07", "juli": "07",
    "aug": "08", "august": "08", "agustus": "08",
    "sep": "09", "september": "09",
    "oct": "10", "october": "10", "oktober": "10",
    "nov": "11", "november": "11",
    "dec": "12", "december": "12", "desember": "12",
}

def normalize_date(raw: str) -> str | None:
    """Convert many date formats to YYYY-MM-DD. Returns None if unrecognizable."""
    if not raw:
        return None
    raw = raw.strip()
    # Already ISO-8601
    if re.match(r"^\d{4}-\d{2}-\d{2}$", raw):
        return raw
    # DD/MM/YYYY or DD-MM-YYYY or DD.MM.YYYY
    m = re.match(r"^(\d{1,2})[\/\-\.](\d{1,2})[\/\-\.](\d{4})$", raw)
    if m:
        return f"{m.group(3)}-{m.group(2).zfill(2)}-{m.group(1).zfill(2)}"
    # DD Month YYYY  (e.g. "12 November 2026" or "12-Nov-2026")
    m = re.match(r"^(\d{1,2})[\s\-]([A-Za-z]+)[\s\-](\d{4})$", raw)
    if m:
        mon = _MONTH_MAP.get(m.group(2).lower())
        if mon:
            return f"{m.group(3)}-{mon}-{m.group(1).zfill(2)}"
    # Month DD, YYYY
    m = re.match(r"^([A-Za-z]+)\s+(\d{1,2}),?\s+(\d{4})$", raw)
    if m:
        mon = _MONTH_MAP.get(m.group(1).lower())
        if mon:
            return f"{m.group(3)}-{mon}-{m.group(2).zfill(2)}"
    return None


def parse_amount(raw: str) -> float | None:
    """Strip currency symbols and thousand-separators; return float or None."""
    if raw is None:
        return None
    s = str(raw).strip()
    # Remove currency labels
    s = re.sub(r"(?i)(IDR|Rp\.?|USD|\$|€|EUR)", "", s).strip()
    # Remove thousand separators (period used in ID locale)
    # Distinguish: if both '.' and ',' present, treat '.' as thousands and ',' as decimal
    if "." in s and "," in s:
        s = s.replace(".", "").replace(",", ".")
    elif "," in s and "." not in s:
        # e.g. "1.000,00" style already handled above; bare "1,000" → keep comma as thousands
        s = s.replace(",", "")
    elif "." in s:
        # Could be "1.000.000" (ID thousands) or "1.50" (decimal)
        parts = s.split(".")
        if len(parts) > 2 or (len(parts) == 2 and len(parts[-1]) == 3):
            # All dots are thousand separators
            s = s.replace(".", "")
        # else treat single dot as decimal point → leave as is
    s = s.replace(",", "").strip()
    try:
        return float(s)
    except ValueError:
        return None


# ── Raw text extractor ───────────────────────────────────────────────────────

def extract_from_text(text: str, source_name: str = "paste") -> dict:
    """
    Parse unstructured invoice text and return a normalized dict
    compatible with the structured JSON schema.
    """
    lines = [l.strip() for l in text.splitlines()]
    full  = " ".join(lines)

    result = {
        "source_name": source_name,
        "vendor": {"name": None, "tax_id": None, "address": None},
        "invoice_number": None,
        "invoice_date":   None,
        "due_date":       None,
        "currency":       "IDR",
        "subtotal":       None,
        "tax_amount":     None,
        "total_amount":   None,
        "items":          [],
    }

    # --- Vendor name: first non-empty line or after "Vendor:" label
    vendor_label = re.search(r"(?i)(?:vendor|supplier|dari|from)\s*[:\-]?\s*(.+)", full)
    if vendor_label:
        result["vendor"]["name"] = vendor_label.group(1).strip()
    else:
        for ln in lines:
            if ln:
                result["vendor"]["name"] = ln
                break

    # --- Tax ID (NPWP)
    npwp = re.search(r"(?i)(?:npwp|tax[\s\-]?id|no\.?\s*pajak)\s*[:\-]?\s*([\d\.\-]+)", full)
    if npwp:
        result["vendor"]["tax_id"] = npwp.group(1).strip()

    # --- Address
    addr = re.search(r"(?i)(?:alamat|address|jl\.|jalan)\s*[:\-]?\s*(.+?)(?:\n|$)", text)
    if addr:
        result["vendor"]["address"] = addr.group(1).strip()

    # --- Invoice number (require label + separator + alphanumeric value with digit)
    inv_no = re.search(
        r"(?i)(?:invoice[\s\-]?(?:no\.?|number|#|num)|no\.?\s*(?:invoice|faktur)|nomor[\s\-]?(?:invoice|faktur))"
        r"\s*[:\-]\s*([A-Z0-9][A-Z0-9\-/]*\d[A-Z0-9\-/]*)",
        full
    )
    if not inv_no:
        # Fallback: NF/YYYY/MM/NNNN style
        inv_no = re.search(r"\b((?:INV|NF|NO|FAK|INV)[-/][A-Z0-9\-/]+)", full)
    if inv_no:
        result["invoice_number"] = inv_no.group(1).strip()

    # --- Invoice date
    inv_date = re.search(
        r"(?i)(?:tanggal[\s\-]?(?:invoice|faktur)?|invoice[\s\-]?date|tgl\.?)\s*[:\-]?\s*"
        r"(\d{1,2}[\s\-\/\.][A-Za-z]+[\s\-\/\.]\d{4}|\d{1,2}[\/\-\.]\d{1,2}[\/\-\.]\d{4}|\d{4}-\d{2}-\d{2})",
        full
    )
    if inv_date:
        result["invoice_date"] = normalize_date(inv_date.group(1).strip())

    # --- Due date
    due_date = re.search(
        r"(?i)(?:jatuh[\s\-]?tempo|due[\s\-]?date|tanggal[\s\-]?jatuh)\s*[:\-]?\s*"
        r"(\d{1,2}[\s\-\/\.][A-Za-z]+[\s\-\/\.]\d{4}|\d{1,2}[\/\-\.]\d{1,2}[\/\-\.]\d{4}|\d{4}-\d{2}-\d{2})",
        full
    )
    if due_date:
        result["due_date"] = normalize_date(due_date.group(1).strip())

    # --- Currency
    if re.search(r"\bUSD\b|\$", full):
        result["currency"] = "USD"
    elif re.search(r"\bEUR\b|€", full):
        result["currency"] = "EUR"

    # Amount token that optionally allows Rp/IDR prefix before digits
    _RP = r"(?:(?:IDR|Rp\.?)\s*)?"

    # --- Line items: look for table-like rows  "description  qty  unit_price  total"
    item_patterns = [
        # "1. Item Name  3   Rp 8.500.000   Rp 25.500.000"
        r"^(\d+)[.\)]\s+(.+?)\s{2,}(\d[\d\.,]*)\s+" + _RP + r"([\d][\d\.,]*)\s+" + _RP + r"([\d][\d\.,]*)\s*$",
        # "1. Item Name  2  150.000  300.000"
        r"(\d+)\.\s+(.+?)\s{2,}(\d[\d\.,]*)\s{2,}(\d[\d\.,]*)\s{2,}(\d[\d\.,]*)",
        # "Item Name | 2 | 150.000 | 300.000"
        r"(.+?)\s*\|\s*(\d[\d\.,]*)\s*\|\s*(\d[\d\.,]*)\s*\|\s*(\d[\d\.,]*)",
        # "Item Name  2 x 150.000 = 300.000"
        r"(.+?)\s+(\d[\d\.,]*)\s*[xX]\s*(\d[\d\.,]*)\s*[=]\s*(\d[\d\.,]*)",
    ]
    for ln in lines:
        matched = False
        for pat in item_patterns:
            m = re.match(pat, ln)
            if m:
                groups = m.groups()
                if len(groups) == 5:
                    desc, qty, up, lt = groups[1], groups[2], groups[3], groups[4]
                elif len(groups) == 4:
                    desc, qty, up, lt = groups[0], groups[1], groups[2], groups[3]
                else:
                    continue
                result["items"].append({
                    "description": desc.strip(),
                    "quantity":    parse_amount(qty) or 1,
                    "unit_price":  parse_amount(up)  or 0,
                    "line_total":  parse_amount(lt)  or 0,
                })
                matched = True
                break
        if matched:
            continue

    # --- Subtotal (allow optional Rp/IDR prefix before number)
    sub = re.search(
        r"(?i)(?:subtotal|sub[\s\-]?total|dpp|harga[\s\-]?sebelum[\s\-]?pajak)\s*[:\-]?\s*(?:IDR|Rp\.?)?\s*([\d][\d\.,]*)",
        full
    )
    if sub:
        result["subtotal"] = parse_amount(sub.group(1))

    # --- Tax (PPN) — allow optional Rp/IDR prefix
    tax = re.search(
        r"(?i)(?:ppn|tax|pajak|vat)\s*(?:\d+\s*%?)?\s*[:\-]?\s*(?:IDR|Rp\.?)?\s*([\d][\d\.,]*)",
        full
    )
    if tax:
        result["tax_amount"] = parse_amount(tax.group(1))

    # --- Total (allow optional Rp/IDR prefix; exclude "subtotal" prefix match)
    total = re.search(
        r"(?i)(?<!sub)(?:total[\s\-]?(?:tagihan|bayar|pembayaran|amount|keseluruhan)?|grand[\s\-]?total|jumlah[\s\-]?total)\s*[:\-]?\s*(?:IDR|Rp\.?)?\s*([\d][\d\.,]*)",
        full
    )
    if total:
        result["total_amount"] = parse_amount(total.group(1))

    # --- Fallback: derive subtotal from items if missing
    if result["items"] and result["subtotal"] is None:
        result["subtotal"] = round(sum(it["line_total"] for it in result["items"]), 2)

    return result


# =============================================================================
# SECTION 3 — VALIDATION ENGINE
# =============================================================================

TOLERANCE = 1.0   # IDR rounding tolerance

def validate_invoice(data: dict) -> tuple[str, str]:
    """
    Cross-check math and required fields.
    Returns (status, notes) where status is 'VERIFIED' or 'NEEDS_REVIEW'.
    """
    notes = []
    status = "VERIFIED"

    # Required fields
    for field in ("vendor", "invoice_number"):
        if not data.get(field):
            notes.append(f"Kolom wajib kosong: {field}.")
            status = "NEEDS_REVIEW"

    vendor = data.get("vendor") or {}
    if not vendor.get("name"):
        notes.append("Nama vendor tidak ditemukan.")
        status = "NEEDS_REVIEW"

    subtotal     = data.get("subtotal")     or 0.0
    tax_amount   = data.get("tax_amount")   or 0.0
    total_amount = data.get("total_amount") or 0.0
    items        = data.get("items")        or []

    # Math check 1: sum(line_total) == subtotal
    if items:
        computed_sub = round(sum(it.get("line_total", 0) for it in items), 2)
        if abs(computed_sub - subtotal) > TOLERANCE:
            notes.append(
                f"Jumlah baris ({computed_sub:,.2f}) != subtotal ({subtotal:,.2f}). "
                f"Selisih: {abs(computed_sub - subtotal):,.2f}."
            )
            status = "NEEDS_REVIEW"

    # Math check 2: subtotal + tax == total
    if total_amount > 0:
        expected_total = round(subtotal + tax_amount, 2)
        if abs(expected_total - total_amount) > TOLERANCE:
            notes.append(
                f"Subtotal ({subtotal:,.2f}) + Pajak ({tax_amount:,.2f}) = "
                f"{expected_total:,.2f} != Total ({total_amount:,.2f}). "
                f"Selisih: {abs(expected_total - total_amount):,.2f}."
            )
            status = "NEEDS_REVIEW"

    return status, (" | ".join(notes) if notes else None)


# =============================================================================
# SECTION 4 — DATABASE UPSERT ENGINE
# =============================================================================

def upsert_invoice(data: dict) -> dict:
    """
    Insert a normalized invoice dict into the database.
    Returns a result dict with keys: status, invoice_id, message.
    """
    source    = data.get("source_name", "unknown")
    inv_no    = data.get("invoice_number")
    vendor_d  = data.get("vendor") or {}
    vname     = (vendor_d.get("name") or "").strip()

    if not vname or not inv_no:
        msg = "Nama vendor atau nomor invoice kosong - tidak dapat disimpan."
        _log(source, inv_no, "ERROR", msg)
        return {"status": "ERROR", "invoice_id": None, "message": msg}

    val_status, val_notes = validate_invoice(data)

    # Collect log info outside the DB transaction to avoid "database is locked"
    _pending_log: list = []  # [(status, msg)] to log after commit

    try:
        with get_db() as conn:
            # --- Upsert vendor
            conn.execute(
                "INSERT INTO vendors(name, tax_id, address) VALUES(?,?,?) "
                "ON CONFLICT(name) DO UPDATE SET "
                "tax_id  = COALESCE(excluded.tax_id,  vendors.tax_id), "
                "address = COALESCE(excluded.address, vendors.address)",
                (vname, vendor_d.get("tax_id"), vendor_d.get("address"))
            )
            vendor_id = conn.execute(
                "SELECT id FROM vendors WHERE name = ?", (vname,)
            ).fetchone()["id"]

            # --- Duplicate check
            dup = conn.execute(
                "SELECT id FROM invoices WHERE vendor_id=? AND invoice_number=?",
                (vendor_id, inv_no)
            ).fetchone()
            if dup:
                msg = f"Duplikat dilewati: Invoice {inv_no} dari {vname} sudah ada (ID={dup['id']})."
                _pending_log.append(("DUPLICATE_SKIPPED", msg))
                # Return value captured after logging below
                _result = {"status": "DUPLICATE_SKIPPED", "invoice_id": dup["id"], "message": msg}
            else:
                # --- Insert invoice
                cur = conn.execute(
                    """INSERT INTO invoices
                       (vendor_id, invoice_number, invoice_date, due_date, currency,
                        subtotal, tax_amount, total_amount,
                        validation_status, validation_notes, source_name)
                       VALUES (?,?,?,?,?,?,?,?,?,?,?)""",
                    (
                        vendor_id,
                        inv_no,
                        data.get("invoice_date"),
                        data.get("due_date"),
                        data.get("currency", "IDR"),
                        data.get("subtotal")     or 0.0,
                        data.get("tax_amount")   or 0.0,
                        data.get("total_amount") or 0.0,
                        val_status,
                        val_notes,
                        source,
                    )
                )
                invoice_id = cur.lastrowid

                # --- Insert line items
                for item in (data.get("items") or []):
                    conn.execute(
                        "INSERT INTO invoice_items(invoice_id, description, quantity, unit_price, line_total) "
                        "VALUES (?,?,?,?,?)",
                        (
                            invoice_id,
                            item.get("description", ""),
                            item.get("quantity",    1),
                            item.get("unit_price",  0),
                            item.get("line_total",  0),
                        )
                    )

                log_status = val_status if val_status != "VERIFIED" else "SUCCESS"
                msg = f"Invoice {inv_no} dari {vname} berhasil disimpan (status: {val_status})."
                _pending_log.append((log_status, msg))
                _result = {"status": val_status, "invoice_id": invoice_id, "message": msg}

        # Transaction committed — now write the audit log safely
        for ls, lm in _pending_log:
            _log(source, inv_no, ls, lm)
        return _result

    except Exception as exc:
        tb = traceback.format_exc()
        msg = f"Kesalahan database: {exc}"
        _log(source, inv_no, "ERROR", msg)
        return {"status": "ERROR", "invoice_id": None, "message": msg, "traceback": tb}


def _log(source: str, inv_no: str | None, status: str, message: str) -> None:
    """Write a record to ingestion_logs (best-effort, separate connection)."""
    try:
        with get_db() as conn:
            conn.execute(
                "INSERT INTO ingestion_logs(source_name, invoice_number, status, message) VALUES(?,?,?,?)",
                (source, inv_no, status, message)
            )
    except Exception:
        pass  # Logging must never crash the main flow


# =============================================================================
# SECTION 5 — CLI INGEST COMMAND
# =============================================================================

def cli_ingest(filepath: str) -> None:
    """Ingest a .txt or .json file from the command line."""
    p = pathlib.Path(filepath)
    if not p.exists():
        print(f"[ERROR] Berkas tidak ditemukan: {filepath}")
        sys.exit(1)

    text = p.read_text(encoding="utf-8")
    suffix = p.suffix.lower()

    if suffix == ".json":
        try:
            data = json.loads(text)
        except json.JSONDecodeError as e:
            print(f"[ERROR] JSON tidak valid: {e}")
            sys.exit(1)
        data["source_name"] = p.name
    else:
        data = extract_from_text(text, source_name=p.name)

    result = upsert_invoice(data)
    icon = "[OK]"  if result["status"] in ("VERIFIED", "SUCCESS") else \
           "[!!]"  if result["status"] == "NEEDS_REVIEW" else \
           "[>>]"  if result["status"] == "DUPLICATE_SKIPPED" else "[ERR]"
    print(f"[{icon}] {result['status']} - {result['message']}")


# =============================================================================
# SECTION 6 — HTTP API HANDLERS
# =============================================================================

class Doc2DBHandler(BaseHTTPRequestHandler):

    def log_message(self, fmt, *args):  # Suppress default Apache-style logs
        pass

    # ── Routing ──────────────────────────────────────────────────────────────
    def do_GET(self):
        parsed = urlparse(self.path)
        path   = parsed.path.rstrip("/") or "/"

        routes = {
            "/":               self._serve_html,
            "/api/invoices":   self._api_invoices,
            "/api/logs":       self._api_logs,
            "/api/kpi":        self._api_kpi,
            "/api/export/csv": self._api_export_csv,
            "/api/export/sql": self._api_export_sql,
            "/api/query":      self._api_query,
        }
        handler = routes.get(path)
        if handler:
            handler(parsed)
        elif path.startswith("/api/invoice-items/"):
            self._api_invoice_items(path)
        else:
            self._send_json({"error": "Endpoint tidak ditemukan."}, 404)

    def do_POST(self):
        parsed = urlparse(self.path)
        path   = parsed.path.rstrip("/")

        routes = {
            "/api/extract":    self._api_extract,
            "/api/ingest-json": self._api_ingest_json,
        }
        handler = routes.get(path)
        if handler:
            handler(parsed)
        else:
            self._send_json({"error": "Endpoint tidak ditemukan."}, 404)

    def do_OPTIONS(self):
        self.send_response(200)
        self._set_cors()
        self.end_headers()

    # ── Helpers ───────────────────────────────────────────────────────────────
    def _set_cors(self):
        self.send_header("Access-Control-Allow-Origin",  "*")
        self.send_header("Access-Control-Allow-Methods", "GET, POST, OPTIONS")
        self.send_header("Access-Control-Allow-Headers", "Content-Type")

    def _send_json(self, obj, status=200):
        body = json.dumps(obj, ensure_ascii=False, default=str).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self._set_cors()
        self.end_headers()
        self.wfile.write(body)

    def _send_bytes(self, data: bytes, content_type: str, filename: str):
        self.send_response(200)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Disposition", f'attachment; filename="{filename}"')
        self.send_header("Content-Length", str(len(data)))
        self._set_cors()
        self.end_headers()
        self.wfile.write(data)

    def _read_body(self) -> bytes:
        length = int(self.headers.get("Content-Length", 0))
        return self.rfile.read(length)

    # ── GET /  ─────────────────────────────────────────────────────────────
    def _serve_html(self, _parsed):
        if HTML_PATH.exists():
            body = HTML_PATH.read_bytes()
            self.send_response(200)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
        else:
            self._send_json({"error": "index.html tidak ditemukan."}, 404)

    # ── GET /api/kpi ────────────────────────────────────────────────────────
    def _api_kpi(self, _parsed):
        with get_db() as conn:
            row = conn.execute("""
                SELECT
                  COUNT(*)                                       AS total_invoices,
                  COALESCE(SUM(total_amount), 0)                 AS total_pengeluaran,
                  COUNT(CASE WHEN validation_status='VERIFIED'     THEN 1 END) AS verified,
                  COUNT(CASE WHEN validation_status='NEEDS_REVIEW' THEN 1 END) AS needs_review
                FROM invoices
            """).fetchone()
        self._send_json(dict(row))

    # ── GET /api/invoices ───────────────────────────────────────────────────
    def _api_invoices(self, parsed):
        qs = parse_qs(parsed.query)
        search = qs.get("q", [""])[0]
        status = qs.get("status", [""])[0]

        sql = """
            SELECT i.id, v.name AS vendor_name, i.invoice_number,
                   i.invoice_date, i.due_date, i.currency,
                   i.subtotal, i.tax_amount, i.total_amount,
                   i.validation_status, i.validation_notes,
                   i.source_name, i.created_at
            FROM invoices i
            JOIN vendors v ON v.id = i.vendor_id
            WHERE 1=1
        """
        params: list = []
        if search:
            sql += " AND (v.name LIKE ? OR i.invoice_number LIKE ?)"
            params += [f"%{search}%", f"%{search}%"]
        if status:
            sql += " AND i.validation_status = ?"
            params.append(status)
        sql += " ORDER BY i.created_at DESC"

        with get_db() as conn:
            rows = conn.execute(sql, params).fetchall()
        self._send_json([dict(r) for r in rows])

    # ── GET /api/invoice-items/<id> ─────────────────────────────────────────
    def _api_invoice_items(self, path):
        m = re.match(r"^/api/invoice-items/(\d+)$", path)
        if not m:
            self._send_json({"error": "ID tidak valid."}, 400)
            return
        inv_id = int(m.group(1))
        with get_db() as conn:
            rows = conn.execute(
                "SELECT * FROM invoice_items WHERE invoice_id=? ORDER BY id",
                (inv_id,)
            ).fetchall()
        self._send_json([dict(r) for r in rows])

    # ── GET /api/logs ────────────────────────────────────────────────────────
    def _api_logs(self, _parsed):
        with get_db() as conn:
            rows = conn.execute(
                "SELECT * FROM ingestion_logs ORDER BY processed_at DESC LIMIT 200"
            ).fetchall()
        self._send_json([dict(r) for r in rows])

    # ── GET /api/export/csv ─────────────────────────────────────────────────
    def _api_export_csv(self, _parsed):
        with get_db() as conn:
            rows = conn.execute("""
                SELECT i.id, v.name, i.invoice_number, i.invoice_date, i.due_date,
                       i.currency, i.subtotal, i.tax_amount, i.total_amount,
                       i.validation_status, i.validation_notes, i.source_name, i.created_at
                FROM invoices i JOIN vendors v ON v.id=i.vendor_id
                ORDER BY i.id
            """).fetchall()
        buf = io.StringIO()
        writer = csv.writer(buf)
        writer.writerow(["id","vendor","invoice_number","invoice_date","due_date",
                          "currency","subtotal","tax_amount","total_amount",
                          "validation_status","validation_notes","source_name","created_at"])
        for r in rows:
            writer.writerow(list(r))
        self._send_bytes(buf.getvalue().encode("utf-8"), "text/csv", "invoices.csv")

    # ── GET /api/export/sql ─────────────────────────────────────────────────
    def _api_export_sql(self, _parsed):
        with get_db() as conn:
            lines = []
            for tbl in ("vendors", "invoices", "invoice_items", "ingestion_logs"):
                rows = conn.execute(f"SELECT * FROM {tbl}").fetchall()
                for row in rows:
                    cols = row.keys()
                    vals = ", ".join(
                        f"'{str(v).replace(chr(39), chr(39)*2)}'" if v is not None else "NULL"
                        for v in row
                    )
                    lines.append(f"INSERT INTO {tbl} ({', '.join(cols)}) VALUES ({vals});")
        dump = "\n".join(lines).encode("utf-8")
        self._send_bytes(dump, "text/plain", "finance_export.sql")

    # ── GET /api/query ───────────────────────────────────────────────────────
    def _api_query(self, parsed):
        qs  = parse_qs(parsed.query)
        sql = qs.get("sql", [""])[0].strip()
        if not sql:
            self._send_json({"error": "Parameter 'sql' diperlukan."}, 400)
            return
        # Safety: only allow SELECT statements
        if not re.match(r"(?i)^\s*SELECT\b", sql):
            self._send_json({"error": "Hanya query SELECT yang diizinkan."}, 403)
            return
        # Block dangerous keywords
        blocked = re.compile(r"(?i)\b(DROP|DELETE|UPDATE|INSERT|ALTER|CREATE|ATTACH|PRAGMA|EXEC)\b")
        if blocked.search(sql):
            self._send_json({"error": "Query mengandung perintah yang dilarang."}, 403)
            return
        try:
            with get_db() as conn:
                cur  = conn.execute(sql)
                rows = cur.fetchall()
                cols = [d[0] for d in cur.description] if cur.description else []
            self._send_json({"columns": cols, "rows": [list(r) for r in rows]})
        except Exception as exc:
            self._send_json({"error": str(exc)}, 400)

    # ── POST /api/extract ────────────────────────────────────────────────────
    def _api_extract(self, _parsed):
        try:
            body = self._read_body()
            payload = json.loads(body.decode("utf-8"))
            text   = payload.get("text", "")
            source = payload.get("source_name", "paste")
            if not text.strip():
                self._send_json({"error": "Teks dokumen kosong."}, 400)
                return
            data = extract_from_text(text, source_name=source)
            # Also run validation preview (don't save)
            val_status, val_notes = validate_invoice(data)
            data["validation_status"] = val_status
            data["validation_notes"]  = val_notes
            self._send_json({"ok": True, "data": data})
        except Exception as exc:
            self._send_json({"error": str(exc)}, 500)

    # ── POST /api/ingest-json ────────────────────────────────────────────────
    def _api_ingest_json(self, _parsed):
        try:
            body = self._read_body()
            data = json.loads(body.decode("utf-8"))
            if not isinstance(data, dict):
                self._send_json({"error": "Payload harus berupa objek JSON."}, 400)
                return
            result = upsert_invoice(data)
            http_status = 200 if result["status"] in ("VERIFIED","NEEDS_REVIEW","DUPLICATE_SKIPPED") else 400
            self._send_json(result, http_status)
        except json.JSONDecodeError as e:
            self._send_json({"error": f"JSON tidak valid: {e}"}, 400)
        except Exception as exc:
            self._send_json({"error": str(exc)}, 500)


# =============================================================================
# SECTION 7 — ENTRY POINT
# =============================================================================

def main():
    parser = argparse.ArgumentParser(
        prog="doc2db-ai",
        description="Doc2DB-AI - Document-to-Database Automation Engine"
    )
    parser.add_argument("--init-db", action="store_true",
                        help="Initialize / migrate finance.db from schema.sql")
    parser.add_argument("--ingest",  metavar="FILE",
                        help="Ingest a .txt or .json invoice file")
    parser.add_argument("--serve",   action="store_true",
                        help="Start the HTTP dashboard server on port 8080")
    parser.add_argument("--port",    type=int, default=8080,
                        help="HTTP server port (default: 8080)")

    args = parser.parse_args()

    if not any([args.init_db, args.ingest, args.serve]):
        parser.print_help()
        sys.exit(0)

    if args.init_db:
        init_db()

    if args.ingest:
        if not DB_PATH.exists():
            print("[ERROR] Database belum diinisialisasi. Jalankan --init-db terlebih dahulu.")
            sys.exit(1)
        cli_ingest(args.ingest)

    if args.serve:
        if not DB_PATH.exists():
            print("[INFO] Database belum ada - menginisialisasi otomatis...")
            init_db()
        host, port = "localhost", args.port
        server = HTTPServer((host, port), Doc2DBHandler)
        print(f"[OK] Doc2DB-AI berjalan di  ->  http://{host}:{port}")
        print(f"     Database               ->  {DB_PATH}")
        print("     Tekan Ctrl+C untuk menghentikan server.")
        try:
            server.serve_forever()
        except KeyboardInterrupt:
            print("\n[INFO] Server dihentikan.")


if __name__ == "__main__":
    main()
