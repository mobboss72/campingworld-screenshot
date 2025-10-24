# server.py
import os
import sys
import hashlib
import datetime
import tempfile
import traceback
import requests

from flask import Flask, request, send_from_directory, Response, render_template_string, send_file
from playwright.sync_api import sync_playwright

# Persist Playwright browser in a writable place
os.environ.setdefault("PLAYWRIGHT_BROWSERS_PATH", "/app/ms-playwright")

PORT = int(os.getenv("PORT", "8080"))
OREGON_ZIP = os.getenv("OREGON_ZIP", "97201")

# In-memory cache for serving the images back
screenshot_cache = {}

app = Flask(__name__, static_folder=None)

# ----------------------------- Routes -----------------------------

@app.get("/")
def root():
    # Serve your existing index.html (has the stock form)
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

        price_png_path, payment_png_path, url = do_capture(stock)

        utc_now = datetime.datetime.utcnow().strftime("%Y-%m-%d %H:%M:%S UTC")
        hdate = https_date()

        price_exists = os.path.exists(price_png_path) if price_png_path else False
        payment_exists = os.path.exists(payment_png_path) if payment_png_path else False

        sha_price = sha256_file(price_png_path) if price_exists else "N/A"
        sha_payment = sha256_file(payment_png_path) if payment_exists else "N/A"

        price_id = f"price_{stock}_{int(datetime.datetime.utcnow().timestamp())}"
        payment_id = f"payment_{stock}_{int(datetime.datetime.utcnow().timestamp())}"
        if price_exists: screenshot_cache[price_id] = price_png_path
        if payment_exists: screenshot_cache[payment_id] = payment_png_path

        html = render_template_string("""
        <!doctype html>
        <html>
        <head>
          <meta charset="utf-8"/>
          <title>Camping World Compliance Capture</title>
          <style>
            body{font-family:Inter,Arial,sans-serif;background:#f3f4f6;color:#111;margin:0;padding:24px}
            h1{margin:0 0 16px}
            .meta{background:#fff;border:1px solid #e5e7eb;border-radius:10px;padding:14px 16px;margin-bottom:18px}
            .grid{display:grid;grid-template-columns:1fr 1fr;gap:18px}
            .card{background:#fff;border:1px solid #e5e7eb;border-radius:12px;padding:16px}
            .title{font-size:20px;font-weight:700;margin:0 0 12px}
            .ok{color:#059669;font-weight:700}
            .bad{color:#dc2626;font-weight:700}
            img{width:100%;height:auto;border:1px solid #e5e7eb;border-radius:8px}
            code{font-family:ui-monospace,SFMono-Regular,Menlo,Monaco,Consolas,monospace}
            a{color:#2563eb;text-decoration:none} a:hover{text-decoration:underline}
          </style>
        </head>
        <body>
          <h1>Camping World Compliance Capture</h1>
          <div class="meta">
            <div><strong>Stock:</strong> {{stock}}</div>
            <div><strong>URL:</strong> <a href="{{url}}" target="_blank" rel="noopener">{{url}}</a></div>
            <div><strong>UTC:</strong> {{utc}}</div>
            <div><strong>HTTPS Date:</strong> {{hdate or 'unavailable'}}</div>
          </div>

          <div class="grid">
            <div class="card">
              <div class="title">Price Tooltip — Full Page</div>
              {% if price_exists %}
                <img src="/screenshot/{{price_id}}" alt="Price Tooltip"/>
                <p class="ok">✔ Captured</p>
                <div><strong>SHA-256:</strong> <code>{{sha_price}}</code></div>
              {% else %}
                <p class="bad">✗ Failed</p>
                <div><strong>SHA-256:</strong> N/A</div>
              {% endif %}
            </div>

            <div class="card">
              <div class="title">Payment Tooltip — Full Page</div>
              {% if payment_exists %}
                <img src="/screenshot/{{payment_id}}" alt="Payment Tooltip"/>
                <p class="ok">✔ Captured</p>
                <div><strong>SHA-256:</strong> <code>{{sha_payment}}</code></div>
              {% else %}
                <p class="bad">✗ Could not capture payment tooltip.</p>
                <div><strong>SHA-256:</strong> N/A</div>
              {% endif %}
            </div>
          </div>
        </body>
        </html>
        """,
        stock=stock, url=url, utc_now=utc_now, utc=utc_now, hdate=hdate,
        price_exists=price_exists, payment_exists=payment_exists,
        price_id=price_id, payment_id=payment_id,
        sha_price=sha_price, sha_payment=sha_payment)

        return Response(html, mimetype="text/html")

    except Exception as e:
        print("❌ /capture failed:", e, file=sys.stderr)
        traceback.print_exc()
        return Response(f"Error: {e}", status=500)

# ---------------------------- Helpers -----------------------------

def sha256_file(path: str) -> str:
    if not path or not os.path.exists(path): return "N/A"
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(1024 * 1024), b""):
            h.update(chunk)
    return h.hexdigest()

def https_date() -> str | None:
    try:
        r = requests.head("https://cloudflare.com", timeout=8)
        return r.headers.get("Date")
    except Exception:
        return None

