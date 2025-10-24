# server.py
import os
import sys
import hashlib
import datetime
import tempfile
import traceback
import requests
from pathlib import Path

from flask import Flask, request, send_from_directory, Response, render_template_string, send_file
from playwright.sync_api import sync_playwright, TimeoutError as PlaywrightTimeout

# Persist Playwright browsers in a writable path (Railway/Render friendly)
os.environ.setdefault("PLAYWRIGHT_BROWSERS_PATH", "/app/ms-playwright")

PORT = int(os.getenv("PORT", "8080"))
OREGON_ZIP = os.getenv("OREGON_ZIP", "97201")

# Simple in-memory path cache for screenshots
screenshot_cache = {}

app = Flask(__name__, static_folder=None)

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

        price_png_path, payment_png_path, url = do_capture(stock)

        utc_now = datetime.datetime.utcnow().strftime("%Y-%m-%d %H:%M:%S UTC")
        hdate = https_date()

        price_exists = os.path.exists(price_png_path)
        payment_exists = os.path.exists(payment_png_path)

        print("\n=== File Status ===")
        print(f"Price screenshot exists: {price_exists} -> {price_png_path}")
        if price_exists:
            print(f"  size: {os.path.getsize(price_png_path)} bytes")
        print(f"Payment screenshot exists: {payment_exists} -> {payment_png_path}")
        if payment_exists:
            print(f"  size: {os.path.getsize(payment_png_path)} bytes")

        sha_price = sha256_file(price_png_path) if price_exists else "N/A (Capture failed)"
        sha_payment = sha256_file(payment_png_path) if payment_exists else "N/A (Capture failed)"

        ts = int(datetime.datetime.utcnow().timestamp())
        price_id = f"price_{stock}_{ts}"
        payment_id = f"payment_{stock}_{ts}"
        if price_exists:
            screenshot_cache[price_id] = price_png_path
        if payment_exists:
            screenshot_cache[payment_id] = payment_png_path

        html = render_template_string("""
            <!DOCTYPE html>
            <html>
            <head>
                <title>Compliance Screenshots</title>
                <meta charset="UTF-8">
                <style>
                    body { font-family: Arial, sans-serif; margin: 20px; background: #f5f5f5; }
                    h2 { text-align: center; margin-bottom: 30px; }
                    .container { display: flex; flex-wrap: wrap; gap: 20px; justify-content: center; }
                    .image-box { border: 2px solid #333; padding: 15px; background: white; box-shadow: 0 2px 4px rgba(0,0,0,0.1); max-width: 600px; }
                    .image-box h3 { margin-top: 0; border-bottom: 2px solid #333; padding-bottom: 10px; }
                    .info { margin-top: 15px; font-size: 0.85em; line-height: 1.6; }
                    .info p { margin: 5px 0; word-wrap: break-word; }
                    .error { color: red; font-weight: bold; padding: 20px; background: #fee; border: 1px solid red; border-radius: 4px; }
                    .success { color: green; font-weight: bold; }
                    img { max-width: 100%; height: auto; border: 1px solid #ddd; display: block; margin: 10px 0; }
                    .screenshot-container { min-height: 200px; background: #fafafa; padding: 10px; border-radius: 4px; }
                </style>
            </head>
            <body>
                <h2>Camping World Proof (Compliance Capture)</h2>
                <div class="container">
                    <div class="image-box">
                        <h3>Price Hover — Full Page</h3>
                        <div class="screenshot-container">
                            {% if price_exists %}
                                <img src="/screenshot/{{ price_id }}" alt="Price Hover Full Page" />
                                <p class="success">&#10003; Screenshot captured successfully</p>
                            {% else %}
                                <p class="error">&#10007; Failed to capture Price Hover screenshot.</p>
                            {% endif %}
                        </div>
                        <div class="info">
                            <p><strong>Stock:</strong> {{ stock }}</p>
                            <p><strong>URL:</strong> <a href="{{ url }}" target="_blank">{{ url }}</a></p>
                            <p><strong>UTC:</strong> {{ utc_now }}</p>
                            <p><strong>HTTPS Date:</strong> {{ hdate or 'unavailable' }}</p>
                            <p><strong>SHA-256:</strong> <code>{{ sha_price }}</code></p>
                        </div>
                    </div>
                    <div class="image-box">
                        <h3>Payment Hover — Full Page</h3>
                        <div class="screenshot-container">
                            {% if payment_exists %}
                                <img src="/screenshot/{{ payment_id }}" alt="Payment Hover Full Page" />
                                <p class="success">&#10003; Screenshot captured successfully</p>
                            {% else %}
                                <p class="error">&#10007; Failed to capture Payment Hover screenshot.</p>
                            {% endif %}
                        </div>
                        <div class="info">
                            <p><strong>Stock:</strong> {{ stock }}</p>
                            <p><strong>URL:</strong> <a href="{{ url }}" target="_blank">{{ url }}</a></p>
                            <p><strong>UTC:</strong> {{ utc_now }}</p>
                            <p><strong>HTTPS Date:</strong> {{ hdate or 'unavailable' }}</p>
                            <p><strong>SHA-256:</strong> <code>{{ sha_payment }}</code></p>
                        </div>
                    </div>
                </div>
            </body>
            </html>
        """,
        stock=stock, url=url, utc_now=utc_now, hdate=hdate,
        price_exists=price_exists, payment_exists=payment_exists,
        price_id=price_id, payment_id=payment_id,
        sha_price=sha_price, sha_payment=sha_payment)

        return Response(html, mimetype="text/html")
    except Exception as e:
        print("❌ /capture failed:", e, file=sys.stderr)
        traceback.print_exc()
        return Response(f"Error: {e}", status=500)

