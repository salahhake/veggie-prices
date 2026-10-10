"""يقرأ صفحات الأسعار من sources.txt، يستخرج الأسعار بواسطة Gemini، ويضيفها إلى data/auto_rows.csv"""
import csv, json, os, re, sys
from datetime import datetime, timedelta, timezone
from pathlib import Path
from urllib.parse import urljoin

import requests
from bs4 import BeautifulSoup

MODEL = os.environ.get("GEMINI_MODEL", "gemini-2.5-flash")
FORCE = os.environ.get("FORCE") == "1"
DRY_RUN = os.environ.get("DRY_RUN") == "1"
DATA = Path("data"); DATA.mkdir(exist_ok=True)
ROWS_FILE, STATE_FILE = DATA / "auto_rows.csv", DATA / "auto_state.json"
COLUMNS = ["date", "product", "category", "price", "unit", "market", "currency", "name_ar", "name_en", "source"]
KEYWORDS = ["أسعار", "اسعار", "الخضر", "الفواكه", "mercuriale", "prix", "légumes", "legumes"]
HEADERS = {"User-Agent": "Mozilla/5.0 (compatible; SoukPricesBot/0.1)"}
MAX_CHARS = 12000
MIN_PRICE, MAX_PRICE = 5, 5000          # دينار للكيلو: خارج هذا المدى يُرفض
MAX_AGE_DAYS = 3                         # لا نقبل مقالات أقدم من ذلك
KNOWN = ("potato tomato onion carrot zucchini pepper hot_pepper lettuce eggplant spinach green_beans fava_beans "
         "cucumber fennel artichoke garlic cabbage cauliflower peas turnip pumpkin parsley coriander orange apple "
         "banana dates lemon strawberry watermelon melon grapes pomegranate pear peach apricot fig cherry mango "
         "kiwi pineapple plum avocado mandarin clementine")
PROMPT = f"""You extract fruit and vegetable prices in ALGERIAN DINAR (DA, DZD, دج, دينار) from the text of a web page.
Rules:
- Use ONLY prices clearly stated in Algerian dinar. Ignore any other currency (Egyptian pounds, euros, Moroccan dirhams...).
- If the page is not about Algeria, return an empty items list.
- Never guess or invent a price that is not in the text. If a price is a range, give price_min and price_max (same number if single).
- product: lowercase English snake_case key. Prefer one of these when it fits: {KNOWN}.
- name_ar: the Arabic name. name_en: the English name. category: vegetable, fruit or seed. unit: kg unless the text clearly says otherwise.
- article_date: YYYY-MM-DD if the page states its date, otherwise empty string.
- country: the country the prices are about."""
SCHEMA = {"type": "OBJECT", "properties": {
    "article_date": {"type": "STRING"}, "country": {"type": "STRING"},
    "items": {"type": "ARRAY", "items": {"type": "OBJECT", "properties": {
        "product": {"type": "STRING"}, "name_ar": {"type": "STRING"}, "name_en": {"type": "STRING"},
        "category": {"type": "STRING"}, "price_min": {"type": "NUMBER"}, "price_max": {"type": "NUMBER"},
        "unit": {"type": "STRING"}, "currency": {"type": "STRING"}},
        "required": ["product", "price_min", "price_max", "currency"]}}},
    "required": ["items"]}


def today():
    return (datetime.now(timezone.utc) + timedelta(hours=1)).date()


