# server.py
import os
import io
import sys
import zipfile
import hashlib
import datetime
import tempfile
import traceback
import requests
import base64

from flask import Flask, request, send_from_directory, Response, render_template_string
from playwright.sync_api import sync_playwright

# Set Playwright browsers path to a writable, persisted location under /app
os.environ.setdefault("PLAYWRIGHT_BROWSERS_PATH", "/app/ms-playwright")

PORT = int(os.getenv("PORT", "8080"))
OREGON_ZIP = os.getenv("OREGON_ZIP", "97201")

# Serve index.html from the repo root
app = Flask(__name__, static_folder=None)

# Routes
@app.get("/")
def root():
    return send_from_directory(".", "index.html")

@app.post("/capture")
def capture():
    try:
        stock = (request.form.get("stock") or "").strip()
        if not stock.isdigit():
            return Response("Invalid stock number", status=400)

        # Capture screenshots with hover states
        price_png_path, payment_png_path, url = do_capture(stock)

        utc_now = datetime.datetime.utcnow().strftime("%Y-%m-%d %H:%M:%S UTC")
        hdate = https_date()
        sha_price = sha256_file(price_png_path) if os.path.exists(price_png_path) else "N/A (Capture failed)"
        sha_payment = sha256_file(payment_png_path) if os.path.exists(payment_png_path) else "N/A (Capture failed)"

        # Render template with images side by side, handling missing files
        price_base64 = encode_image_to_base64(price_png_path) if os.path.exists(price_png_path) else ""
        payment_base64 = encode_image_to_base64(payment_png_path) if os.path.exists(payment_png_path) else ""

        html = render_template_string('''
            <!DOCTYPE html>
            <html>
            <head>
                <title>Compliance Screenshots</title>
                <style>
                    body { font-family: Arial, sans-serif; margin: 20px; }
                    .container { display: flex; flex-wrap: wrap; gap: 20px; }
                    .image-box { border: 1px solid #ccc; padding: 10px; }
                    .info { margin-top: 20px; font-size: 0.9em; }
                    .error { color: red; }
                </style>
            </head>
            <body>
                <h2>Camping World Proof (Compliance Capture)</h2>
                <div class="container">
                    <div class="image-box">
                        <h3>Price Hover Screenshot</h3>
                        {% if price_base64 %}
                            <img src="data:image/png;base64,{{ price_base64 }}" alt="Price Hover">
                        {% else %}
                            <p class="error">Failed to capture Price Hover screenshot.</p>
                        {% endif %}
                        <p>Stock: {{ stock }}</p>
                        <p>URL: {{ url }}</p>
                        <p>UTC: {{ utc_now }}</p>
                        <p>HTTPS Date: {{ hdate or 'unavailable' }}</p>
                        <p>SHA-256: {{ sha_price }}</p>
                    </div>
                    <div class="image-box">
                        <h3>Payment Hover Screenshot</h3>
                        {% if payment_base64 %}
                            <img src="data:image/png;base64,{{ payment_base64 }}" alt="Payment Hover">
                        {% else %}
                            <p class="error">Failed to capture Payment Hover screenshot.</p>
                        {% endif %}
                        <p>Stock: {{ stock }}</p>
                        <p>URL: {{ url }}</p>
                        <p>UTC: {{ utc_now }}</p>
                        <p>HTTPS Date: {{ hdate or 'unavailable' }}</p>
                        <p>SHA-256: {{ sha_payment }}</p>
                    </div>
                </div>
            </body>
            </html>
        ''', 
        stock=stock, 
        url=url, 
        utc_now=utc_now, 
        hdate=hdate,
        price_base64=price_base64,
        payment_base64=payment_base64,
        sha_price=sha_price,
        sha_payment=sha_payment)

        return Response(html, mimetype="text/html")

    except Exception as e:
        print("❌ /capture failed:", e, file=sys.stderr)
        traceback.print_exc()
        return Response(f"Error: {e}", status=500)

# Helpers
def sha256_file(path: str) -> str:
    if not os.path.exists(path):
        return "N/A (File not found)"
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

def encode_image_to_base64(path: str) -> str:
    if not os.path.exists(path):
        return ""
    with open(path, "rb") as image_file:
        return base64.b64encode(image_file.read()).decode('utf-8')

