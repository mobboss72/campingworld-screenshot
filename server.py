# server.py
import os
import sys
import hashlib
import datetime
import tempfile
import traceback
import requests
from typing import Optional, Tuple

from flask import Flask, request, send_from_directory, Response, render_template_string, send_file
from playwright.sync_api import sync_playwright, BrowserContext, Page, Locator

# ---------------------- Config ----------------------
os.environ.setdefault("PLAYWRIGHT_BROWSERS_PATH", "/app/ms-playwright")
PORT = int(os.getenv("PORT", "8080"))
OREGON_ZIP = os.getenv("OREGON_ZIP", "97201")

app = Flask(__name__, static_folder=None)
screenshot_cache: dict[str, str] = {}


# ---------------------- Helpers ---------------------
def https_date() -> Optional[str]:
    try:
        r = requests.head("https://cloudflare.com", timeout=8)
        return r.headers.get("Date")
    except Exception:
        return None

def sha256_file(path: str) -> str:
    if not os.path.exists(path):
        return "N/A (file not found)"
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(1024 * 1024), b""):
            h.update(chunk)
    return h.hexdigest()

def unit_url(stock: str) -> str:
    return f"https://rv.campingworld.com/rv/{stock}"


# ---------------------- Flask -----------------------
@app.get("/")
def root():
    return send_from_directory(".", "index.html")

@app.get("/screenshot/<sid>")
def serve_screenshot(sid: str):
    path = screenshot_cache.get(sid)
    if not path or not os.path.exists(path):
        return Response("Screenshot not found", status=404)
    return send_file(path, mimetype="image/png")

@app.post("/capture")
def capture():
    try:
        stock = (request.form.get("stock") or "").strip()
        if not stock.isdigit():
            return Response("Invalid stock number", status=400)

        price_png, payment_png, url = do_capture(stock)

        utc_now = datetime.datetime.utcnow().strftime("%Y-%m-%d %H:%M:%S UTC")
        hdate = https_date()

        price_ok   = os.path.exists(price_png)
        payment_ok = os.path.exists(payment_png)

        sha_price   = sha256_file(price_png)   if price_ok   else "N/A"
        sha_payment = sha256_file(payment_png) if payment_ok else "N/A"

        sid = str(int(datetime.datetime.utcnow().timestamp()))
        pid = f"price_{stock}_{sid}"
        qid = f"payment_{stock}_{sid}"
        if price_ok:   screenshot_cache[pid] = price_png
        if payment_ok: screenshot_cache[qid] = payment_png

        html = render_template_string("""
<!doctype html>
<html>
<head>
  <meta charset="utf-8"/>
  <title>Camping World Compliance Capture</title>
  <style>
    body{font-family:Arial,Helvetica,sans-serif;background:#f5f5f5;margin:20px}
    h1{margin:0 0 16px;text-align:center}
    .meta{max-width:1100px;margin:0 auto 18px;background:#fff;border:1px solid #ddd;padding:12px;border-radius:6px}
    .meta p{margin:6px 0}
    .grid{display:flex;flex-wrap:wrap;gap:16px;justify-content:center}
    .card{background:#fff;border:2px solid #333;border-radius:6px;max-width:680px;padding:12px;box-shadow:0 2px 4px rgba(0,0,0,.08)}
    .card h2{margin:0 0 8px;border-bottom:2px solid #333;padding-bottom:6px;font-size:20px}
    .imgwrap{background:#fafafa;border:1px solid #ddd;border-radius:4px;padding:10px;min-height:220px}
    img{max-width:100%;height:auto;display:block}
    .ok{color:green;font-weight:600}
    .err{color:#b00020;font-weight:600}
    code{word-break:break-all}
  </style>
</head>
<body>
  <h1>Camping World Compliance Capture</h1>
  <div class="meta">
    <p><strong>Stock:</strong> {{stock}}</p>
    <p><strong>URL:</strong> <a href="{{url}}" target="_blank" rel="noopener">{{url}}</a></p>
    <p><strong>UTC:</strong> {{utc}}</p>
    <p><strong>HTTPS Date:</strong> {{hdate or 'unavailable'}}</p>
  </div>

  <div class="grid">
    <div class="card">
      <h2>Price Tooltip — Full Page</h2>
      <div class="imgwrap">
        {% if price_ok %}
          <img src="/screenshot/{{pid}}" alt="Price Tooltip"/>
          <p class="ok">✓ Captured</p>
        {% else %}
          <p class="err">✗ Could not capture price tooltip.</p>
        {% endif %}
      </div>
      <p><strong>SHA-256:</strong> <code>{{sha_price}}</code></p>
    </div>

    <div class="card">
      <h2>Payment Tooltip — Full Page</h2>
      <div class="imgwrap">
        {% if payment_ok %}
          <img src="/screenshot/{{qid}}" alt="Payment Tooltip"/>
          <p class="ok">✓ Captured</p>
        {% else %}
          <p class="err">✗ Could not capture payment tooltip.</p>
        {% endif %}
      </div>
      <p><strong>SHA-256:</strong> <code>{{sha_payment}}</code></p>
    </div>
  </div>
</body>
</html>
        """, stock=stock, url=url, utc=utc_now, hdate=hdate,
           price_ok=price_ok, payment_ok=payment_ok,
           pid=pid, qid=qid, sha_price=sha_price, sha_payment=sha_payment)
        return Response(html, mimetype="text/html")
    except Exception as e:
        print("❌ /capture failed:", e, file=sys.stderr)
        traceback.print_exc()
        return Response(f"Error: {e}", status=500)


