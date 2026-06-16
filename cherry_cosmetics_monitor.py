"""
Cherry Cosmetics Monitor
Uses the WooCommerce Store API (no auth needed) for clean paginated JSON.

API: https://www.cherrycosmetics.co.uk/wp-json/wc/store/v1/products

Detects and alerts on Discord for:
  - New product listings
  - Price drops / increases
  - Stock changes (restock, drop, OOS, back in stock)

Deps:  pip install requests
"""

import json
import os
import re
import time
import random
import requests
from datetime import datetime, timezone

# ---------------------------------------------------------------------------
# CONFIG
# ---------------------------------------------------------------------------

BASE_URL       = "https://www.cherrycosmetics.co.uk"
API_URL        = f"{BASE_URL}/wp-json/wc/store/v1/products"
SNAPSHOT_FILE  = "snapshot_cherry.json"
PER_PAGE       = 100        # max per page for the store API
REQUEST_DELAY  = 1.5        # seconds between API calls
RUN_ONCE       = os.getenv("RUN_ONCE", "false").lower() == "true"
CHECK_INTERVAL = int(os.getenv("CHECK_INTERVAL", "3600"))  # 1 hour

DISCORD_WEBHOOK = os.getenv(
    "DISCORD_WEBHOOK",
    "https://discord.com/api/webhooks/1516406785177817289/lJRkTNmGN7iNR4fq8eCqvuc68poiC7-nbs5NOK3mGRdxYYNvmrw_YMlzThHmpuP9dkBd"
)

HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/124.0.0.0 Safari/537.36"
    ),
    "Accept": "application/json",
}

# Discord embed colours
COLOUR_NEW        = 0xE91E8C
COLOUR_PRICE_DROP = 0x2ECC71
COLOUR_PRICE_UP   = 0xE74C3C
COLOUR_RESTOCK    = 0x3498DB
COLOUR_LOW_STOCK  = 0xF39C12
COLOUR_OOS        = 0x95A5A6
COLOUR_BACK       = 0x9B59B6

# ---------------------------------------------------------------------------
# API HELPERS
# ---------------------------------------------------------------------------

SESSION = requests.Session()
SESSION.headers.update(HEADERS)


