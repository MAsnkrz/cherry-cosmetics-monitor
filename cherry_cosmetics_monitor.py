"""
Cherry Cosmetics Monitor — Clean Rewrite
Monitors https://www.cherrycosmetics.co.uk via WooCommerce Store API

Alerts on:
  🆕 New listings (in stock only)
  🟢 Back in stock (was OOS, now in stock)
  📦 Restock (stock increased meaningfully)
  📉 Price drops (>1% AND >£0.02)

Key fixes over previous version:
  - No more scraping every product page on every run (was causing timeouts)
  - WooCommerce API provides stock, price, barcode — no page scrapes needed
  - Only scrapes detail pages for NEW products (to get barcode if missing from API)
  - Back in stock: scrapes detail page if barcode missing from snapshot
  - Baseline flag file for reliable first-run detection
  - Atomic snapshot saves — crash-safe
  - SAS EAN + SAS Title links using inc-VAT unit price

Env vars:
  DISCORD_WEBHOOK   required
  CHECK_INTERVAL    seconds (default 3600 = 60 min)
  RUN_ONCE          "true" for GitHub Actions

Deps: pip install requests beautifulsoup4
"""

import json
import os
import re
import time
import random
import requests
from datetime import datetime, timezone
from urllib.parse import quote
from bs4 import BeautifulSoup

# ---------------------------------------------------------------------------
# CONFIG
# ---------------------------------------------------------------------------

BASE_URL       = "https://www.cherrycosmetics.co.uk"
API_URL        = f"{BASE_URL}/wp-json/wc/store/v1/products"
SNAPSHOT_FILE  = "snapshot_cherry.json"
BASELINE_FLAG  = "baseline_done_cherry.txt"
PER_PAGE       = 100
REQUEST_DELAY  = 1.5
RUN_ONCE       = os.getenv("RUN_ONCE", "false").lower() == "true"
CHECK_INTERVAL = int(os.getenv("CHECK_INTERVAL", "3600"))
DISCORD_WEBHOOK = os.getenv("DISCORD_WEBHOOK", "")

HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/124.0.0.0 Safari/537.36"
    ),
    "Accept": "application/json",
}

SESSION = requests.Session()
SESSION.headers.update(HEADERS)

COLOUR_NEW   = 0xE91E8C
COLOUR_BACK  = 0x9B59B6
COLOUR_STOCK = 0x3498DB
COLOUR_DROP  = 0x00C853

# ---------------------------------------------------------------------------
# API — WooCommerce Store API (no auth needed)
# ---------------------------------------------------------------------------

def api_get(page, retries=3):
    params = {"page": page, "per_page": PER_PAGE,
              "orderby": "date", "order": "desc"}
    for attempt in range(retries):
        try:
            r = SESSION.get(API_URL, params=params, timeout=20)
            if r.status_code == 429:
                wait = 20 * (attempt + 1)
                print(f"  [!] Rate limited — waiting {wait}s")
                time.sleep(wait)
                continue
            r.raise_for_status()
            total_pages = int(r.headers.get("X-WP-TotalPages", 1))
            total_items = int(r.headers.get("X-WP-Total", 0))
            return r.json(), total_pages, total_items
        except Exception as e:
            print(f"  [!] API error (page {page}): {e} — attempt {attempt+1}/{retries}")
            if attempt < retries - 1:
                time.sleep(5 * (attempt + 1))
    return [], 0, 0