# -------------------- Playwright --------------------
HIDE_NOISE_CSS = """
/* hide common chat/cookie elements that steal hover/focus */
iframe[src*="chat"], iframe[title*="chat" i], [class*="chat" i], [id*="chat" i],
#launcher, .drift-widget, .intercom-launcher, [class*="cookie" i], [id*="cookie" i] {
  display:none!important; visibility:hidden!important; pointer-events:none!important;
}
"""

def prepare_page(ctx: BrowserContext, url: str) -> Page:
    page = ctx.new_page()
    page.goto(url, wait_until="domcontentloaded")
    try:
        page.wait_for_load_state("networkidle", timeout=30000)
    except Exception as e:
        print(f"networkidle timeout: {e}")

    # Force Oregon ZIP so pricing/payment renders consistently
    try:
        page.evaluate("""(zip)=>{
          try{ localStorage.setItem('cw_zip', zip); }catch(e){}
          document.cookie='cw_zip='+zip+';path=/;SameSite=Lax';
        }""", OREGON_ZIP)
        page.reload(wait_until="domcontentloaded")
        try: page.wait_for_load_state("networkidle", timeout=20000)
        except Exception: pass
    except Exception as e:
        print(f"ZIP inject failed: {e}")

    try: page.add_style_tag(content=HIDE_NOISE_CSS)
    except Exception: pass

    return page


# ---- PRICE (keep your working snippet exactly) ----
def capture_price_tooltip(page: Page, out_path: str) -> bool:
    try:
        page.wait_for_selector(".MuiTypography-root.MuiTypography-subtitle1", state="visible", timeout=15000)
    except Exception:
        pass

    price_selector = ".MuiTypography-root.MuiTypography-subtitle1:visible"
    price_elements = page.locator(price_selector)
    try:
        print(f"Number of price elements found: {price_elements.count()}")
    except Exception:
        pass

    visible_price: Optional[Locator] = None
    try:
        for i in range(price_elements.count()):
            elem = price_elements.nth(i)
            if elem.is_visible():
                visible_price = elem
                print(f"Visible price element found at index {i}")
                break
    except Exception as e:
        print(f"Iterating price elems failed: {e}")

    if not visible_price:
        print("No visible price element found")
        return False

    try:
        visible_price.scroll_into_view_if_needed(timeout=5000)

        # prefer an adjacent info icon if present
        icon = visible_price.locator(
            "xpath=following::*[(self::button or self::*[name()='svg'] or contains(@class,'MuiSvgIcon-root'))][1]"
        )
        trigger = icon.first if (icon.count() and icon.first.is_visible()) else visible_price

        trigger.hover(timeout=10000, force=True)
        page.wait_for_timeout(1100)  # let MUI tooltip appear
        # verify tooltip (role or aria-describedby)
        try:
            page.wait_for_selector("[role='tooltip'], .MuiTooltip-popper, [data-popper-placement], span[aria-describedby*='mui-tooltip']",
                                   state="visible", timeout=2500)
        except Exception:
            pass
        page.screenshot(path=out_path, full_page=True)
        print(f"Price full page screenshot saved to: {out_path}")
        return True
    except Exception as e:
        print(f"Price hover failed: {e}")
        return False


# ---- PAYMENT (robust multi-strategy) ----
def tooltip_visible(page: Page) -> bool:
    try:
        loc = page.locator("[role='tooltip'], .MuiTooltip-popper, [data-popper-placement], span[aria-describedby*='mui-tooltip']")
        return loc.count() > 0 and loc.first.is_visible()
    except Exception:
        return False

def near(bb_a, bb_b, px: float) -> bool:
    if not bb_a or not bb_b:
        return False
    ax, ay = bb_a["x"] + bb_a["width"]/2,  bb_a["y"] + bb_a["height"]/2
    bx, by = bb_b["x"] + bb_b["width"]/2,  bb_b["y"] + bb_b["height"]/2
    dx, dy = ax-bx, ay-by
    return (dx*dx + dy*dy) ** 0.5 <= px

