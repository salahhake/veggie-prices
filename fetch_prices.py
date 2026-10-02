import csv, io, json, os, sys
from datetime import datetime, timezone
from pathlib import Path

import requests

SHEET_CSV_URL = os.environ.get("SHEET_CSV_URL")
DATA = Path("data")
DATA.mkdir(exist_ok=True)


def load_rows():
    r = requests.get(SHEET_CSV_URL, timeout=30)
    r.raise_for_status()
    r.encoding = "utf-8"
    rows = []
    for i, row in enumerate(csv.DictReader(io.StringIO(r.text)), start=2):
        try:
            date = row["date"].strip()
            datetime.strptime(date, "%Y-%m-%d")
            product = row["product"].strip().lower()
            price = float(row["price"].replace(",", "."))
            if not product or price <= 0:
                raise ValueError
        except (KeyError, ValueError, AttributeError):
            if any((v or "").strip() for v in row.values()):
                print(f"skip row {i}", file=sys.stderr)
            continue
        rows.append({
            "date": date, "product": product, "price": price,
            "category": (row.get("category") or "").strip(),
            "unit": (row.get("unit") or "kg").strip(),
            "market": (row.get("market") or "").strip(),
            "currency": (row.get("currency") or "DZD").strip().upper(),
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