def parse_product(item):
    """Parse WooCommerce Store API product. No page scraping needed."""
    def pence(val):
        try:
            return f"{int(val) / 100:.2f}"
        except (TypeError, ValueError):
            return ""

    prices   = item.get("prices", {})
    price    = pence(prices.get("price", ""))
    regular  = pence(prices.get("regular_price", ""))

    pack_price = regular if regular else price
    sale_price = price if (regular and price != regular) else ""

    name       = item.get("name", "")
    short_desc = item.get("short_description", "") or ""
    desc       = item.get("description", "") or ""
    full_text  = f"{name} {short_desc} {desc}"

    # Per unit from text
    pu_m     = re.search(r"£([\d.]+)\s*each", full_text, re.IGNORECASE)
    per_unit = pu_m.group(1) if pu_m else ""

    # Pack size from title
    ps_m      = re.search(r"[Xx]\s*(\d+)", name)
    pack_size = ps_m.group(1) if ps_m else "1"
    if not per_unit and pack_price and pack_size != "1":
        try:
            per_unit = f"{float(pack_price) / int(pack_size):.2f}"
        except (ValueError, ZeroDivisionError):
            pass

    # Stock — from API directly, no page scrape needed
    stock_status = item.get("stock_status", "instock")
    stock_qty    = item.get("stock_quantity")
    in_stock     = stock_status in ("instock", "onbackorder")

    # Barcode from description text
    ean_m = re.search(
        r"(?:EAN|Barcode|GTIN)[^\d]*([0-9]{8,14})",
        full_text, re.IGNORECASE
    )
    if not ean_m:
        ean_m = re.search(r"\b([0-9]{13})\b", full_text)
    barcode = ean_m.group(1) if ean_m else ""

    # Image
    images = item.get("images", [])
    image  = images[0].get("src", "") if images else ""

    # Brand from categories
    cats  = [c.get("name", "") for c in item.get("categories", [])]
    brand = ""
    known_brands = ["Rimmel","L'Oreal","Maybelline","Max Factor","NYX","Barry M",
                    "Essie","Revlon","Bourjois","Schwarzkopf","Garnier","Dove",
                    "Nivea","Sally Hansen","OPI"]
    for b in known_brands:
        if b.lower() in name.lower():
            brand = b
            break
    if not brand and cats:
        brand = cats[-1]

    return {
        "id":         str(item.get("id", "")),
        "slug":       item.get("slug", ""),
        "title":      name,
        "brand":      brand,
        "url":        item.get("permalink", f"{BASE_URL}/product/{item.get('slug', '')}/"),
        "image":      image,
        "barcode":    barcode,
        "sku":        item.get("sku", ""),
        "pack_size":  pack_size,
        "pack_price": pack_price,
        "sale_price": sale_price,
        "per_unit":   per_unit,
        "stock":      stock_qty,
        "in_stock":   in_stock,
        "categories": ", ".join(cats),
    }


def fetch_all_products():
    """Fetch all products from WooCommerce Store API."""
    items, total_pages, total_items = api_get(1)
    if not items:
        return []
    print(f"  {total_items} products across {total_pages} pages")
    all_products = [parse_product(i) for i in items]

    for page in range(2, total_pages + 1):
        time.sleep(REQUEST_DELAY + random.uniform(0, 0.5))
        items, _, _ = api_get(page)
        all_products.extend([parse_product(i) for i in items])
        print(f"  Page {page}/{total_pages}: total {len(all_products)}")

    return all_products


def scrape_barcode_from_page(url):
    """
    Scrape a product page ONLY to find the EAN/barcode.
    Called for new products or back-in-stock products with no cached barcode.
    NOT called on every run for every product.
    """
    try:
        r = SESSION.get(url, timeout=15)
        if not r.ok:
            return ""
        soup = BeautifulSoup(r.text, "html.parser")
        text = soup.get_text(" ", strip=True)

        # YITH barcode plugin image URL pattern
        for img in soup.find_all("img", src=re.compile(r"EAN13", re.IGNORECASE)):
            m = re.search(r"EAN13[_-](\d{8,14})", img.get("src", ""))
            if m:
                return m.group(1)

        # Text patterns
        for pat in [
            r"(?:EAN|Barcode|GTIN)[^\d]*([0-9]{8,14})",
            r"\b([0-9]{13})\b",
        ]:
            m = re.search(pat, text, re.IGNORECASE)
            if m:
                return m.group(1)
    except Exception:
        pass
    return ""

# ---------------------------------------------------------------------------
# HELPERS
# ---------------------------------------------------------------------------

def safe_float(v):
    try:
        return float(v)
    except (TypeError, ValueError):
        return None


def vat(price_str):
    f = safe_float(price_str)
    return f"{f * 1.2:.2f}" if f else price_str


def effective_price(product):
    return product.get("sale_price") or product.get("pack_price") or ""


def sas_ean(barcode, cost):
    if not barcode:
        return None
    return (f"https://sas.selleramp.com/sas/lookup/"
            f"?search_term={barcode}&sas_cost_price={vat(cost)}")


def sas_title(title, cost):
    return (f"https://sas.selleramp.com/sas/lookup/"
            f"?search_term={quote(title)}&sas_cost_price={vat(cost)}")

