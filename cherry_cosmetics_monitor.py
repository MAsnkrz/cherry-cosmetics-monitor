"""
Cherry Cosmetics Monitor
Monitors https://www.cherrycosmetics.co.uk/shop/?orderby=date

Detects and alerts on Discord for:
  - New product listings
  - Price drops / increases
  - Stock changes (restock, drop, OOS, back in stock)

Deps:  pip install playwright requests beautifulsoup4
       python -m playwright install chromium --with-deps
"""

import json
import os
import re
import time
import random
import requests
from datetime import datetime, timezone
from bs4 import BeautifulSoup
from playwright.sync_api import sync_playwright, TimeoutError as PWTimeout

# ---------------------------------------------------------------------------
# CONFIG
# ---------------------------------------------------------------------------

BASE_URL       = "https://www.cherrycosmetics.co.uk"
SHOP_URL       = f"{BASE_URL}/shop/"
SNAPSHOT_FILE  = "snapshot_cherry.json"
REQUEST_DELAY  = 3.0
RUN_ONCE       = os.getenv("RUN_ONCE", "false").lower() == "true"
CHECK_INTERVAL = int(os.getenv("CHECK_INTERVAL", "300"))
HEADLESS       = os.getenv("HEADLESS", "true").lower() == "true"

DISCORD_WEBHOOK = os.getenv(
    "DISCORD_WEBHOOK",
    "YOUR_DISCORD_WEBHOOK_HERE"
)

# Discord embed colours
COLOUR_NEW        = 0xE91E8C
COLOUR_PRICE_DROP = 0x2ECC71
COLOUR_PRICE_UP   = 0xE74C3C
COLOUR_RESTOCK    = 0x3498DB
COLOUR_LOW_STOCK  = 0xF39C12
COLOUR_OOS        = 0x95A5A6
COLOUR_BACK       = 0x9B59B6

# ---------------------------------------------------------------------------
# BROWSER
# ---------------------------------------------------------------------------

def make_browser(playwright):
    browser = playwright.chromium.launch(headless=HEADLESS)
    context = browser.new_context(
        user_agent=(
            "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
            "AppleWebKit/537.36 (KHTML, like Gecko) "
            "Chrome/124.0.0.0 Safari/537.36"
        ),
        locale="en-GB",
        viewport={"width": 1280, "height": 800},
    )
    return browser, context


def fetch_html(context, url, wait_selector=None, timeout=20000, retries=3):
    for attempt in range(retries):
        page = context.new_page()
        try:
            page.goto(url, timeout=timeout, wait_until="domcontentloaded")
            if wait_selector:
                try:
                    page.wait_for_selector(wait_selector, timeout=8000)
                except PWTimeout:
                    pass
            html = page.content()
            title = page.title()
            if "too many requests" in title.lower() or "403" in title or "429" in title:
                wait_secs = 15 * (attempt + 1)
                print(f"  [!] Rate limited (attempt {attempt+1}/{retries}) — waiting {wait_secs}s")
                page.close()
                time.sleep(wait_secs)
                continue
            return html
        except Exception as e:
            print(f"  [!] Fetch error ({url}): {e} — attempt {attempt+1}/{retries}")
            if attempt < retries - 1:
                time.sleep(5 * (attempt + 1))
        finally:
            try:
                page.close()
            except Exception:
                pass
    return None

# ---------------------------------------------------------------------------
# SCRAPING — LISTING
# ---------------------------------------------------------------------------

def scrape_listing_page(context, page_num):
    url = f"{SHOP_URL}page/{page_num}/?orderby=date&per_page=30"
    html = fetch_html(context, url, wait_selector="a[href*='/product/']")
    if not html:
        return [], False

    soup = BeautifulSoup(html, "html.parser")
    products = []
    seen = set()

    for a in soup.find_all("a", href=re.compile(r"/product/[^/?#]+/?")):
        href = a["href"]
        if any(x in href for x in ["add-to-cart", "wishlist", "compare", "#"]):
            continue
        m = re.search(r"/product/([^/?#]+)/?", href)
        if not m:
            continue
        slug = m.group(1)
        if slug in seen:
            continue
        seen.add(slug)

        title = a.get_text(strip=True)
        card  = a.find_parent("div") or a.find_parent("li")
        pack_price = sale_price = per_unit = ""

        if card:
            ct = card.get_text(" ", strip=True)
            orig = re.search(r"£([\d.]+)\s*Original price", ct)
            sale = re.search(r"£([\d.]+)Current price", ct)
            if orig and sale:
                pack_price = orig.group(1)
                sale_price = sale.group(1)
            else:
                pp = re.search(r"£([\d.]+)\s*Excl\. VAT", ct)
                if pp: pack_price = pp.group(1)
            pu = re.search(r"£([\d.]+)\s*each", ct)
            if pu: per_unit = pu.group(1)

        products.append({
            "slug":       slug,
            "title":      title,
            "url":        f"{BASE_URL}/product/{slug}/",
            "pack_price": pack_price,
            "sale_price": sale_price,
            "per_unit":   per_unit,
        })

    has_next = bool(soup.find("a", class_=re.compile(r"next")))
    return products, has_next