def do_capture(stock: str) -> tuple[str, str, str]:
    url = f"https://rv.campingworld.com/rv/{stock}"
    tmpdir = tempfile.mkdtemp(prefix=f"cw-{stock}-")
    price_png_path = os.path.join(tmpdir, f"cw_{stock}_price.png")
    payment_png_path = os.path.join(tmpdir, f"cw_{stock}_payment.png")

    with sync_playwright() as p:
        browser = p.chromium.launch(
            headless=True,
            args=[
                "--no-sandbox",
                "--disable-setuid-sandbox",
                "--disable-dev-shm-usage",
            ],
        )
        ctx = browser.new_context(
            locale="en-US",
            geolocation={"latitude": 45.5122, "longitude": -122.6587},
            permissions=["geolocation"],
            viewport={"width": 1920, "height": 1080},
            user_agent="Mozilla/5.0 Chrome",
        )
        page = ctx.new_page()

        # Load unit page and ensure interactivity
        page.goto(url, wait_until="domcontentloaded")
        try:
            page.wait_for_load_state("networkidle", timeout=30_000)
        except Exception as e:
            print(f"Load failed: {e}")

        # Set Oregon ZIP
        try:
            page.evaluate('''(zip) => {
                try {
                    localStorage.setItem('cw_zip', zip);
                } catch (e) {}
                document.cookie = 'cw_zip=' + zip + ';path=/;SameSite=Lax';
            }''', OREGON_ZIP)
            page.reload(wait_until="domcontentloaded")
            try:
                page.wait_for_load_state("networkidle", timeout=20_000)
            except Exception as e:
                print(f"Reload failed: {e}")
        except Exception as e:
            print(f"ZIP set failed: {e}")

        # Wait for price and payment elements
        try:
            page.wait_for_selector(".MuiTypography-root.MuiTypography-subtitle1", state="visible", timeout=10_000)
            page.wait_for_selector(".est-payment-block", state="visible", timeout=10_000)
        except Exception as e:
            print(f"Selector wait failed: {e}")

        # Capture price hover screenshot
        price_selector = ".MuiTypography-root.MuiTypography-subtitle1:visible"
        price_elements = page.locator(price_selector)
        print(f"Number of price elements found: {price_elements.count()}")
        visible_price = None
        for i in range(price_elements.count()):
            elem = price_elements.nth(i)
            if elem.is_visible():
                visible_price = elem
                print(f"Visible price element found at index {i}")
                break
        if visible_price:
            try:
                visible_price.scroll_into_view_if_needed(timeout=5000)
                visible_price.hover(timeout=10000, force=True)
                page.wait_for_timeout(1000)  # Wait for tooltip to appear
                tooltip_selector = ".MuiTooltip-tooltip:visible"
                tooltip = page.locator(tooltip_selector).first
                if tooltip and tooltip.is_visible():
                    tooltip.screenshot(path=price_png_path)
                    print(f"Price tooltip screenshot saved to: {price_png_path}")
                else:
                    print("No visible tooltip found for price")
            except Exception as e:
                print(f"Price hover failed: {e}")
        else:
            print("No visible price element found")

        # Capture payment hover screenshot
        payment_selector = ".est-payment-block:visible"
        payment_elements = page.locator(payment_selector)
        print(f"Number of payment elements found: {payment_elements.count()}")
        visible_payment = None
        for i in range(payment_elements.count()):
            elem = payment_elements.nth(i)
            if elem.is_visible():
                visible_payment = elem
                print(f"Visible payment element found at index {i}")
                break
        if visible_payment:
            try:
                visible_payment.scroll_into_view_if_needed(timeout=5000)
                visible_payment.hover(timeout=10000, force=True)
                page.wait_for_timeout(1000)  # Wait for tooltip to appear
                tooltip_selector = ".MuiTooltip-tooltip:visible"
                tooltip = page.locator(tooltip_selector).first
                if tooltip and tooltip.is_visible():
                    tooltip.screenshot(path=payment_png_path)
                    print(f"Payment tooltip screenshot saved to: {payment_png_path}")
                else:
                    print("No visible tooltip found for payment")
            except Exception as e:
                print(f"Payment hover failed: {e}")
        else:
            print("No visible payment element found")

        browser.close()

    return price_png_path, payment_png_path, url

if __name__ == "__main__":
    app.run(host="0.0.0.0", port=PORT)