# ---------------------------------------------------------------------------
# DISCORD
# ---------------------------------------------------------------------------

def _send(payload):
    if not DISCORD_WEBHOOK:
        return
    try:
        r = requests.post(DISCORD_WEBHOOK, json=payload, timeout=10)
        if r.status_code == 429:
            wait = float(r.json().get("retry_after", 5)) + 0.5
            print(f"  [!] Discord rate limited — waiting {wait:.1f}s")
            time.sleep(wait)
            requests.post(DISCORD_WEBHOOK, json=payload, timeout=10)
        else:
            r.raise_for_status()
    except Exception as e:
        print(f"  [!] Discord error: {e}")


def _base_fields(product):
    barcode  = product.get("barcode", "")
    brand    = product.get("brand", "")
    sku      = product.get("sku", "")
    pack_sz  = product.get("pack_size", "")
    stock    = product.get("stock")
    in_stock = product.get("in_stock", True)
    cost     = effective_price(product)
    per_unit = product.get("per_unit", "")

    sas_cost = per_unit or cost   # prefer unit price for SAS

    stock_val = (f"{stock:,} units" if stock is not None
                 else ("✅ In stock" if in_stock else "❌ OOS"))

    fields = [
        {"name": "🏷️ Brand",        "value": brand or "-",                         "inline": True},
        {"name": "📦 Pack Size",     "value": f"{pack_sz} units" if pack_sz else "-","inline": True},
        {"name": "🔖 SKU",           "value": f"`{sku}`" if sku else "-",            "inline": True},
        {"name": "📊 Stock",         "value": stock_val,                             "inline": True},
        {"name": "🔢 GTIN / EAN",    "value": f"`{barcode}`" if barcode else "-",    "inline": True},
        {"name": "💷 inc-VAT",       "value": f"£{vat(per_unit)}" if per_unit else "-", "inline": True},
    ]

    ean_url   = sas_ean(barcode, sas_cost)
    title_url = sas_title(product.get("title",""), sas_cost)
    if ean_url:
        fields.append({"name": "🔍 SAS EAN",   "value": f"[Search by barcode]({ean_url})", "inline": True})
    fields.append(    {"name": "🔍 SAS Title", "value": f"[Search by title]({title_url})",  "inline": True})

    return fields


def _embed(title, url, colour, fields, product, footer_extra=""):
    embed = {
        "title":     title,
        "url":       url,
        "color":     colour,
        "fields":    fields,
        "timestamp": datetime.now(timezone.utc).isoformat(),
        "footer":    {"text": f"Cherry Cosmetics Monitor • cherrycosmetics.co.uk{footer_extra}"},
    }
    image = product.get("image", "")
    if image:
        embed["thumbnail"] = {"url": image}
    return embed


def notify_new(product):
    cost  = effective_price(product)
    pu    = product.get("per_unit", "")
    price_val = f"~~£{product['pack_price']}~~ → **£{product['sale_price']}**" \
                if product.get("sale_price") else f"**£{cost}**"
    fields = [
        {"name": "💰 New Price",       "value": price_val,                     "inline": True},
        {"name": "💷 Per Unit (ex-VAT)","value": f"£{pu}" if pu else "-",      "inline": True},
    ] + _base_fields(product)

    _send({"embeds": [_embed(
        f"🆕  NEW — {product['title']}",
        product["url"], COLOUR_NEW, fields, product
    )]})
    print(f"  ✅ NEW: {product['title'][:60]}")


def notify_back_in_stock(product):
    pu    = product.get("per_unit", "")
    cost  = effective_price(product)
    fields = [
        {"name": "💰 New Price",        "value": f"**£{cost}**",               "inline": True},
        {"name": "💷 Per Unit (ex-VAT)","value": f"£{pu}" if pu else "-",      "inline": True},
    ] + _base_fields(product)

    _send({"embeds": [_embed(
        f"🟢  BACK IN STOCK — {product['title']}",
        product["url"], COLOUR_BACK, fields, product
    )]})
    print(f"  ✅ BACK IN STOCK: {product['title'][:55]}")