def scrape_all_products(context):
    """Paginate through the ENTIRE store (for initial snapshot). Returns all products."""
    all_products = []
    page_num = 1
    while True:
        print(f"  Fetching shop page {page_num}...")
        products, has_next = scrape_listing_page(context, page_num)
        all_products.extend(products)
        print(f"    {len(products)} products found (total so far: {len(all_products)})")
        if not has_next or not products:
            break
        page_num += 1
        time.sleep(REQUEST_DELAY + random.uniform(1, 3))
    return all_products


def scrape_new_arrivals(context, max_pages=3):
    """Scrape only the first N pages sorted by date (for incremental checks)."""
    all_products = []
    for page_num in range(1, max_pages + 1):
        print(f"  Fetching shop page {page_num} (newest first)...")
        products, has_next = scrape_listing_page(context, page_num)
        all_products.extend(products)
        if not has_next:
            break
        time.sleep(REQUEST_DELAY + random.uniform(0, 2))
    return all_products

# ---------------------------------------------------------------------------
# SCRAPING — PRODUCT DETAIL
# ---------------------------------------------------------------------------

def scrape_product_detail(context, slug, existing=None):
    url = f"{BASE_URL}/product/{slug}/"
    html = fetch_html(context, url, wait_selector=".summary", timeout=15000)
    if not html:
        return existing or {"slug": slug, "url": url}

    product = (existing or {}).copy()
    product["slug"] = slug
    product["url"]  = url

    soup = BeautifulSoup(html, "html.parser")

    # Title
    h1 = soup.find("h1")
    if h1:
        product["title"] = h1.get_text(strip=True)

    # Image from og:image
    og_img = soup.find("meta", property="og:image")
    product["image"] = og_img["content"] if og_img else ""

    # Restrict to product section (before Related products)
    text    = soup.get_text(" ", strip=True)
    rel_idx = text.find("Related products")
    section = text[:rel_idx] if rel_idx > 0 else text

    # EAN barcode
    ean_m = re.search(r"EAN\s*(?:Code)?\s*([0-9]{8,14})", section)
    if not ean_m:
        ean_m = re.search(r"\b([0-9]{13})\b", section)
    product["barcode"] = ean_m.group(1) if ean_m else ""

    # SKU
    sku_m = re.search(r"SKU:\s*([A-Z0-9]+)", section)
    product["sku"] = sku_m.group(1) if sku_m else ""

    # Stock
    stock_m = re.search(r"(\d+)\s+in\s+stock", section)
    if stock_m:
        product["stock"]    = int(stock_m.group(1))
        product["in_stock"] = True
    elif "out of stock" in section.lower():
        product["stock"]    = 0
        product["in_stock"] = False
    else:
        product.setdefault("stock",    None)
        product.setdefault("in_stock", True)

    # Prices from WooCommerce price box
    price_box = soup.find("p", class_=re.compile(r"price"))
    if price_box:
        del_tag = price_box.find("del")
        ins_tag = price_box.find("ins")
        if del_tag and ins_tag:
            orig = re.search(r"£([\d.]+)", del_tag.get_text())
            sale = re.search(r"£([\d.]+)", ins_tag.get_text())
            if orig: product["pack_price"] = orig.group(1)
            if sale: product["sale_price"] = sale.group(1)
        else:
            pp = re.search(r"£([\d.]+)", price_box.get_text())
            if pp:
                product["pack_price"] = pp.group(1)
                product["sale_price"] = ""
    
    # Per unit
    pu_m = re.search(r"£([\d.]+)\s*each", section)
    if pu_m:
        product["per_unit"] = pu_m.group(1)

    # Pack size from title e.g. "X 3" or "X3"
    ps_m = re.search(r"[Xx]\s*(\d+)", product.get("title", ""))
    product["pack_size"] = ps_m.group(1) if ps_m else "1"

    return product

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
# DISCORD
# ---------------------------------------------------------------------------

