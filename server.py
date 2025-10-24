# server.py
import os
import sys
import hashlib
import datetime
import tempfile
import traceback
import requests
from pathlib import Path
from typing import Optional, Tuple

from flask import Flask, request, send_from_directory, Response, render_template_string, send_file
from playwright.sync_api import sync_playwright, Page, Locator

# -----------------------------
# Environment / constants
# -----------------------------
os.environ.setdefault("PLAYWRIGHT_BROWSERS_PATH", "/app/ms-playwright")
PORT = int(os.getenv("PORT", "8080"))
OREGON_ZIP = os.getenv("OREGON_ZIP", "97201")

DATA_DIR = Path("/app/data")
DATA_DIR.mkdir(parents=True, exist_ok=True)
CHANGELOG = DATA_DIR / "changelog.md"

# -----------------------------
# App / cache
# -----------------------------
app = Flask(__name__, static_folder=None)
screenshot_cache = {}

# -----------------------------
# Utilities
# -----------------------------
def https_date() -> Optional[str]:
    """Trusted network time from HTTPS response headers (RFC 7231 Date)."""
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

def log_changelog(stock: str, url: str, tmpdir: str, notes: str) -> None:
    ts = datetime.datetime.utcnow().strftime("%Y-%m-%d %H:%M:%S UTC")
    try:
        with CHANGELOG.open("a") as f:
            f.write(f"- {ts} | stock={stock} | {url}\n  {notes}\n  (tmpdir: {tmpdir})\n")
    except Exception:
        pass

# -----------------------------
# Flask routes
# -----------------------------
@app.get("/")
def root():
    return send_from_directory(".", "index.html")

@app.get("/screenshot/<screenshot_id>")
def serve_screenshot(screenshot_id):
    path = screenshot_cache.get(screenshot_id)
    if not path or not os.path.exists(path):
        return Response("Screenshot not found", status=404)
    return send_file(path, mimetype="image/png")

@app.post("/capture")
def capture():
    try:
        stock = (request.form.get("stock") or "").strip()
        if not stock.isdigit():
            return Response("Invalid stock number", status=400)

        price_png, payment_png, url, tmpdir = do_capture(stock)

        utc_now = datetime.datetime.utcnow().strftime("%Y-%m-%d %H:%M:%S UTC")
        hdate = https_date()

        price_exists = os.path.exists(price_png)
        payment_exists = os.path.exists(payment_png)

        sha_price = sha256_file(price_png) if price_exists else "N/A"
        sha_payment = sha256_file(payment_png) if payment_exists else "N/A"

        # Make IDs to serve images
        tsid = str(int(datetime.datetime.utcnow().timestamp()))
        price_id = f"price_{stock}_{tsid}"
        pay_id = f"payment_{stock}_{tsid}"

        if price_exists: screenshot_cache[price_id] = price_png
        if payment_exists: screenshot_cache[pay_id] = payment_png

        notes = f"price={'ok' if price_exists else 'fail'}, payment={'ok' if payment_exists else 'fail'}"
        log_changelog(stock, url, tmpdir, notes)

        html = render_template_string("""
<!DOCTYPE html>
<html>
<head>
<meta charset="UTF-8" />
<title>Compliance Capture</title>
<style>
 body{font-family:Arial,Helvetica,sans-serif;background:#f5f5f5;margin:20px}
 h2{text-align:center;margin-bottom:10px}
 .meta{max-width:1080px;margin:0 auto 20px auto;background:#fff;border:1px solid #ddd;padding:12px;border-radius:6px}
 .meta p{margin:6px 0}
 .container{display:flex;flex-wrap:wrap;gap:16px;justify-content:center}
 .box{background:#fff;border:2px solid #333;border-radius:6px;max-width:680px;padding:12px;box-shadow:0 2px 4px rgba(0,0,0,.08)}
 .box h3{margin:0 0 10px 0;border-bottom:2px solid #333;padding-bottom:6px}
 .imgwrap{background:#fafafa;border:1px solid #ddd;border-radius:4px;padding:10px;min-height:220px}
 img{max-width:100%;height:auto;display:block}
 .success{color:green;font-weight:600}
 .error{color:#b00020;font-weight:600}
 code{word-break:break-all}
</style>
</head>
<body>
  <h2>Camping World Compliance Capture</h2>
  <div class="meta">
    <p><strong>Stock:</strong> {{ stock }}</p>
    <p><strong>URL:</strong> <a href="{{ url }}" target="_blank">{{ url }}</a></p>
    <p><strong>UTC:</strong> {{ utc }}</p>
    <p><strong>HTTPS Date:</strong> {{ hdate or 'unavailable' }}</p>
  </div>

  <div class="container">
    <div class="box">
      <h3>Price Tooltip — Full Page</h3>
      <div class="imgwrap">
        {% if price_exists %}
          <img src="/screenshot/{{ price_id }}" alt="Price Tooltip" />
          <p class="success">✓ Captured</p>
        {% else %}
          <p class="error">✗ Could not capture price tooltip</p>
        {% endif %}
      </div>
      <p><strong>SHA-256:</strong> <code>{{ sha_price }}</code></p>
    </div>

    <div class="box">
      <h3>Payment Tooltip — Full Page</h3>
      <div class="imgwrap">
        {% if payment_exists %}
          <img src="/screenshot/{{ pay_id }}" alt="Payment Tooltip" />
          <p class="success">✓ Captured</p>
        {% else %}
          <p class="error">✗ Could not capture payment tooltip</p>
        {% endif %}
      </div>
      <p><strong>SHA-256:</strong> <code>{{ sha_payment }}</code></p>
    </div>
  </div>
</body>
</html>
        """,
        stock=stock, url=url, utc=utc_now, hdate=hdate,
        price_exists=price_exists, payment_exists=payment_exists,
        price_id=price_id, pay_id=pay_id,
        sha_price=sha_price, sha_payment=sha_payment)

        return Response(html, mimetype="text/html")
    except Exception as e:
        print("❌ /capture failed:", e, file=sys.stderr)
        traceback.print_exc()
        return Response(f"Error: {e}", status=500)

