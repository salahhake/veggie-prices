import csv, io, json, os, sys
from datetime import datetime, timezone
from pathlib import Path

import requests

SHEET_CSV_URL = os.environ.get("SHEET_CSV_URL")
DATA = Path("data")
DATA.mkdir(exist_ok=True)

AR_DIGITS = str.maketrans("٠١٢٣٤٥٦٧٨٩٫٬", "0123456789.,")
DATE_FORMATS = ["%Y-%m-%d", "%Y/%m/%d", "%d/%m/%Y", "%d-%m-%Y", "%m/%d/%Y"]


def clean(v):
    return (v or "").translate(AR_DIGITS).strip()


def parse_date(s):
    s = clean(s)
    for fmt in DATE_FORMATS:
        try:
            return datetime.strptime(s, fmt).strftime("%Y-%m-%d")
        except ValueError:
            pass
    raise ValueError(f"bad date: {s!r}")


def load_rows():
    r = requests.get(SHEET_CSV_URL, timeout=30)
    r.raise_for_status()
    r.encoding = "utf-8"
    reader = csv.DictReader(io.StringIO(r.text))
    print("columns:", reader.fieldnames)
    rows = []
    for i, row in enumerate(reader, start=2):
        if not any(clean(v) for v in row.values()):
            continue
        try:
            row = {(k or "").strip().lower(): v for k, v in row.items()}
            date = parse_date(row.get("date"))
            product = clean(row.get("product")).lower()
            price = float(clean(row.get("price")).replace(",", "."))
            if not product:
                raise ValueError("empty product")
            if price <= 0:
                raise ValueError("price must be > 0")
        except Exception as e:
            print(f"skip row {i}: {e} | raw: {dict(row)}", file=sys.stderr)
            continue
        rows.append({
            "date": date, "product": product, "price": price,
            "category": clean(row.get("category")),
            "unit": clean(row.get("unit")) or "kg",
            "market": clean(row.get("market")),
            "currency": (clean(row.get("currency")) or "DZD").upper(),
        })
    return rows


def main():
    if not SHEET_CSV_URL:
        sys.exit("SHEET_CSV_URL is not set")
    rows = load_rows()
    if not rows:
        sys.exit("No valid rows, keeping old data")
    rows.sort(key=lambda x: x["date"])

    latest, history = {}, {}
    for r in rows:
        key = f'{r["product"]}@{r["market"]}' if r["market"] else r["product"]
        prev = latest.get(key)
        entry = {k: r[k] for k in ("product", "category", "price", "unit", "market", "currency")}
        entry["date"] = r["date"]
        entry["change"] = round(r["price"] - prev["price"], 2) if prev else 0
        latest[key] = entry
        series = history.setdefault(key, [])
        if series and series[-1]["date"] == r["date"]:
            series[-1]["price"] = r["price"]
        else:
            series.append({"date": r["date"], "price": r["price"]})

    out = {"generated_at": datetime.now(timezone.utc).isoformat(timespec="seconds"), "prices": latest}
    (DATA / "prices.json").write_text(json.dumps(out, ensure_ascii=False, indent=2), "utf-8")
    (DATA / "history.json").write_text(json.dumps(history, ensure_ascii=False, indent=2), "utf-8")
    print(f"done: {len(latest)} items, {len(rows)} rows")


if __name__ == "__main__":
    main()