def read_sources():
    out = []
    p = Path("sources.txt")
    if not p.exists():
        return out
    for line in p.read_text("utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        parts = [x.strip() for x in line.split("|")]
        if parts[0].upper() == "LIST" and len(parts) >= 2:
            out.append(("list", parts[1], parts[2] if len(parts) > 2 else "algeria"))
        else:
            out.append(("page", parts[0], parts[1] if len(parts) > 1 else "algeria"))
    return out


def get_html(url):
    r = requests.get(url, headers=HEADERS, timeout=30)
    r.raise_for_status()
    r.encoding = r.apparent_encoding if r.encoding in (None, "ISO-8859-1") else r.encoding
    return r.text


def first_article(list_url):
    soup = BeautifulSoup(get_html(list_url), "html.parser")
    for a in soup.find_all("a", href=True):
        label = a.get_text(" ", strip=True).lower()
        if len(label) > 15 and any(k.lower() in label for k in KEYWORDS):
            return urljoin(list_url, a["href"])
    return None


def page_text(url):
    soup = BeautifulSoup(get_html(url), "html.parser")
    for t in soup(["script", "style", "nav", "footer", "header", "aside", "form"]):
        t.decompose()
    node = soup.find("article") or soup.body or soup
    text = re.sub(r"\n\s*\n+", "\n", node.get_text("\n", strip=True))
    return text[:MAX_CHARS]


def call_gemini(text):
    key = os.environ.get("GEMINI_API_KEY")
    if not key:
        raise RuntimeError("GEMINI_API_KEY is not set")
    url = f"https://generativelanguage.googleapis.com/v1beta/models/{MODEL}:generateContent"
    body = {"contents": [{"parts": [{"text": PROMPT + "\n\nPAGE TEXT:\n" + text}]}],
            "generationConfig": {"temperature": 0, "responseMimeType": "application/json", "responseSchema": SCHEMA}}
    r = requests.post(url, params={"key": key}, json=body, timeout=90)
    if r.status_code != 200:
        raise RuntimeError(f"Gemini HTTP {r.status_code}: {r.text[:300]}")
    return json.loads(r.json()["candidates"][0]["content"]["parts"][0]["text"])


def to_rows(result, source, market):
    d = today()
    try:
        ad = datetime.strptime((result.get("article_date") or "").strip(), "%Y-%m-%d").date()
    except ValueError:
        ad = d
    if ad > d + timedelta(days=1) or ad < d - timedelta(days=MAX_AGE_DAYS):
        print(f"  stale article date {ad}, skipping")
        return []
    rows = []
    for it in result.get("items", []):
        try:
            if (it.get("currency") or "").strip().upper() not in ("DZD", "DA", "دج", "دينار"):
                raise ValueError(f"currency {it.get('currency')!r}")
            lo, hi = float(it["price_min"]), float(it["price_max"])
            price = round((lo + hi) / 2, 1)
            if not (MIN_PRICE <= price <= MAX_PRICE):
                raise ValueError(f"price {price} out of range")
            product = re.sub(r"[\s-]+", "_", it["product"].strip().lower())
            if not product:
                raise ValueError("empty product")
            cat = (it.get("category") or "vegetable").strip().lower()
            rows.append({"date": ad.isoformat(), "product": product, "category": cat if cat in ("vegetable", "fruit", "seed") else "vegetable",
                         "price": price, "unit": (it.get("unit") or "kg").strip().lower(), "market": market, "currency": "DZD",
                         "name_ar": (it.get("name_ar") or "").strip(), "name_en": (it.get("name_en") or "").strip(), "source": source})
        except Exception as e:
            print(f"  skip item {it.get('product')}: {e}")
    return rows


def existing_keys():
    if not ROWS_FILE.exists():
        return set()
    with ROWS_FILE.open(encoding="utf-8", newline="") as f:
        return {(r["date"], r["product"], r["market"]) for r in csv.DictReader(f)}


def append_rows(rows):
    new_file = not ROWS_FILE.exists()
    with ROWS_FILE.open("a", encoding="utf-8", newline="") as f:
        w = csv.DictWriter(f, fieldnames=COLUMNS)
        if new_file:
            w.writeheader()
        w.writerows(rows)


def main():
    sources = read_sources()
    if not sources:
        print("sources.txt has no sources, nothing to do")
        return
    state = json.loads(STATE_FILE.read_text("utf-8")) if STATE_FILE.exists() else {}
    seen = existing_keys()
    day = today().isoformat()
    total = 0
    for kind, url, market in sources:
        try:
            if state.get(url) == day and not FORCE:
                print(f"already done today: {url}")
                continue
            page_url = first_article(url) if kind == "list" else url
            if not page_url:
                print(f"no matching article found in {url}")
                continue
            print(f"reading {page_url}")
            result = call_gemini(page_text(page_url))
            rows = [r for r in to_rows(result, page_url, market) if (r["date"], r["product"], r["market"]) not in seen]
            print(f"  {len(rows)} new rows")
            if DRY_RUN:
                for r in rows:
                    print("  ", r)
            else:
                append_rows(rows)
                state[url] = day
                seen |= {(r["date"], r["product"], r["market"]) for r in rows}
            total += len(rows)
        except Exception as e:
            print(f"error on {url}: {e}", file=sys.stderr)
    if not DRY_RUN:
        STATE_FILE.write_text(json.dumps(state, ensure_ascii=False, indent=2), "utf-8")
    print(f"done, {total} rows added")


if __name__ == "__main__":
    main()
