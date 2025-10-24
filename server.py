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
from pathlib import Path

from flask import Flask, request, send_from_directory, Response, render_template_string, send_file
from playwright.sync_api import sync_playwright

# Set Playwright browsers path to a writable, persisted location under /app
os.environ.setdefault("PLAYWRIGHT_BROWSERS_PATH", "/app/ms-playwright")

PORT = int(os.getenv("PORT", "8080"))
OREGON_ZIP = os.getenv("OREGON_ZIP", "97201")

# Global storage for screenshot paths (in production, use a database)
screenshot_cache = {}

# Serve index.html from the repo root
app = Flask(__name__, static_folder=None)

# Routes
@app.get("/")
def root():
    return send_from_directory(".", "index.html")

@app.get("/screenshot/<screenshot_id>")
def serve_screenshot(screenshot_id):
    """Serve a screenshot image file"""
    if screenshot_id not in screenshot_cache:
        return Response("Screenshot not found", status=404)
    
    file_path = screenshot_cache[screenshot_id]
    if not os.path.exists(file_path):
        return Response("Screenshot file not found", status=404)
    
    return send_file(file_path, mimetype='image/png')

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
        
        # Check if files exist and get their info
        price_exists = os.path.exists(price_png_path)
        payment_exists = os.path.exists(payment_png_path)
        
        print(f"\n=== File Status ===")
        print(f"Price screenshot exists: {price_exists}")
        if price_exists:
            print(f"  Path: {price_png_path}")
            print(f"  Size: {os.path.getsize(price_png_path)} bytes")
        print(f"Payment screenshot exists: {payment_exists}")
        if payment_exists:
            print(f"  Path: {payment_png_path}")
            print(f"  Size: {os.path.getsize(payment_png_path)} bytes")
        
        sha_price = sha256_file(price_png_path) if price_exists else "N/A (Capture failed)"
        sha_payment = sha256_file(payment_png_path) if payment_exists else "N/A (Capture failed)"

        # Store paths in cache for serving
        price_id = f"price_{stock}_{int(datetime.datetime.utcnow().timestamp())}"
        payment_id = f"payment_{stock}_{int(datetime.datetime.utcnow().timestamp())}"
        
        if price_exists:
            screenshot_cache[price_id] = price_png_path
        if payment_exists:
            screenshot_cache[payment_id] = payment_png_path

        html = render_template_string('''
            <!DOCTYPE html>
            <html>
            <head>
                <title>Compliance Screenshots</title>
                <meta charset="UTF-8">
                <style>
                    body { 
                        font-family: Arial, sans-serif; 
                        margin: 20px; 
                        background: #f5f5f5;
                    }
                    h2 {
                        text-align: center;
                        margin-bottom: 30px;
                    }
                    .container { 
                        display: flex; 
                        flex-wrap: wrap; 
                        gap: 20px; 
                        justify-content: center;
                    }
                    .image-box { 
                        border: 2px solid #333; 
                        padding: 15px; 
                        background: white;
                        box-shadow: 0 2px 4px rgba(0,0,0,0.1);
                        max-width: 600px;
                    }
                    .image-box h3 {
                        margin-top: 0;
                        border-bottom: 2px solid #333;
                        padding-bottom: 10px;
                    }
                    .info { 
                        margin-top: 15px; 
                        font-size: 0.85em;
                        line-height: 1.6;
                    }
                    .info p {
                        margin: 5px 0;
                        word-wrap: break-word;
                    }
                    .error { 
                        color: red; 
                        font-weight: bold;
                        padding: 20px;
                        background: #fee;
                        border: 1px solid red;
                        border-radius: 4px;
                    }
                    .success {
                        color: green;
                        font-weight: bold;
                    }
                    img { 
                        max-width: 100%; 
                        height: auto; 
                        border: 1px solid #ddd;
                        display: block;
                        margin: 10px 0;
                    }
                    .screenshot-container {
                        min-height: 200px;
                        background: #fafafa;
                        padding: 10px;
                        border-radius: 4px;
                    }
                </style>
            </head>
            <body>
                <h2>Camping World Proof (Compliance Capture)</h2>
                <div class="container">
                    <div class="image-box">
                        <h3>Price Hover Full Page Screenshot</h3>
                        <div class="screenshot-container">
                            {% if price_exists %}
                                <img src="/screenshot/{{ price_id }}" alt="Price Hover Full Page" />
                                <p class="success">✓ Screenshot captured successfully</p>
                            {% else %}
                                <p class="error">✗ Failed to capture Price Hover screenshot.</p>
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
                        <h3>Payment Hover Full Page Screenshot</h3>
                        <div class="screenshot-container">
                            {% if payment_exists %}
                                <img src="/screenshot/{{ payment_id }}" alt="Payment Hover Full Page" />
                                <p class="success">✓ Screenshot captured successfully</p>
                            {% else %}
                                <p class="error">✗ Failed to capture Payment Hover screenshot.</p>
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
        ''', 
        stock=stock, 
        url=url, 
        utc_now=utc_now, 
        hdate=hdate,
        price_exists=price_exists,
        payment_exists=payment_exists,
        price_id=price_id,
        payment_id=payment_id,
        sha_price=sha_price,
        sha_payment=sha_payment)

        return Response(html, mimetype="text/html")

    except Exception as e:
        print("❌ /capture failed:", e, file=sys.stderr)
        traceback.print_exc()
        return Response(f"Error: {e}", status=500)

