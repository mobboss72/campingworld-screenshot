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
from playwright.sync_api import sync_playwright, Page, BrowserContext, Locator

# --------------------------------------------
# Config
# --------------------------------------------
os.environ.setdefault("PLAYWRIGHT_BROWSERS_PATH", "/app/ms-playwright")
PORT = int(os.getenv("PORT", "8080"))
OREGON_ZIP = os.getenv("OREGON_ZIP", "97201")

app = Flask(__name__, static_folder=None)
screenshot_cache: dict[str, str] = {}


# --------------------------------------------
# Utilities
# --------------------------------------------
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


# --------------------------------------------
# Flask routes
# --------------------------------------------
@app.get("/")
def root():
    return send_from_directory(".", "index.html")

@app.get("/screenshot/<sid>")
def serve_screenshot(sid):
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

        price_png, pay_png, url = do_capture(stock)

        utc_now = datetime.datetime.utcnow().strftime("%Y-%m-%d %H:%M:%S UTC")
        hdate = https_date()

        price_exists = os.path.exists(price_png)
        pay_exists   = os.path.exists(pay_png)

        sha_price = sha256_file(price_png) if price_exists else "N/A"
        sha_pay   = sha256_file(pay_png)   if pay_exists   else "N/A"

        tsid = str(int(datetime.datetime.utcnow().timestamp()))
        pid = f"price_{stock}_{tsid}"
        qid = f"payment_{stock}_{tsid}"
        if price_exists: screenshot_cache[pid] = price_png
        if pay_exists:   screenshot_cache[qid] = pay_png

        html = render_template_string("""
<!doctype html>
<html>
<head>
<meta charset="utf-8"/>
<title>Camping World Compliance Capture</title>
<style>
  body{font-family:Arial,Helvetica,sans-serif;background:#f5f5f5;margin:20px}
  h2{margin:0 0 16px;text-align:center}
  .meta{max-width:1080px;margin:0 auto 18px;background:#fff;border:1px solid #ddd;padding:12px;border-radius:6px}
  .meta p{margin:6px 0}
  .grid{display:flex;flex-wrap:wrap;gap:16px;justify-content:center}
  .card{background:#fff;border:2px solid #333;border-radius:6px;max-width:680px;padding:12px;box-shadow:0 2px 4px rgba(0,0,0,.08)}
  .card h3{margin:0 0 8px;border-bottom:2px solid #333;padding-bottom:6px}
  .imgwrap{background:#fafafa;border:1px solid #ddd;border-radius:4px;padding:10px;min-height:220px}
  img{max-width:100%;height:auto;display:block}
  .ok{color:green;font-weight:600}
  .err{color:#b00020;font-weight:600}
  code{word-break:break-all}
</style>
</head>
<body>
<h2>Camping World Compliance Capture</h2>
<div class="meta">
  <p><strong>Stock:</strong> {{stock}}</p>
  <p><strong>URL:</strong> <a href="{{url}}" target="_blank" rel="noopener">{{url}}</a></p>
  <p><strong>UTC:</strong> {{utc}}</p>
  <p><strong>HTTPS Date:</strong> {{hdate or 'unavailable'}}</p>
</div>

<div class="grid">
  <div class="card">
    <h3>Price Tooltip — Full Page</h3>
    <div class="imgwrap">
      {% if price_exists %}
        <img src="/screenshot/{{pid}}" alt="Price Tooltip"/>
        <p class="ok">✓ Captured</p>
      {% else %}
        <p class="err">✗ Could not capture price tooltip.</p>
      {% endif %}
    </div>
    <p><strong>SHA-256:</strong> <code>{{sha_price}}</code></p>
  </div>

  <div class="card">
    <h3>Payment Tooltip — Full Page</h3>
    <div class="imgwrap">
      {% if pay_exists %}
        <img src="/screenshot/{{qid}}" alt="Payment Tooltip"/>
        <p class="ok">✓ Captured</p>
      {% else %}
        <p class="err">✗ Could not capture payment tooltip.</p>
      {% endif %}
    </div>
    <p><strong>SHA-256:</strong> <code>{{sha_pay}}</code></p>
  </div>
</div>
</body>
</html>
        """, stock=stock, url=url, utc=utc_now, hdate=hdate,
           price_exists=price_exists, pay_exists=pay_exists,
           pid=pid, qid=qid, sha_price=sha_price, sha_pay=sha_pay)
        return Response(html, mimetype="text/html")

    except Exception as e:
        print("❌ /capture failed:", e, file=sys.stderr)
        traceback.print_exc()
        return Response(f"Error: {e}", status=500)


# --------------------------------------------
# Playwright capture (two independent pages)
# --------------------------------------------
def prepare_page(ctx: BrowserContext, url: str) -> Page:
    page = ctx.new_page()
    page.goto(url, wait_until="domcontentloaded")
    try:
        page.wait_for_load_state("networkidle", timeout=30000)
    except Exception as e:
        print(f"networkidle timeout: {e}")

    # Force Oregon pricing context
    try:
        page.evaluate("""(zip)=>{
            try{ localStorage.setItem('cw_zip', zip); }catch(e){}
            document.cookie='cw_zip='+zip+';path=/;SameSite=Lax';
        }""", OREGON_ZIP)
        page.reload(wait_until="domcontentloaded")
        try:
            page.wait_for_load_state("networkidle", timeout=20000)
        except Exception:
            pass
    except Exception as e:
        print(f"ZIP inject failed: {e}")

    return page