def sha256_file(path: str) -> str:
    if not os.path.exists(path):
        return "N/A (File not found)"
    try:
        h = hashlib.sha256()
        with open(path, "rb") as f:
            for chunk in iter(lambda: f.read(1024 * 1024), b""):
                h.update(chunk)
        return h.hexdigest()
    except Exception as e:
        print(f"Error calculating SHA-256: {e}")
        return f"N/A (Error: {e})"

def https_date() -> str | None:
    try:
        r = requests.head("https://cloudflare.com", timeout=8)
        return r.headers.get("Date")
    except Exception:
        return None

def do_capture(stock: str) -> tuple[str, str, str]:
    """Capture price and payment hover screenshots for a stock page."""
    url = f"https://rv.campingworld.com/rv/{stock}"
    tmpdir = tempfile.mkdtemp(prefix=f"cw-{stock}-")
    price_png_path = os.path.join(tmpdir, f"cw_{stock}_price.png")
    payment_png_path = os.path.join(tmpdir, f"cw_{stock}_payment.png")

    print(f"\n=== Starting capture for stock {stock} ===")
    print(f"Temp directory: {tmpdir}")
    print(f"Price path: {price_png_path}")
    print(f"Payment path: {payment_png_path}")

    with sync_playwright() as p:
        browser = p.chromium.launch(
            headless=True,
            args=["--no-sandbox", "--disable-setuid-sandbox", "--disable-dev-shm-usage"],
        )
        ctx = browser.new_context(
            locale="en-US",
            geolocation={"latitude": 45.5122, "longitude": -122.6587},
            permissions=["geolocation"],
            viewport={"width": 1920, "height": 1080},
            user_agent="Mozilla/5.0 Chrome",
        )
        page = ctx.new_page()

        print(f"Loading URL: {url}")
        page.goto(url, wait_until="domcontentloaded")
        try:
            page.wait_for_load_state("networkidle", timeout=30_000)
            print("Page loaded (networkidle)")
        except PlaywrightTimeout as e:
            print(f"Load networkidle timeout: {e}")

        # Set Oregon ZIP and reload so pricing/payments reflect OR
        try:
            page.evaluate(
                """(zip) => {
                    try { localStorage.setItem('cw_zip', zip); } catch {}
                    document.cookie = 'cw_zip=' + zip + ';path=/;SameSite=Lax';
                }""",
                OREGON_ZIP,
            )
            print(f"Set ZIP code to: {OREGON_ZIP}")
            page.reload(wait_until="domcontentloaded")
            try:
                page.wait_for_load_state("networkidle", timeout=20_000)
            except PlaywrightTimeout as e:
                print(f"Reload networkidle timeout: {e}")
        except Exception as e:
            print(f"ZIP set failed: {e}")

        # Wait for price to exist
        try:
            page.wait_for_selector(".MuiTypography-root.MuiTypography-subtitle1", state="visible", timeout=15_000)
            print("Price element found and visible")
        except PlaywrightTimeout as e:
            print(f"Price selector wait failed: {e}")

        # ===== PRICE HOVER (working) =====
        print("\n=== Capturing Price Hover ===")
        try:
            price_selector = ".MuiTypography-root.MuiTypography-subtitle1:visible"
            price_elements = page.locator(price_selector)
            count = price_elements.count()
            print(f"Found {count} price elements")

            visible_price = None
            for i in range(count):
                elem = price_elements.nth(i)
                if elem.is_visible():
                    visible_price = elem
                    print(f"Using visible price element at index {i}")
                    break

            if visible_price:
                visible_price.scroll_into_view_if_needed(timeout=5000)
                visible_price.hover(timeout=10000, force=True)
                page.wait_for_timeout(1500)
                page.screenshot(path=price_png_path, full_page=True)
                if os.path.exists(price_png_path):
                    print(f"✅ Price screenshot saved: {os.path.getsize(price_png_path)} bytes")
                else:
                    print("❌ Price screenshot file not created")
            else:
                print("❌ No visible price element found")
        except Exception as e:
            print(f"❌ Price hover capture failed: {e}")
            traceback.print_exc()

        # ===== PAYMENT HOVER (fixed + more robust) =====
        print("\n=== Capturing Payment Hover ===")

        try:
            # 1) Wait for any likely payment text to appear
            #    Try multiple find strategies because markup varies.
            payment_locator_candidates = [
                # Common MUI subtitle for the line that shows “$xxx/mo”
                ".MuiTypography-root.MuiTypography-subtitle2",
                # Any text mentioning payment or /mo
                "text=/payment|/mo|monthly/i",
                # A more specific selector sometimes used near the price row
                "[data-testid*=payment], [id*=payment], [class*=payment]",
            ]

            visible_payment = None
            for sel in payment_locator_candidates:
                try:
                    page.wait_for_selector(sel, state="visible", timeout=6000)
                    loc = page.locator(sel)
                    count = loc.count()
                    print(f"Selector '{sel}' visible; count={count}")
                    for i in range(count):
                        cand = loc.nth(i)
                        if not cand.is_visible():
                            continue
                        txt = (cand.text_content() or "").lower()
                        if any(key in txt for key in ["payment", "/mo", "monthly"]):
                            visible_payment = cand
                            print(f"Using payment element from '{sel}' at index {i} with text: {txt[:120]!r}")
                            break
                    if visible_payment:
                        break
                except PlaywrightTimeout:
                    print(f"Selector '{sel}' not visible within timeout")

            if not visible_payment:
                print("❌ Payment element not found by text/selector heuristics")
            else:
                # 2) Find the real hover trigger: prefer an adjacent info icon if present
                trigger = visible_payment
                icon = visible_payment.locator("xpath=following::*[contains(@class,'MuiSvgIcon-root')][1]")
                if icon.count() > 0 and icon.first.is_visible():
                    trigger = icon.first
                    print("Hover trigger resolved to adjacent info icon")
                else:
                    print("Hover trigger is the payment text element")

                # Make sure it’s on screen and not covered
                trigger.scroll_into_view_if_needed(timeout=5000)

                # Some overlays (chat widget) can steal hover; try to hide them gently
                try:
                    page.evaluate("""
                        () => {
                            const el = document.querySelector('#crisp-chatbox, .crisp-client');
                            if (el) el.style.display = 'none';
                        }
                    """)
                except Exception:
                    pass

                # 3) Do a physical mouse hover (more reliable than .hover() on some portals)
                box = trigger.bounding_box()
                if box:
                    page.mouse.move(box["x"] + box["width"]/2, box["y"] + box["height"]/2)
                try:
                    trigger.hover(force=True, timeout=10_000)
                except Exception as e:
                    print(f".hover() raised {e}; relying on mouse.move only")

                # 4) Wait for a tooltip/popover attached to body
                tooltip = page.locator("[role=tooltip], .MuiTooltip-popper, [data-popper-placement], .MuiPopover-root, .MuiTooltip-tooltip")
                try:
                    tooltip.wait_for(state="visible", timeout=5000)
                    print("Tooltip/popover became visible")
                except PlaywrightTimeout:
                    # As a fallback, nudge the mouse slightly to keep hover active and try again briefly
                    if box:
                        page.mouse.move(box["x"] + box["width"]/2 + 2, box["y"] + box["height"]/2 + 2)
                    try:
                        tooltip.wait_for(state="visible", timeout=2000)
                        print("Tooltip visible after slight mouse nudge")
                    except PlaywrightTimeout:
                        print("❌ Tooltip did not appear; proceeding to screenshot anyway")

                page.wait_for_timeout(500)  # settle
                page.screenshot(path=payment_png_path, full_page=True)
                if os.path.exists(payment_png_path):
                    print(f"✅ Payment screenshot saved: {os.path.getsize(payment_png_path)} bytes")
                else:
                    print("❌ Payment screenshot not found after capture")

        except Exception as e:
            print(f"❌ Payment hover capture failed: {e}")
            traceback.print_exc()

        browser.close()
        print("\n=== Browser closed ===")

    return price_png_path, payment_png_path, url

if __name__ == "__main__":
    app.run(host="0.0.0.0", port=PORT)