def _base_fields(product):
    barcode  = product.get("barcode", "")
    sku      = product.get("sku", "")
    pack_size = product.get("pack_size", "?")
    per_unit = product.get("per_unit", "")
    stock    = product.get("stock")
    sas_url  = selleramp_url(barcode, per_unit or effective_price(product))

    fields = [
        {"name": "📦 Pack Size",     "value": f"{pack_size} units",                               "inline": True},
        {"name": "🔢 Barcode / EAN", "value": f"`{barcode}`" if barcode else "-",                 "inline": True},
        {"name": "🔖 SKU",           "value": f"`{sku}`" if sku else "-",                         "inline": True},
        {"name": "📊 Stock",         "value": f"**{stock} units**" if stock is not None else "-", "inline": True},
    ]

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
        {"name": "💰 Old Price",           "value": f"£{old_price}",                                     "inline": True},
        {"name": "💰 New Price",           "value": f"**£{new_price}**",                                 "inline": True},
        {"name": "📉 Change",              "value": f"{'↓' if is_drop else '↑'} {diff} ({pct})",         "inline": True},
        {"name": "💷 Per Unit (ex. VAT)",  "value": f"£{per_unit}" if per_unit else "-",                 "inline": True},
        {"name": "💷 Per Unit (inc. VAT)", "value": f"£{vat_price(per_unit)}" if per_unit else "-",      "inline": True},
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

    for key in ("image", "barcode", "sku", "pack_size"):
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

    with sync_playwright() as pw:
        browser, context = make_browser(pw)
        try:
            snapshot    = load_snapshot()
            known_slugs = set(snapshot.keys())
            is_first_run = len(known_slugs) == 0

            if is_first_run:
                # ----------------------------------------------------------------
                # FIRST RUN: crawl the entire store to build a complete snapshot.
                # No Discord alerts fired — we just record everything as baseline.
                # ----------------------------------------------------------------
                print("  First run detected — crawling entire store for baseline snapshot...")
                all_products = scrape_all_products(context)
                print(f"  Found {len(all_products)} products across the whole store")
                print(f"  Fetching detail for each product (this will take a while)...")

                for i, product in enumerate(all_products, 1):
                    slug = product["slug"]
                    print(f"  [{i}/{len(all_products)}] {product['title'][:55]}")
                    time.sleep(REQUEST_DELAY + random.uniform(0, 2))
                    product = scrape_product_detail(context, slug, existing=product)
                    entry = snapshot_entry(product)
                    entry["first_seen"] = datetime.now(timezone.utc).isoformat()
                    snapshot[slug] = entry

                    # Save incrementally every 50 products in case of crash
                    if i % 50 == 0:
                        save_snapshot(snapshot)
                        print(f"  Auto-saved snapshot at {i} products")

                save_snapshot(snapshot)
                print(f"  Baseline snapshot complete — {len(snapshot)} products recorded")
                print(f"  No Discord alerts sent (first run baseline only)")

            else:
                # ----------------------------------------------------------------
                # SUBSEQUENT RUNS: check new arrivals + monitor known products
                # ----------------------------------------------------------------

                # 1. Scrape latest pages for new arrivals
                latest = scrape_new_arrivals(context, max_pages=3)
                if not latest:
                    print("  [!] No products scraped")
                    return

                current_slugs = {p["slug"] for p in latest}
                new_slugs     = current_slugs - known_slugs
                print(f"  {len(latest)} products on latest pages, {len(new_slugs)} new")

                # 2. Notify and snapshot new products
                for product in latest:
                    slug = product["slug"]
                    if slug not in new_slugs:
                        continue
                    print(f"  -> NEW: {product['title'][:60]}")
                    time.sleep(REQUEST_DELAY + random.uniform(0, 2))
                    product = scrape_product_detail(context, slug, existing=product)
                    notify_new(product)
                    time.sleep(1.5)
                    entry = snapshot_entry(product)
                    entry["first_seen"] = datetime.now(timezone.utc).isoformat()
                    snapshot[slug] = entry

                # 3. Re-check all known products for price/stock changes
                print(f"  Checking {len(snapshot)} tracked products for changes...")
                for slug, old in list(snapshot.items()):
                    time.sleep(REQUEST_DELAY + random.uniform(0, 1.5))
                    product = scrape_product_detail(
                        context, slug,
                        existing={"slug": slug, "url": old.get("url", f"{BASE_URL}/product/{slug}/")}
                    )
                    check_changes(product, old)
                    entry = snapshot_entry(product)
                    entry["first_seen"] = old.get("first_seen", entry["first_seen"])
                    snapshot[slug] = entry

                save_snapshot(snapshot)
                print(f"  Snapshot saved ({len(snapshot)} products tracked)")

        finally:
            browser.close()


def main():
    print("=" * 55)
    print("  Cherry Cosmetics Monitor")
    print(f"  Watching: {SHOP_URL}")
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