def do_capture(stock: str) -> tuple[str | None, str | None, str]:
    url = f"https://rv.campingworld.com/rv/{stock}"
    tmpdir = tempfile.mkdtemp(prefix=f"cw-{stock}-")
    price_png_path = os.path.join(tmpdir, f"cw_{stock}_price.png")
    payment_png_path = os.path.join(tmpdir, f"cw_{stock}_payment.png")

    print(f"goto domcontentloaded: {url}")
    with sync_playwright() as p:
        browser = p.chromium.launch(
            headless=True,
            args=["--no-sandbox", "--disable-setuid-sandbox", "--disable-dev-shm-usage"],
        )
        page = browser.new_page(
            viewport={"width": 1920, "height": 1080},
            user_agent="Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome Safari",
            locale="en-US",
        )

        page.goto(url, wait_until="domcontentloaded", timeout=45_000)
        try:
            page.wait_for_load_state("networkidle", timeout=30_000)
            print("networkidle reached")
        except Exception:
            print("networkidle timeout (ignored)")

        # ZIP / reload to get Oregon finance copy
        try:
            page.evaluate("""(zip)=>{
                localStorage.setItem('cw_zip', zip);
                document.cookie = 'cw_zip='+zip+';path=/;SameSite=Lax';
            }""", OREGON_ZIP)
            print(f"ZIP injected {OREGON_ZIP}")
            try:
                page.reload(wait_until="networkidle", timeout=20_000)
                print("post-zip networkidle reached")
            except Exception as e:
                print("ZIP injection/reload issue:", e)
        except Exception as e:
            print("ZIP set failed:", e)

        # Hide overlays/chat that can block hover
        page.add_style_tag(content="""
            [id*="intercom"], [class*="livechat"], [class*="chat"], [data-testid*="modal"],
            [role="dialog"], .cf-overlay, .cf-powered-by, .cf-cta, .cf-overlay, .MuiBackdrop-root {
                display: none !important;
                visibility: hidden !important;
                opacity: 0 !important;
                pointer-events: none !important;
            }
        """)
        print("overlay-hiding CSS injected")

        # -------- PRICE TOOLTIP (works) --------
        try:
            # Use the price label in the right rail + the adjacent info icon
            price_block = page.locator("xpath=//*[contains(@class,'MuiTypography') and contains(normalize-space(.),'Total Price')]/ancestor::div[1] | //*[contains(@class,'MuiTypography-subtitle1')]")
            count = price_block.count()
            print(f"price candidates={count}")
            idx = max(0, count - 1)
            target = price_block.nth(idx)
            text = (target.text_content() or "").strip()
            print(f"price use idx={idx} text='{text}'")

            target.scroll_into_view_if_needed(timeout=5000)

            info_icon = target.locator("xpath=following::*[name()='svg' and contains(@class,'MuiSvgIcon-root')][1]")
            trigger = info_icon.first if info_icon.count() > 0 else target
            if info_icon.count() > 0:
                print("price: using adjacent info icon")
            else:
                print("price: using text as trigger")

            _synthetic_hover(page, trigger)
            page.wait_for_selector("[role='tooltip'], .MuiTooltip-popper, .base-Popper-root", state="visible", timeout=8000)
            print("price tooltip appeared")
            page.wait_for_timeout(400)
            page.screenshot(path=price_png_path, full_page=True)
            print(f"price screenshot saved {price_png_path} size={os.path.getsize(price_png_path)} bytes")
        except Exception as e:
            print("ERROR price:", e)
            price_png_path = None

        # -------- PAYMENT TOOLTIP (robust) --------
        try:
            anchor = page.locator(
                "xpath=//*[contains(normalize-space(.), '/mo') or contains(translate(normalize-space(.),'PAYMENT','payment') , 'payment')]"
            ).first
            anchor.scroll_into_view_if_needed(timeout=5000)
            print("payment anchor scrolled into view")

            # Prefer the nearby info icon; otherwise hover the text itself
            icon = anchor.locator("xpath=following::*[name()='svg' and contains(@class,'MuiSvgIcon-root')][1]")
            trigger = icon.first if icon.count() > 0 else anchor
            if icon.count() > 0:
                print("payment: using adjacent info icon")
            else:
                print("payment: using text as trigger")

            _synthetic_hover(page, trigger)
            page.wait_for_selector(
                "[role='tooltip'], .MuiTooltip-popper, .MuiTooltip-popperInteractive, .base-Popper-root",
                state="visible",
                timeout=8000
            )
            print("payment tooltip appeared")
            page.wait_for_timeout(400)
            page.screenshot(path=payment_png_path, full_page=True)
            print(f"payment screenshot saved {payment_png_path} size={os.path.getsize(payment_png_path)} bytes")
        except Exception as e:
            print("ERROR payment:", e)
            payment_png_path = None

        browser.close()

        # Final log of file presence
        if price_png_path and os.path.exists(price_png_path):
            print(f"price file: exists ({os.path.getsize(price_png_path)} bytes)")
        else:
            print("price file: missing")
        if payment_png_path and os.path.exists(payment_png_path):
            print(f"payment file: exists ({os.path.getsize(payment_png_path)} bytes)")
        else:
            print("payment file: missing")

    return price_png_path, payment_png_path, url

# Synthetic hover helper (fires multiple events so MUI tooltip opens reliably)
def _synthetic_hover(page, element_handle):
    page.evaluate("""
        (el)=>{
            const events = ['pointerover','mouseover','mouseenter','mousemove','focus','pointerenter'];
            for(const t of events){
                el.dispatchEvent(new MouseEvent(t,{bubbles:true,cancelable:true,view:window}));
            }
        }
    """, element_handle)

# ---------------------------- Entrypoint ----------------------------

if __name__ == "__main__":
    app.run(host="0.0.0.0", port=PORT)