def capture_payment_tooltip(page: Page, out_path: str) -> bool:
    # 1) Anchor on a payment text node
    pay_text = page.locator(r"text=/\$\s*\d[\d,\.]*\s*\/\s*mo/i").first
    if not (pay_text and pay_text.count()):
        pay_text = page.locator(r"text=/est\.?\s*payment|payment|per month|monthly|\/mo/i").first

    if not (pay_text and pay_text.count() and pay_text.is_visible()):
        print("❌ Payment text element not found")
        return False

    try: pay_text.scroll_into_view_if_needed(timeout=5000)
    except Exception: pass

    pay_bb = pay_text.bounding_box()
    if not pay_bb:
        print("No bounding box for payment text")
        return False

    # 2) Try immediate sibling icon/button
    try:
        inline = pay_text.locator(
            "xpath=following::*[(self::button or self::*[name()='svg'] or contains(@class,'MuiSvgIcon-root') "
            " or @role='button' or contains(@class,'info') or contains(@aria-label,'info') or contains(@data-testid,'info'))][1]"
        )
        if inline and inline.count() and inline.first.is_visible():
            for action in ("hover","focus","click","synthetic"):
                try:
                    t = inline.first
                    if action == "hover":
                        t.hover(timeout=7000, force=True)
                    elif action == "focus":
                        t.focus()
                    elif action == "click":
                        t.click(timeout=2500)
                    else:
                        page.evaluate("""(el)=>{
                          ['pointerover','mouseenter','mouseover','focusin','pointermove'].forEach(
                            ty=>el.dispatchEvent(new MouseEvent(ty,{bubbles:true}))
                          );
                        }""", t)
                    page.wait_for_timeout(350)
                    if tooltip_visible(page):
                        page.screenshot(path=out_path, full_page=True)
                        print(f"Payment full page screenshot saved to: {out_path} (inline)")
                        return True
                except Exception:
                    continue
    except Exception:
        pass

    # 3) Scan nearby clickable/icon-ish nodes and try them
    cand = page.locator(
        "button, [role='button'], [aria-haspopup='true'], [aria-label*='info' i], [data-testid*='info' i], "
        ".MuiSvgIcon-root, svg[focusable], svg[tabindex], span[role='img']"
    )
    try: total = min(60, cand.count())
    except Exception: total = 0

    tried = 0
    for i in range(total):
        try:
            c = cand.nth(i)
            if not c.is_visible():
                continue
            bb = c.bounding_box()
            if not near(bb, pay_bb, 420):
                continue
            tried += 1
            for action in ("hover","focus","click","synthetic"):
                try:
                    if action == "hover":
                        c.hover(timeout=6000, force=True)
                    elif action == "focus":
                        c.focus()
                    elif action == "click":
                        c.click(timeout=2500)
                    else:
                        page.evaluate("""(el)=>{
                          ['pointerover','mouseenter','mouseover','focusin','pointermove'].forEach(
                            ty=>el.dispatchEvent(new MouseEvent(ty,{bubbles:true}))
                          );
                        }""", c)
                    page.wait_for_timeout(300)
                    if tooltip_visible(page):
                        page.screenshot(path=out_path, full_page=True)
                        print(f"Payment full page screenshot saved to: {out_path} (nearby trigger after {tried} tries)")
                        return True
                except Exception:
                    continue
        except Exception:
            continue

    print("❌ Payment tooltip not captured (scanned nearby triggers).")
    return False


def do_capture(stock: str) -> Tuple[str, str, str]:
    url = unit_url(stock)
    tmpdir = tempfile.mkdtemp(prefix=f"cw-{stock}-")
    ts = datetime.datetime.utcnow().strftime("%Y%m%d-%H%M%S")
    price_png   = os.path.join(tmpdir, f"cw_{stock}_PRICE_{ts}.png")
    payment_png = os.path.join(tmpdir, f"cw_{stock}_PAYMENT_{ts}.png")

    print(f"\n=== Capture start stock={stock} ===")
    print(f"URL: {url}")
    print(f"TEMP DIR: {tmpdir}")

    with sync_playwright() as p:
        browser = p.chromium.launch(
            headless=True,
            args=["--no-sandbox","--disable-setuid-sandbox","--disable-dev-shm-usage"]
        )
        ctx = browser.new_context(
            viewport={"width":1920,"height":1080},
            permissions=["geolocation"],
            geolocation={"latitude":45.5122,"longitude":-122.6587},
            locale="en-US",
            user_agent="Mozilla/5.0 Chrome"
        )

        # Price on its own page (keeps payment popup logic untouched)
        page_price = prepare_page(ctx, url)
        ok_price = capture_price_tooltip(page_price, price_png)
        try: page_price.close()
        except Exception: pass

        # Payment on a fresh page so the site doesn’t auto-close the first tooltip
        page_pay = prepare_page(ctx, url)
        ok_pay = capture_payment_tooltip(page_pay, payment_png)
        try: page_pay.close()
        except Exception: pass

        browser.close()

    if not ok_price:   print("⚠️ Price tooltip not captured.")
    if not ok_pay:     print("⚠️ Payment tooltip not captured.")
    print("=== Capture done ===")

    return price_png, payment_png, url


# ---------------------- Main ------------------------
if __name__ == "__main__":
    app.run(host="0.0.0.0", port=PORT)
