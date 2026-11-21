# AGENTS.md — Doc2DB-AI · IBM Bob Agent Instructions

> **Project:** Doc2DB-AI · IBM Global AI Builders Challenge (IT Track)  
> **Purpose:** This file tells IBM Bob (or any compatible AI Agent) exactly how to extract structured data from business documents and load them into the Doc2DB-AI SQLite database.

---

## Overview

Doc2DB-AI accepts two ingestion paths:

| Path | Input | Endpoint / Flag |
|------|-------|-----------------|
| **Raw text parser** | Unstructured `.txt` pasted or uploaded | `POST /api/extract` → review → `POST /api/ingest-json` |
| **Structured JSON loader** | Pre-extracted JSON from an AI agent | `python app.py --ingest file.json` or `POST /api/ingest-json` |

Bob's job is **Path 2**: read a document, extract the fields below into a valid JSON object, and POST it to the running server (or save it as a `.json` file for CLI ingestion).

---

## Target JSON Schema

Every document Bob processes must be serialized as a single JSON object matching this schema. All keys are **lowercase snake_case**.

```json
{
  "source_name":    "string — original filename or brief description",
  "vendor": {
    "name":   "string (REQUIRED) — legal company / vendor name",
    "tax_id": "string or null    — NPWP / Tax Registration Number",
    "address":"string or null    — full postal address"
  },
  "invoice_number": "string (REQUIRED) — unique invoice / nota number",
  "invoice_date":   "YYYY-MM-DD or null — issue date in ISO-8601",
  "due_date":       "YYYY-MM-DD or null — payment due date in ISO-8601",
  "currency":       "IDR | USD | EUR    — default IDR if unspecified",
  "subtotal":       123456.00,
  "tax_amount":     13580.00,
  "total_amount":   137036.00,
  "items": [
    {
      "description": "string — line-item name or service description",
      "quantity":    1.0,
      "unit_price":  123456.00,
      "line_total":  123456.00
    }
  ]
}
```

### Field rules

| Field | Required? | Rule |
|-------|-----------|------|
| `vendor.name` | ✅ | Extract from letterhead or "Vendor:" / "Supplier:" / "Dari:" label |
| `invoice_number` | ✅ | Look for "Invoice No.", "Nomor Faktur", "No. NF", "Faktur #" |
| `invoice_date` | Recommended | Normalize **all date formats** to `YYYY-MM-DD` |
| `due_date` | Optional | "Jatuh Tempo", "Due Date", "Tanggal Bayar" |
| `currency` | Optional | Default `IDR`; detect USD/EUR from symbols or labels |
| `subtotal` | Recommended | "Subtotal", "DPP", "Harga Sebelum Pajak" |
| `tax_amount` | Optional | "PPN", "VAT", "Tax" — strip `%` and compute actual value |
| `total_amount` | Recommended | "Total Tagihan", "Grand Total", "Jumlah Total" |
| `items` | Optional | Table rows with qty × unit_price = line_total |

**Never invent values.** If a field is missing or illegible, set it to `null`. Missing `vendor.name` or `invoice_number` will be flagged `NEEDS_REVIEW` and not saved.

---

## Normalization Rules Bob MUST Follow

### 1. Date formats
Convert all encountered date formats to `YYYY-MM-DD`:

```
"12 November 2026"  →  "2026-11-12"
"18/11/2026"        →  "2026-11-18"
"18-Nov-26"         →  "2026-11-18"
"November 12, 2026" →  "2026-11-12"
```

### 2. Currency & amounts
Strip all currency symbols and thousand-separators before converting to a float:

```
"Rp 34.600.000"   →  34600000.0    (Indonesian locale: dot = thousands)
"Rp 34,600,000"   →  34600000.0    (US locale)
"IDR 1.500.000,50" → 1500000.50   (dot = thousands, comma = decimal)
"$ 1,200.50"       → 1200.50
```

### 3. Tax calculation
If only a **percentage** is stated (e.g., "PPN 11%"), compute:

```
tax_amount = round(subtotal * 0.11, 2)
```

### 4. Line items
Map table columns to: `description`, `quantity`, `unit_price`, `line_total`.  
If `line_total` is missing, compute: `line_total = quantity * unit_price`.

---

## Sending Data to Doc2DB-AI

### Option A — HTTP API (server must be running)

```bash
# Start the server first (once):
python app.py --init-db --serve

# Then POST extracted JSON:
curl -X POST http://localhost:8080/api/ingest-json \
  -H "Content-Type: application/json" \
  -d @extracted_invoice.json
```

**Response codes:**

| HTTP | `status` field | Meaning |
|------|---------------|---------|
| 200 | `VERIFIED` | Saved, all math checks passed |
| 200 | `NEEDS_REVIEW` | Saved with warnings (math mismatch / missing fields) |
| 200 | `DUPLICATE_SKIPPED` | Already exists — not saved again |
| 400 | `ERROR` | Critical field missing or DB error |

### Option B — CLI (no server needed)

```bash
# Save the JSON object to a file, then:
python app.py --ingest extracted_invoice.json
```

---

## Validation Bob Should Pre-Check

Before submitting, verify:

1. `sum(item.line_total for item in items)` ≈ `subtotal` (tolerance ±1 IDR)
2. `subtotal + tax_amount` ≈ `total_amount` (tolerance ±1 IDR)
3. If checks fail, **still submit** — Doc2DB-AI will flag `NEEDS_REVIEW` and record the exact discrepancy in `validation_notes`.

---

## Example — Valid Extraction

**Input document excerpt:**
```
PT. MAJU BERSAMA TEKNOLOGI
NPWP: 01.234.567.8-091.000
Invoice No. : INV-2026-0042
Tanggal     : 12 November 2026

1. Lisensi Software ERP   2   Rp 5.000.000   Rp 10.000.000
2. Jasa Implementasi     10   Rp 1.500.000   Rp 15.000.000

Subtotal   Rp 25.000.000
PPN 11%    Rp  2.750.000
Total      Rp 27.750.000
```

**Bob's output JSON:**
```json
{
  "source_name": "faktur_mbt_0042.txt",
  "vendor": {
    "name":    "PT. MAJU BERSAMA TEKNOLOGI",
    "tax_id":  "01.234.567.8-091.000",
    "address": null
  },
  "invoice_number": "INV-2026-0042",
  "invoice_date":   "2026-11-12",
  "due_date":       null,
  "currency":       "IDR",
  "subtotal":       25000000.0,
  "tax_amount":     2750000.0,
  "total_amount":   27750000.0,
  "items": [
    { "description": "Lisensi Software ERP",  "quantity": 2,  "unit_price": 5000000.0, "line_total": 10000000.0 },
    { "description": "Jasa Implementasi",      "quantity": 10, "unit_price": 1500000.0, "line_total": 15000000.0 }
  ]
}
```

---

## Audit Trail

Every ingestion attempt — whether successful, flagged, or duplicate — is recorded in `ingestion_logs`. Bob can query the audit log via:

```
GET http://localhost:8080/api/logs
```

or directly in SQL Explorer at `http://localhost:8080` using:

```sql
SELECT * FROM ingestion_logs ORDER BY processed_at DESC LIMIT 20;
```

---

## Quick-Start

```bash
# 1. Initialize database
python app.py --init-db

# 2. Ingest a sample file
python app.py --ingest samples/sample1_valid.txt

# 3. Launch dashboard
python app.py --serve
# → Open http://localhost:8080
```

---

*Doc2DB-AI — IBM Global AI Builders Challenge IT Track*  
*Stack: Python 3 stdlib only · SQLite · Zero external dependencies*