def notify_restock(product, old_stock, new_stock):
    diff  = new_stock - old_stock if (new_stock and old_stock) else "?"
    cost  = effective_price(product)
    pu    = product.get("per_unit", "")
    fields = [
        {"name": "📦 Was",   "value": f"{old_stock:,} units",            "inline": True},
        {"name": "📦 Now",   "value": f"**{new_stock:,} units**",        "inline": True},
        {"name": "📈 Added", "value": f"+{diff:,}" if isinstance(diff, int) else "?", "inline": True},
        {"name": "💰 Price", "value": f"£{cost}",                        "inline": True},
        {"name": "💷 Per Unit (ex-VAT)","value": f"£{pu}" if pu else "-","inline": True},
    ] + _base_fields(product)

    _send({"embeds": [_embed(
        f"📦  RESTOCK — {product['title']}",
        product["url"], COLOUR_STOCK, fields, product
    )]})
    print(f"  ✅ RESTOCK: {product['title'][:55]}")


def notify_price_drop(product, old_price, new_price, pct):
    pct_str  = f"{pct*100:.1f}%"
    abs_drop = safe_float(old_price) - safe_float(new_price)
    pu       = product.get("per_unit", "")
    icon     = "🔥" if pct >= 0.20 else ("💰" if pct >= 0.10 else "💵")
    colour   = 0x00C853 if pct >= 0.20 else (0x2ECC71 if pct >= 0.10 else 0x82E0AA)

    fields = [
        {"name": "💰 Was",              "value": f"£{old_price}",                     "inline": True},
        {"name": "💰 Now",              "value": f"**£{new_price}**",                 "inline": True},
        {"name": "📉 Drop",             "value": f"↓ £{abs_drop:.2f} (-{pct_str})",  "inline": True},
        {"name": "💷 Per Unit (ex-VAT)","value": f"£{pu}" if pu else "-",            "inline": True},
    ] + _base_fields(product)

    _send({"embeds": [_embed(
        f"{icon}  PRICE DROP -{pct_str} — {product['title']}",
        product["url"], colour, fields, product,
        footer_extra=f" • was £{old_price}"
    )]})
    print(f"  ✅ PRICE DROP -{pct_str}: {product['title'][:45]}")

# ---------------------------------------------------------------------------
# SNAPSHOT
# ---------------------------------------------------------------------------

def load_snapshot():
    if os.path.exists(SNAPSHOT_FILE):
        try:
            with open(SNAPSHOT_FILE) as f:
                return json.load(f)
        except json.JSONDecodeError:
            bak = f"{SNAPSHOT_FILE}.bak.{int(time.time())}"
            print(f"  [!] Snapshot corrupted — backed up to {bak}")
            try:
                os.rename(SNAPSHOT_FILE, bak)
            except OSError:
                pass
    return {}


def save_snapshot(data):
    tmp = SNAPSHOT_FILE + ".tmp"
    with open(tmp, "w") as f:
        json.dump(data, f, indent=2)
    os.replace(tmp, SNAPSHOT_FILE)


def to_entry(product):
    return {
        "title":      product.get("title", ""),
        "url":        product.get("url", ""),
        "image":      product.get("image", ""),
        "barcode":    product.get("barcode", ""),
        "brand":      product.get("brand", ""),
        "sku":        product.get("sku", ""),
        "pack_size":  product.get("pack_size", ""),
        "pack_price": product.get("pack_price", ""),
        "sale_price": product.get("sale_price", ""),
        "per_unit":   product.get("per_unit", ""),
        "stock":      product.get("stock"),
        "in_stock":   product.get("in_stock", True),
        "categories": product.get("categories", ""),
        "first_seen": product.get("first_seen", datetime.now(timezone.utc).isoformat()),
        "last_updated": datetime.now(timezone.utc).isoformat(),
    }

# ---------------------------------------------------------------------------
# MAIN CHECK
# ---------------------------------------------------------------------------