# Helpers
def sha256_file(path: str) -> str:
    """Calculate SHA-256 hash of a file"""
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
    """Get current date from HTTPS headers"""
    try:
        r = requests.head("https://cloudflare.com", timeout=8)
        return r.headers.get("Date")
    except Exception:
        return None

def do_capture(stock: str) -> tuple[str, str, str]:
    """Capture price and payment hover screenshots"""
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
        print(f"Loading URL: {url}")
        page.goto(url, wait_until="domcontentloaded")
        try:
            page.wait_for_load_state("networkidle", timeout=30_000)
            print("Page loaded (networkidle)")
        except Exception as e:
            print(f"Load networkidle timeout: {e}")

        # Set Oregon ZIP
        try:
            page.evaluate('''(zip) => {
                try {
                    localStorage.setItem('cw_zip', zip);
                } catch (e) {}
                document.cookie = 'cw_zip=' + zip + ';path=/;SameSite=Lax';
            }''', OREGON_ZIP)
            print(f"Set ZIP code to: {OREGON_ZIP}")
            page.reload(wait_until="domcontentloaded")
            try:
                page.wait_for_load_state("networkidle", timeout=20_000)
            except Exception as e:
                print(f"Reload networkidle timeout: {e}")
        except Exception as e:
            print(f"ZIP set failed: {e}")

        # Wait for key elements
        try:
            page.wait_for_selector(".MuiTypography-root.MuiTypography-subtitle1", state="visible", timeout=15_000)
            print("Price element found and visible")
        except Exception as e:
            print(f"Price selector wait failed: {e}")

        # ===== CAPTURE PRICE HOVER SCREENSHOT (WORKING - DON'T CHANGE) =====
        print("\n=== Capturing Price Hover ===")
        price_selector = ".MuiTypography-root.MuiTypography-subtitle1:visible"
        try:
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
                print("Scrolled to price element")
                visible_price.hover(timeout=10000, force=True)
                print("Hovering over price element")
                page.wait_for_timeout(1500)
                page.screenshot(path=price_png_path, full_page=True)
                
                if os.path.exists(price_png_path):
                    size = os.path.getsize(price_png_path)
                    print(f"✅ Price screenshot saved: {size} bytes")
                else:
                    print("❌ Price screenshot file not created")
            else:
                print("❌ No visible price element found")
        except Exception as e:
            print(f"❌ Price hover capture failed: {e}")
            traceback.print_exc()

        # ===== CAPTURE PAYMENT HOVER SCREENSHOT (SIMPLIFIED TO MIRROR PRICE) =====
        print("\n=== Capturing Payment Hover ===")
        
        # Use the EXACT same simple approach as price hover
        # Just find ANY MuiTypography element with "payment" text
        try:
            # Wait for payment elements to be available
            page.wait_for_timeout(2000)  # Give page time to settle
            
            # Find all MuiTypography elements
            all_typography = page.locator(".MuiTypography-root:visible")
            count = all_typography.count()
            print(f"Found {count} total MuiTypography elements")
            
            visible_payment = None
            for i in range(count):
                elem = all_typography.nth(i)
                try:
                    if elem.is_visible():
                        text = elem.inner_text().lower()
                        # Look for payment-related text
                        if 'payment' in text or '/mo' in text:
                            print(f"Found payment element at index {i}: {text[:50]}")
                            visible_payment = elem
                            break
                except:
                    continue
            
            if visible_payment:
                visible_payment.scroll_into_view_if_needed(timeout=5000)
                print("Scrolled to payment element")
                visible_payment.hover(timeout=10000, force=True)
                print("Hovering over payment element")
                page.wait_for_timeout(2000)  # Slightly longer wait for payment tooltip
                page.screenshot(path=payment_png_path, full_page=True)
                
                if os.path.exists(payment_png_path):
                    size = os.path.getsize(payment_png_path)
                    print(f"✅ Payment screenshot saved: {size} bytes")
                else:
                    print("❌ Payment screenshot file not created")
            else:
                print("❌ No visible payment element found")
                
        except Exception as e:
            print(f"❌ Payment hover capture failed: {e}")
            traceback.print_exc()

        browser.close()
        print("\n=== Browser closed ===")

    return price_png_path, payment_png_path, url

if __name__ == "__main__":
    app.run(host="0.0.0.0", port=PORT)
