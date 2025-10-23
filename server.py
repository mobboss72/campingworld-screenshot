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
        sha_price = sha256_file(price_png_path)
        sha_payment = sha256_file(payment_png_path)

        # Render template with images side by side
        html = render_template_string("""
            <!DOCTYPE html>
            <html>
            <head>
                <title>Compliance Screenshots</title>
                <style>
                    body { font-family: Arial, sans-serif; margin: 20px; }
                    .container { display: flex; flex-wrap: wrap; gap: 20px; }
                    .image-box { border: 1px solid #ccc; padding: 10px; }
                    .info { margin-top: 20px; font-size: 0.9em; }
                </style>
            </head>
            <body>
                <h2>Camping World Proof (Compliance Capture)</h2>
                <div class="container">
                    <div class="image-box">
                        <h3>Price Hover Screenshot</h3>
                        <img src="data:image/png;base64,{{ price_base64 }}" alt="Price Hover">
                        <p>Stock: {{ stock }}</p>
                        <p>URL: {{ url }}</p>
                        <p>UTC: {{ utc_now }}</p>
                        <p>HTTPS Date: {{ hdate or 'unavailable' }}</p>
                        <p>SHA-256: {{ sha_price }}</p>
                    </div>
                    <div class="image-box">
                        <h3>Payment Hover Screenshot</h3>
                        <img src="data:image/png;base64,{{ payment_base64 }}" alt="Payment Hover">
                        <p>Stock: {{ stock }}</p>
                        <p>URL: {{ url }}</p>
                        <p>UTC: {{ utc_now }}</p>
                        <p>HTTPS Date: {{ hdate or 'unavailable' }}</p>
                        <p>SHA-256: {{ sha_payment }}</p>
                    </div>
                </div>
            </body>
            </html>
        """, 
        stock=stock, 
        url=url, 
        utc_now=utc_now, 
        hdate=hdate,
        price_base64=encode_image_to_base64(price_png_path),
        payment_base64=encode_image_to_base64(payment_png_path),
        sha_price=sha_price,
        sha_payment=sha_payment)

        return Response(html, mimetype="text/html")

    except Exception as e:
        print("❌ /capture failed:", e, file=sys.stderr)
        traceback.print_exc()
        return Response(f"Error: {e}", status=500)

# Helpers
def sha256_file(path: str) -> str:
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
    import base64
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

        # Load unit page
        page.goto(url, wait_until="domcontentloaded")
        try:
            page.wait_for_load_state("networkidle", timeout=30_000)
        except Exception:
            pass

        # Set Oregon ZIP
        try:
            page.evaluate(
                "(zip)=>{try{localStorage.setItem('cw_zip',zip);}catch(e){};document.cookie='cw_zip='+zip+';path=/;SameSite=Lax';}",
                OREGON_ZIP,
            )
            page.reload(wait_until="domcontentloaded")
            try:
                page.wait_for_load_state("networkidle", timeout=20_000)
            except Exception:
                pass
        except Exception:
            pass

        # Capture price hover screenshot
        price_element = page.query_selector(".price-block a")
        if price_element:
            with page.expect_popup() as popup_info:
                price_element.hover()
            popup = popup_info.value
            popup.wait_for_load_state("networkidle")
            popup.screenshot(path=price_png_path)

        # Capture payment hover screenshot
        payment_element = page.query_selector(".est-payment-block a")
        if payment_element:
            with page.expect_popup() as popup_info:
                payment_element.hover()
            popup = popup_info.value
            popup.wait_for_load_state("networkidle")
            popup.screenshot(path=payment_png_path)

        browser.close()

    return price_png_path, payment_png_path, url

if __name__ == "__main__":
    app.run(host="0.0.0.0", port=PORT)