# -----------------------------
# Core capture logic
# -----------------------------
def do_capture(stock: str) -> Tuple[str, str, str, str]:
    """
    Returns (price_png, payment_png, url, tmpdir)
    """
    url = f"https://rv.campingworld.com/rv/{stock}"
    tmpdir = tempfile.mkdtemp(prefix=f"cw-{stock}-")
    ts = datetime.datetime.utcnow().strftime("%Y%m%d-%H%M%S")

    price_png = os.path.join(tmpdir, f"cw_{stock}_price_{ts}.png")
    payment_png = os.path.join(tmpdir, f"cw_{stock}_payment_{ts}.png")

    print(f"\n=== Starting capture for stock {stock} ===")
    print(f"Temp dir: {tmpdir}\nURL: {url}")

    utc_now = datetime.datetime.utcnow().strftime("%Y-%m-%d %H:%M:%S UTC")
    hdate = https_date()

    with sync_playwright() as p:
        browser = p.chromium.launch(
            headless=True,
            args=["--no-sandbox", "--disable-setuid-sandbox", "--disable-dev-shm-usage"]
        )
        ctx = browser.new_context(
            viewport={"width": 1920, "height": 1080},
            permissions=["geolocation"],
            geolocation={"latitude": 45.5122, "longitude": -122.6587},
            locale="en-US",
            user_agent="Mozilla/5.0 Chrome"
        )
        page = ctx.new_page()

        # Load & settle
        page.goto(url, wait_until="domcontentloaded")
        try:
            page.wait_for_load_state("networkidle", timeout=30000)
        except Exception as e:
            print(f"networkidle wait failed: {e}")

        # Force Oregon ZIP for pricing/payment rules
        try:
            page.evaluate("""(zip)=>{
                try{localStorage.setItem('cw_zip', zip);}catch(e){}
                document.cookie='cw_zip='+zip+';path=/;SameSite=Lax';
            }""", OREGON_ZIP)
            page.reload(wait_until="domcontentloaded")
            try:
                page.wait_for_load_state("networkidle", timeout=20000)
            except Exception:
                pass
        except Exception as e:
            print(f"ZIP inject failed: {e}")

        # ----- helpers -----
        def inject_banner(page: Page, label: str):
            """Overlay legal time markers inside the page so they're baked into the PNG."""
            page.evaluate(
                """(text)=>{
                    const id='cw-proof-banner';
                    let el=document.getElementById(id);
                    if(!el){
                      el=document.createElement('div');
                      el.id=id;
                      Object.assign(el.style,{
                        position:'fixed', top:'10px', right:'10px', zIndex: 2147483647,
                        background:'rgba(0,0,0,0.72)', color:'#fff',
                        padding:'8px 10px', font:'12px/1.35 -apple-system,Segoe UI,Arial',
                        borderRadius:'6px', boxShadow:'0 2px 8px rgba(0,0,0,.35)',
                        maxWidth:'420px', pointerEvents:'none'
                      });
                      document.body.appendChild(el);
                    }
                    el.textContent=text;
                }""",
                f"{label} • UTC {utc_now} • HTTPS Date {hdate or 'unavailable'}"
            )

        def remove_banner(page: Page):
            page.evaluate("""()=>{
                const el=document.getElementById('cw-proof-banner'); if(el) el.remove();
            }""")

        def is_tooltip_open(page: Page) -> bool:
            t = page.locator("[role='tooltip'], .MuiTooltip-popper, [data-popper-placement], .MuiPopover-root")
            try:
                return t.first.is_visible()
            except Exception:
                return False

        def activate_tooltip(trigger: Optional[Locator], label: str) -> bool:
            if trigger is None:
                return False
            try:
                trigger.scroll_into_view_if_needed(timeout=5000)
                page.wait_for_timeout(120)
            except Exception:
                pass

            # Try hover
            try:
                trigger.hover(force=True, timeout=5000)
                page.wait_for_timeout(250)
                if is_tooltip_open(page): return True
            except Exception:
                pass
            # Try focus
            try:
                trigger.focus()
                page.wait_for_timeout(200)
                if is_tooltip_open(page): return True
            except Exception:
                pass
            # Synthetic events (helps when hover handlers are attached above)
            try:
                page.evaluate("""(el)=>{
                    ['pointerover','mouseenter','mouseover','focusin'].forEach(t=>el.dispatchEvent(new MouseEvent(t,{bubbles:true})));
                }""", trigger)
                page.wait_for_timeout(250)
                if is_tooltip_open(page): return True
            except Exception:
                pass
            # Click (some popovers)
            try:
                trigger.click(timeout=3000)
                page.wait_for_timeout(250)
                if is_tooltip_open(page): return True
            except Exception:
                pass
            return False

        # ---- PRICE trigger (more robust) ----
        def find_price_trigger(page: Page) -> Optional[Locator]:
            cands = [
                # Icon immediately following a big price dollar amount
                page.locator("xpath=(//*[matches(., '\\$\\s*\\d[\\d,]*(\\.\\d{2})?')])[1]/following::*[(self::*[name()='svg'] or contains(@class,'MuiSvgIcon-root') or self::button)][1]"),
                # Any element containing 'price' with a following icon
                page.locator("xpath=//*[contains(translate(., 'PRICE','price'),'price')]/following::*[(self::*[name()='svg'] or contains(@class,'MuiSvgIcon-root') or self::button)][1]"),
                # Sometimes the label itself is the trigger
                page.locator("xpath=(//*[contains(translate(., 'PRICE','price'),'price')])[1]"),
                # Fallback: the big dollar amount node
                page.locator("xpath=(//*[matches(., '\\$\\s*\\d[\\d,]*(\\.\\d{2})?')])[1]"),
            ]
            for cand in cands:
                try:
                    if cand.count():
                        if cand.first.is_visible():
                            return cand.first
                except Exception:
                    continue
            return None

        # ---- PAYMENT trigger (text-first + neighbor icon) ----
        def find_payment_trigger(page: Page) -> Optional[Locator]:
            payment_text = None
            text_cands = [
                page.get_by_text("/mo", exact=False),
                page.get_by_text("per month", exact=False),
                page.get_by_text("monthly", exact=False),
                page.get_by_text("payment", exact=False),
                page.locator("xpath=//*[matches(., '\\/\\s*mo')]"),
            ]
            for cand in text_cands:
                try:
                    n = cand.count()
                    for i in range(n):
                        el = cand.nth(i)
                        if el.is_visible():
                            txt = (el.text_content() or "").lower()
                            if any(k in txt for k in ["/mo", "per month", "monthly", "payment"]):
                                payment_text = el
                                raise StopIteration
                except StopIteration:
                    break
                except Exception:
                    pass

            if payment_text:
                icon = payment_text.locator("xpath=following::*[(self::button or self::*[name()='svg'] or contains(@class,'MuiSvgIcon-root'))][1]")
                try:
                    if icon.count() and icon.first.is_visible():
                        return icon.first
                except Exception:
                    pass
                return payment_text
            return None

        # ===== PRICE =====
        print("\n=== PRICE tooltip ===")
        price_trigger = find_price_trigger(page)
        price_open = activate_tooltip(price_trigger, "Price Tooltip")
        inject_banner(page, "Price Tooltip")
        try:
            page.screenshot(path=price_png, full_page=True)
            print(f"Price capture saved -> {price_png} (open={price_open})")
        except Exception as e:
            print(f"Price screenshot failed: {e}")
        finally:
            remove_banner(page)

        # Close any popover that might stay open to avoid obstructing payment
        try:
            page.keyboard.press("Escape")
            page.wait_for_timeout(150)
        except Exception:
            pass

        # ===== PAYMENT =====
        print("\n=== PAYMENT tooltip ===")
        payment_trigger = find_payment_trigger(page)
        payment_open = activate_tooltip(payment_trigger, "Payment Tooltip")
        inject_banner(page, "Payment Tooltip")
        try:
            page.screenshot(path=payment_png, full_page=True)
            print(f"Payment capture saved -> {payment_png} (open={payment_open})")
        except Exception as e:
            print(f"Payment screenshot failed: {e}")
        finally:
            remove_banner(page)

        browser.close()
        print("=== Done ===")

    return price_png, payment_png, url, tmpdir

# -----------------------------
# Entry
# -----------------------------
if __name__ == "__main__":
    app.run(host="0.0.0.0", port=PORT)