def capture_price_tooltip(page: Page, out_path: str) -> bool:
    # === YOUR WORKING SNIPPET (kept intact) ===
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
        # try an adjacent info icon first (some tooltips attach there)
        icon = visible_price.locator("xpath=following::*[(self::button or self::*[name()='svg'] or contains(@class,'MuiSvgIcon-root'))][1]")
        trigger = icon.first if (icon.count() and icon.first.is_visible()) else visible_price

        trigger.hover(timeout=10000, force=True)
        page.wait_for_timeout(1000)  # wait for tooltip
        page.screenshot(path=out_path, full_page=True)
        print(f"Price full page screenshot saved to: {out_path}")
        return True
    except Exception as e:
        print(f"Price hover failed: {e}")
        return False

def capture_payment_tooltip(page: Page, out_path: str) -> bool:
    # robust: prefer text that includes payment hints; hover/focus/click fallback
    payment_selector = ".MuiTypography-root.MuiTypography-subtitle2:visible"
    payment_elems = page.locator(payment_selector)
    try:
        cnt = payment_elems.count()
        print(f"Found {cnt} payment elements")
    except Exception:
        cnt = 0

    visible_payment: Optional[Locator] = None
    for i in range(cnt):
        try:
            el = payment_elems.nth(i)
            if el.is_visible():
                txt = (el.text_content() or "").lower()
                if any(k in txt for k in ("payment", "/mo", "per month", "monthly")):
                    visible_payment = el
                    print(f"Using payment element at index {i} with text: {txt}")
                    break
        except Exception:
            continue

    if not visible_payment:
        print("❌ Payment element not found by subtitle2; try broader selector")
        # fallback: any element with payment-ish text
        alt = page.locator("text=/.*(per month|/mo|payment).*/i").first
        if alt and alt.count() and alt.is_visible():
            visible_payment = alt

    if not visible_payment:
        return False

    try:
        visible_payment.scroll_into_view_if_needed(timeout=5000)
        trigger: Locator = visible_payment
        icon = visible_payment.locator("xpath=following::*[(self::button or self::*[name()='svg'] or contains(@class,'MuiSvgIcon-root'))][1]")
        if icon.count() and icon.first.is_visible():
            trigger = icon.first

        opened = False
        for action in ("hover", "focus", "click", "synthetic"):
            try:
                if action == "hover":
                    trigger.hover(timeout=10000, force=True)
                elif action == "focus":
                    trigger.focus()
                elif action == "click":
                    trigger.click(timeout=4000)
                else:
                    page.evaluate(
                        """(el)=>{
                          ['pointerover','mouseenter','mouseover','focusin'].forEach(
                            t=>el.dispatchEvent(new MouseEvent(t,{bubbles:true}))
                          );
                        }""",
                        trigger,
                    )
                page.wait_for_timeout(300)
                # simple visibility probe
                t = page.locator("[role='tooltip'], .MuiTooltip-popper, [data-popper-placement], .MuiPopover-root")
                if t.first.is_visible():
                    opened = True
                    break
            except Exception:
                continue

        page.screenshot(path=out_path, full_page=True)
        print(f"Payment full page screenshot saved to: {out_path} (opened={opened})")
        return True
    except Exception as e:
        print(f"Payment hover failed: {e}")
        return False


def do_capture(stock: str) -> Tuple[str, str, str]:
    url = unit_url(stock)
    tmpdir = tempfile.mkdtemp(prefix=f"cw-{stock}-")
    ts = datetime.datetime.utcnow().strftime("%Y%m%d-%H%M%S")
    price_png = os.path.join(tmpdir, f"cw_{stock}_PRICE_{ts}.png")
    pay_png   = os.path.join(tmpdir, f"cw_{stock}_PAYMENT_{ts}.png")

    print(f"\n=== Capture start stock={stock} ===")
    print(f"URL: {url}")
    print(f"TEMP DIR: {tmpdir}")

    with sync_playwright() as p:
        browser = p.chromium.launch(headless=True, args=["--no-sandbox","--disable-setuid-sandbox","--disable-dev-shm-usage"])
        ctx = browser.new_context(
            viewport={"width":1920,"height":1080},
            permissions=["geolocation"],
            geolocation={"latitude":45.5122,"longitude":-122.6587},
            locale="en-US",
            user_agent="Mozilla/5.0 Chrome"
        )

        # IMPORTANT: use separate pages so the site can’t auto-close one tooltip when the other opens
        page_price = prepare_page(ctx, url)
        ok_price = capture_price_tooltip(page_price, price_png)
        try: page_price.close()
        except Exception: pass

        page_pay = prepare_page(ctx, url)
        ok_pay = capture_payment_tooltip(page_pay, pay_png)
        try: page_pay.close()
        except Exception: pass

        browser.close()

    if not ok_price:
        print("⚠️ Price tooltip not captured.")
    if not ok_pay:
        print("⚠️ Payment tooltip not captured.")

    print("=== Capture done ===")
    return price_png, pay_png, url


# --------------------------------------------
# main
# --------------------------------------------
if __name__ == "__main__":
    app.run(host="0.0.0.0", port=PORT)