def api_get(page, orderby="date", order="desc", retries=3):
    """Fetch one page of products from the WooCommerce Store API."""
    params = {
        "page":     page,
        "per_page": PER_PAGE,
        "orderby":  orderby,
        "order":    order,
    }
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
    """Extract the fields we care about from a Store API product object."""
    # Price — API returns prices in pence as strings e.g. "1400"
    def pence_to_pounds(val):
        try:
            return f"{int(val) / 100:.2f}"
        except (TypeError, ValueError):
            return ""

    prices      = item.get("prices", {})
    raw_price   = prices.get("price", "")
    raw_regular = prices.get("regular_price", "")

    price   = pence_to_pounds(raw_price)
    regular = pence_to_pounds(raw_regular)

    # If price < regular_price it's on sale
    pack_price = regular if regular else price
    sale_price = price if (regular and price != regular) else ""

    # Per unit — shown in short description or name as "£X each"
    short_desc = item.get("short_description", "") or ""
    name       = item.get("name", "")
    full_text  = f"{name} {short_desc}"

    pu_m = re.search(r"£([\d.]+)\s*each", full_text)
    per_unit = pu_m.group(1) if pu_m else ""

    # If no per_unit from text, try to derive from pack size
    ps_m = re.search(r"[Xx]\s*(\d+)", name)
    pack_size = ps_m.group(1) if ps_m else "1"
    if not per_unit and pack_price and pack_size != "1":
        try:
            per_unit = f"{float(pack_price) / int(pack_size):.2f}"
        except (ValueError, ZeroDivisionError):
            pass

    # Stock
    stock_status = item.get("stock_status", "")
    stock_qty    = item.get("stock_quantity")   # may be None if not tracked
    in_stock     = stock_status == "instock"

    # Barcode / EAN — in description or short_description
    desc = item.get("description", "") or ""
    ean_m = re.search(r"(?:EAN|Barcode)[^\d]*([0-9]{8,14})", f"{short_desc} {desc}", re.IGNORECASE)
    if not ean_m:
        ean_m = re.search(r"\b([0-9]{13})\b", f"{short_desc} {desc}")
    barcode = ean_m.group(1) if ean_m else ""

    # Image
    images = item.get("images", [])
    image  = images[0].get("src", "") if images else ""

    # Categories
    cats = [c.get("name", "") for c in item.get("categories", [])]

    return {
        "id":         str(item.get("id", "")),
        "slug":       item.get("slug", ""),
        "title":      name,
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


def fetch_all_products(orderby="date", order="desc"):
    """Fetch every product from the API. Returns list of parsed product dicts."""
    print(f"  Fetching page 1...")
    items, total_pages, total_items = api_get(1, orderby=orderby, order=order)
    if not items:
        return []

    print(f"  {total_items} total products across {total_pages} pages")
    all_products = [parse_product(i) for i in items]

    for page in range(2, total_pages + 1):
        time.sleep(REQUEST_DELAY + random.uniform(0, 1))
        print(f"  Fetching page {page}/{total_pages}...")
        items, _, _ = api_get(page, orderby=orderby, order=order)
        all_products.extend([parse_product(i) for i in items])

    return all_products


def fetch_recent_products(pages=1):
    """Fetch just the most recent pages (for incremental new arrival checks)."""
    all_products = []
    for page in range(1, pages + 1):
        items, total_pages, _ = api_get(page, orderby="date", order="desc")
        all_products.extend([parse_product(i) for i in items])
        if page >= total_pages:
            break
        time.sleep(REQUEST_DELAY)
    return all_products


# ---------------------------------------------------------------------------
# PRICING HELPERS
# ---------------------------------------------------------------------------

def effective_price(product):
    return product.get("sale_price") or product.get("pack_price") or "0"


def vat_price(price_str):
    try:
        return f"{float(price_str) * 1.2:.2f}"
    except (ValueError, TypeError):
        return price_str


def selleramp_url(barcode, cost_price_str):
    if not barcode:
        return None
    return (
        f"https://sas.selleramp.com/sas/lookup/"
        f"?search_term={barcode}&sas_cost_price={vat_price(cost_price_str)}"
    )


def safe_float(val):
    try:
        return float(val)
    except (TypeError, ValueError):
        return None


# ---------------------------------------------------------------------------
# DISCORD EMBEDS
# ---------------------------------------------------------------------------

def _base_fields(product):
    barcode   = product.get("barcode", "")
    sku       = product.get("sku", "")
    pack_size = product.get("pack_size", "?")
    per_unit  = product.get("per_unit", "")
    stock     = product.get("stock")
    in_stock  = product.get("in_stock", True)
    cats      = product.get("categories", "")
    sas_url   = selleramp_url(barcode, per_unit or effective_price(product))

    stock_val = f"**{stock} units**" if stock is not None else ("In stock" if in_stock else "Out of stock")

    fields = [
        {"name": "📦 Pack Size",     "value": f"{pack_size} units",              "inline": True},
        {"name": "🔢 Barcode / EAN", "value": f"`{barcode}`" if barcode else "-", "inline": True},
        {"name": "🔖 SKU",           "value": f"`{sku}`" if sku else "-",         "inline": True},
        {"name": "📊 Stock",         "value": stock_val,                          "inline": True},
    ]
    if cats:
        fields.append({"name": "🏷️ Category", "value": cats, "inline": True})
    if sas_url:
        fields.append({"name": "🔍 SellerAmp SAS", "value": f"[Open in SellerAmp]({sas_url})", "inline": False})
    return fields


def _send_embed(embed):
    payload = {"embeds": [embed]}
    try:
        r = requests.post(DISCORD_WEBHOOK, json=payload, timeout=10)
        if r.status_code == 429:
            wait = float(r.json().get("retry_after", 5)) + 0.5
            time.sleep(wait)
            requests.post(DISCORD_WEBHOOK, json=payload, timeout=10)
        else:
            r.raise_for_status()
    except Exception as e:
        print(f"  [!] Discord error: {e}")


def _thumbnail(product):
    image = product.get("image", "")
    return {"url": image} if image else None


def _price_display(product):
    pack_price = product.get("pack_price", "")
    sale_price = product.get("sale_price", "")
    if sale_price:
        return f"£{pack_price} -> **£{sale_price}**"
    return f"**£{pack_price}**" if pack_price else "-"


def notify_new(product):
    per_unit = product.get("per_unit", "")
    fields = [
        {"name": "💰 Pack Price (ex. VAT)", "value": _price_display(product),                        "inline": True},
        {"name": "💷 Per Unit (ex. VAT)",   "value": f"£{per_unit}" if per_unit else "-",            "inline": True},
        {"name": "💷 Per Unit (inc. VAT)",  "value": f"£{vat_price(per_unit)}" if per_unit else "-", "inline": True},
    ] + _base_fields(product)

    embed = {
        "title":     f"🆕  NEW LISTING — {product.get('title', '')}",
        "url":       product.get("url", BASE_URL),
        "color":     COLOUR_NEW,
        "fields":    fields,
        "timestamp": datetime.now(timezone.utc).isoformat(),
        "footer":    {"text": "Cherry Cosmetics Monitor • cherrycosmetics.co.uk"},
    }
    t = _thumbnail(product)
    if t: embed["thumbnail"] = t
    _send_embed(embed)
    print(f"  Discord: NEW — {product.get('title', '')[:60]}")


def notify_price_change(product, old_price, new_price, is_drop):
    old_f = safe_float(old_price)
    new_f = safe_float(new_price)
    diff  = f"£{abs(new_f - old_f):.2f}" if old_f and new_f else "?"
    pct   = f"{abs((new_f - old_f) / old_f * 100):.1f}%" if old_f and new_f else "?"
    per_unit = product.get("per_unit", "")

    fields = [
        {"name": "💰 Old Price",           "value": f"£{old_price}",                                    "inline": True},
        {"name": "💰 New Price",           "value": f"**£{new_price}**",                                "inline": True},
        {"name": "📉 Change",              "value": f"{'↓' if is_drop else '↑'} {diff} ({pct})",        "inline": True},
        {"name": "💷 Per Unit (ex. VAT)",  "value": f"£{per_unit}" if per_unit else "-",                "inline": True},
        {"name": "💷 Per Unit (inc. VAT)", "value": f"£{vat_price(per_unit)}" if per_unit else "-",     "inline": True},
    ] + _base_fields(product)

    embed = {
        "title":     f"{'💰  PRICE DROP' if is_drop else '📈  PRICE INCREASE'} — {product.get('title', '')}",
        "url":       product.get("url", BASE_URL),
        "color":     COLOUR_PRICE_DROP if is_drop else COLOUR_PRICE_UP,
        "fields":    fields,
        "timestamp": datetime.now(timezone.utc).isoformat(),
        "footer":    {"text": "Cherry Cosmetics Monitor • cherrycosmetics.co.uk"},
    }
    t = _thumbnail(product)
    if t: embed["thumbnail"] = t
    _send_embed(embed)
    print(f"  Discord: PRICE {'DROP' if is_drop else 'UP'} — {product.get('title', '')[:50]}")


def notify_stock_change(product, old_stock, new_stock, is_restock):
    diff = abs(new_stock - old_stock) if (new_stock is not None and old_stock is not None) else "?"
    fields = [
        {"name": "📊 Old Stock", "value": f"{old_stock} units",     "inline": True},
        {"name": "📊 New Stock", "value": f"**{new_stock} units**", "inline": True},
        {"name": "📉 Change",    "value": f"{'↑ +' if is_restock else '↓ -'}{diff} units", "inline": True},
    ] + _base_fields(product)

    embed = {
        "title":     f"{'🟢  RESTOCK' if is_restock else '📉  STOCK DROP'} — {product.get('title', '')}",
        "url":       product.get("url", BASE_URL),
        "color":     COLOUR_RESTOCK if is_restock else COLOUR_LOW_STOCK,
        "fields":    fields,
        "timestamp": datetime.now(timezone.utc).isoformat(),
        "footer":    {"text": "Cherry Cosmetics Monitor • cherrycosmetics.co.uk"},
    }
    t = _thumbnail(product)
    if t: embed["thumbnail"] = t
    _send_embed(embed)
    print(f"  Discord: {'RESTOCK' if is_restock else 'STOCK DROP'} — {product.get('title', '')[:50]}")


def notify_oos(product):
    embed = {
        "title":     f"🔴  OUT OF STOCK — {product.get('title', '')}",
        "url":       product.get("url", BASE_URL),
        "color":     COLOUR_OOS,
        "fields":    _base_fields(product),
        "timestamp": datetime.now(timezone.utc).isoformat(),
        "footer":    {"text": "Cherry Cosmetics Monitor • cherrycosmetics.co.uk"},
    }
    t = _thumbnail(product)
    if t: embed["thumbnail"] = t
    _send_embed(embed)
    print(f"  Discord: OOS — {product.get('title', '')[:60]}")


def notify_back_in_stock(product):
    per_unit = product.get("per_unit", "")
    fields = [
        {"name": "💷 Per Unit (ex. VAT)",  "value": f"£{per_unit}" if per_unit else "-",            "inline": True},
        {"name": "💷 Per Unit (inc. VAT)", "value": f"£{vat_price(per_unit)}" if per_unit else "-", "inline": True},
    ] + _base_fields(product)

    embed = {
        "title":     f"🟢  BACK IN STOCK — {product.get('title', '')}",
        "url":       product.get("url", BASE_URL),
        "color":     COLOUR_BACK,
        "fields":    fields,
        "timestamp": datetime.now(timezone.utc).isoformat(),
        "footer":    {"text": "Cherry Cosmetics Monitor • cherrycosmetics.co.uk"},
    }
    t = _thumbnail(product)
    if t: embed["thumbnail"] = t
    _send_embed(embed)
    print(f"  Discord: BACK IN STOCK — {product.get('title', '')[:60]}")


# ---------------------------------------------------------------------------
# SNAPSHOT
# ---------------------------------------------------------------------------

def load_snapshot():
    if os.path.exists(SNAPSHOT_FILE):
        with open(SNAPSHOT_FILE) as f:
            return json.load(f)
    return {}


def save_snapshot(data):
    with open(SNAPSHOT_FILE, "w") as f:
        json.dump(data, f, indent=2)


def snapshot_entry(product):
    return {
        "title":        product.get("title", ""),
        "url":          product.get("url", ""),
        "image":        product.get("image", ""),
        "barcode":      product.get("barcode", ""),
        "sku":          product.get("sku", ""),
        "pack_size":    product.get("pack_size", ""),
        "pack_price":   product.get("pack_price", ""),
        "sale_price":   product.get("sale_price", ""),
        "per_unit":     product.get("per_unit", ""),
        "stock":        product.get("stock"),
        "in_stock":     product.get("in_stock", True),
        "categories":   product.get("categories", ""),
        "first_seen":   product.get("first_seen", datetime.now(timezone.utc).isoformat()),
        "last_updated": datetime.now(timezone.utc).isoformat(),
    }


# ---------------------------------------------------------------------------
# CHANGE DETECTION
# ---------------------------------------------------------------------------

def check_changes(product, old):
    old_price    = old.get("sale_price") or old.get("pack_price") or ""
    new_price    = product.get("sale_price") or product.get("pack_price") or ""
    old_stock    = old.get("stock")
    new_stock    = product.get("stock")
    was_in_stock = old.get("in_stock", True)
    now_in_stock = product.get("in_stock", True)

    # Fill cached fields if API didn't return them
    for key in ("image", "barcode", "sku", "pack_size", "categories"):
        if not product.get(key):
            product[key] = old.get(key, "")

    old_f = safe_float(old_price)
    new_f = safe_float(new_price)

    if not was_in_stock and now_in_stock:
        notify_back_in_stock(product)
        time.sleep(1)
    elif was_in_stock and not now_in_stock:
        notify_oos(product)
        time.sleep(1)
    elif old_f and new_f and new_f < old_f - 0.01:
        notify_price_change(product, old_price, new_price, is_drop=True)
        time.sleep(1)
    elif old_f and new_f and new_f > old_f + 0.01:
        notify_price_change(product, old_price, new_price, is_drop=False)
        time.sleep(1)

    if old_stock is not None and new_stock is not None and now_in_stock:
        if new_stock > old_stock + 5:
            notify_stock_change(product, old_stock, new_stock, is_restock=True)
            time.sleep(1)
        elif new_stock < old_stock - 5:
            notify_stock_change(product, old_stock, new_stock, is_restock=False)
            time.sleep(1)


# ---------------------------------------------------------------------------
# MAIN
# ---------------------------------------------------------------------------

def run_check():
    print(f"\n[{datetime.now(timezone.utc).strftime('%H:%M:%S UTC')}] Checking Cherry Cosmetics...")

    snapshot     = load_snapshot()
    known_ids    = set(snapshot.keys())
    is_first_run = len(known_ids) == 0

    if is_first_run:
        # ----------------------------------------------------------------
        # FIRST RUN: pull entire store via API, build baseline — no alerts
        # ----------------------------------------------------------------
        print("  First run — fetching entire store as baseline (no alerts will fire)...")
        all_products = fetch_all_products(orderby="date", order="desc")
        print(f"  {len(all_products)} products fetched")

        for i, product in enumerate(all_products, 1):
            pid = product["id"]
            entry = snapshot_entry(product)
            entry["first_seen"] = datetime.now(timezone.utc).isoformat()
            snapshot[pid] = entry

            if i % 100 == 0:
                save_snapshot(snapshot)
                print(f"  Auto-saved at {i}/{len(all_products)} products")

        save_snapshot(snapshot)
        print(f"  Baseline complete — {len(snapshot)} products recorded. No Discord alerts sent.")

    else:
        # ----------------------------------------------------------------
        # SUBSEQUENT RUNS: full store re-fetch and diff against snapshot
        # ----------------------------------------------------------------
        print(f"  Fetching full store to check for changes...")
        all_products = fetch_all_products(orderby="date", order="desc")
        if not all_products:
            print("  [!] No products returned from API")
            return

        current_ids = {p["id"] for p in all_products}
        new_ids     = current_ids - known_ids
        print(f"  {len(all_products)} products fetched, {len(new_ids)} new")

        for product in all_products:
            pid = product["id"]

            if pid in new_ids:
                print(f"  -> NEW: {product['title'][:60]}")
                notify_new(product)
                time.sleep(1.5)
                entry = snapshot_entry(product)
                entry["first_seen"] = datetime.now(timezone.utc).isoformat()
                snapshot[pid] = entry
            else:
                old = snapshot[pid]
                check_changes(product, old)
                entry = snapshot_entry(product)
                entry["first_seen"] = old.get("first_seen", entry["first_seen"])
                snapshot[pid] = entry

        save_snapshot(snapshot)
        print(f"  Snapshot saved ({len(snapshot)} products tracked)")


def main():
    print("=" * 55)
    print("  Cherry Cosmetics Monitor (WooCommerce Store API)")
    print(f"  Watching: {API_URL}")
    print("  Tracking: new listings, price & stock changes")
    print("=" * 55)

    if RUN_ONCE:
        run_check()
    else:
        while True:
            try:
                run_check()
            except Exception as e:
                print(f"  [!] Unexpected error: {e}")
            print(f"  Sleeping {CHECK_INTERVAL}s...")
            time.sleep(CHECK_INTERVAL)


if __name__ == "__main__":
    main()