def run_check():
    now_str = datetime.now(timezone.utc).strftime("%H:%M:%S UTC")
    print(f"\n[{now_str}] Checking Cherry Cosmetics...")

    snapshot      = load_snapshot()
    known_ids     = set(snapshot.keys())
    baseline_done = os.path.exists(BASELINE_FLAG)
    is_first_run  = not baseline_done

    # Fetch all products from WooCommerce API (fast — no page scrapes)
    all_products = fetch_all_products()
    if not all_products:
        print("  [!] No products returned — skipping")
        return

    current_ids = {p["id"] for p in all_products}
    new_ids     = current_ids - known_ids
    gone_ids    = known_ids - current_ids

    print(f"  {len(all_products)} products | {len(new_ids)} new | {len(gone_ids)} gone")

    if is_first_run:
        print(f"  First run — building baseline. No alerts.")

    alerts_sent = 0

    for product in all_products:
        pid      = product["id"]
        is_new   = pid in new_ids
        old      = snapshot.get(pid, {})

        was_in_stock = old.get("in_stock", True)
        now_in_stock = product.get("in_stock", True)
        is_back      = not was_in_stock and now_in_stock and not is_new

        # Scrape product page for barcode ONLY when needed:
        # - New product AND barcode not in API response
        # - Back in stock AND no barcode in snapshot
        needs_barcode_scrape = (
            (is_new and not is_first_run and not product.get("barcode")) or
            (is_back and not old.get("barcode") and not product.get("barcode"))
        )
        if needs_barcode_scrape:
            time.sleep(REQUEST_DELAY)
            scraped = scrape_barcode_from_page(product["url"])
            if scraped:
                product["barcode"] = scraped

        # Carry forward cached fields for existing products
        for key in ("barcode", "brand", "image", "sku", "categories"):
            if not product.get(key):
                product[key] = old.get(key, "")

        if is_first_run:
            entry = to_entry(product)
            entry["first_seen"] = datetime.now(timezone.utc).isoformat()
            snapshot[pid] = entry
            continue

        # --- ALERTS ---

        # New product
        if is_new:
            if now_in_stock:
                notify_new(product)
                alerts_sent += 1
                time.sleep(1.5)
            entry = to_entry(product)
            entry["first_seen"] = datetime.now(timezone.utc).isoformat()
            snapshot[pid] = entry
            continue

        # Back in stock
        if is_back:
            notify_back_in_stock(product)
            alerts_sent += 1
            time.sleep(1.5)

        # Price drop (only if still in stock)
        elif now_in_stock:
            old_price = old.get("sale_price") or old.get("pack_price") or ""
            new_price = product.get("sale_price") or product.get("pack_price") or ""
            old_f     = safe_float(old_price)
            new_f     = safe_float(new_price)
            if old_f and new_f and old_f > 0:
                pct = (old_f - new_f) / old_f
                if pct > 0.01 and (old_f - new_f) > 0.02:
                    notify_price_drop(product, old_price, new_price, pct)
                    alerts_sent += 1
                    time.sleep(1.5)

            # Restock (stock went up meaningfully)
            old_stock = old.get("stock")
            new_stock = product.get("stock")
            if (old_stock is not None and new_stock is not None
                    and new_stock > old_stock + max(5, int(old_stock * 0.2))):
                notify_restock(product, old_stock, new_stock)
                alerts_sent += 1
                time.sleep(1.5)

        # Update snapshot
        entry = to_entry(product)
        entry["first_seen"] = old.get("first_seen", entry["first_seen"])
        snapshot[pid] = entry

    # Mark gone products as OOS
    for pid in gone_ids:
        if pid in snapshot:
            snapshot[pid]["in_stock"] = False
            snapshot[pid]["last_updated"] = datetime.now(timezone.utc).isoformat()

    save_snapshot(snapshot)

    if is_first_run:
        with open(BASELINE_FLAG, "w") as f:
            f.write(datetime.now(timezone.utc).isoformat())
        print(f"  Baseline saved — {len(snapshot)} products tracked. Monitoring begins next cycle.")
    else:
        print(f"  Done — {alerts_sent} alert(s) | {len(snapshot)} products tracked.")

# ---------------------------------------------------------------------------
# ENTRY POINT
# ---------------------------------------------------------------------------

def main():
    print("=" * 55)
    print("  Cherry Cosmetics Monitor")
    print(f"  API: {API_URL}")
    print("  Alerts: new listings | back in stock | price drops | restocks")
    print("=" * 55)

    if not DISCORD_WEBHOOK:
        print("  ⚠️  DISCORD_WEBHOOK not set")

    if RUN_ONCE:
        run_check()
        return

    while True:
        try:
            run_check()
        except Exception as e:
            print(f"  [!] Unexpected error: {e}")
        print(f"  Sleeping {CHECK_INTERVAL}s...")
        time.sleep(CHECK_INTERVAL)


if __name__ == "__main__":
    main()
