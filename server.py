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

# Persist Playwright browsers in a writable path
os.environ.setdefault("PLAYWRIGHT_BROWSERS_PATH", "/app/ms-playwright")

PORT = int(os.getenv("PORT", "8080"))
OREGON_ZIP = os.getenv("OREGON_ZIP", "97201")
DATA_DIR = Path("/app/data")
DATA_DIR.mkdir(parents=True, exist_ok=True)
VERSION_FILE = DATA_DIR / "version.txt"

# --- Version handling ---
def get_version() -> str:
    if not VERSION_FILE.exists():
        VERSION_FILE.write_text("1.0.0")
    ver = VERSION_FILE.read_text().strip()
    parts = ver.split(".")
    if len(parts) == 3:
        parts[2] = str(int(parts[2]) + 1)
    else:
        ver = "1.0.0"
    new_ver = ".".join(parts)
    VERSION_FILE.write_text(new_ver)
    return new_ver

# Simple in-memory cache
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
        version = get_version()
        price_png_path, payment_png_path, url = do_capture(stock, version)

        utc_now = datetime.datetime.utcnow().strftime("%Y-%m-%d %H:%M:%S UTC")
        hdate = https_date()

        price_exists = os.path.exists(price_png_path)
        payment_exists = os.path.exists(payment_png_path)
        sha_price = sha256_file(price_png_path) if price_exists else "N/A"
        sha_payment = sha256_file(payment_png_path) if payment_exists else "N/A"

        price_id = f"price_{stock}_{version}"
        payment_id = f"payment_{stock}_{version}"
        if price_exists: screenshot_cache[price_id] = price_png_path
        if payment_exists: screenshot_cache[payment_id] = payment_png_path

        html = render_template_string("""
        <!DOCTYPE html>
        <html>
        <head>
        <meta charset="UTF-8">
        <title>Camping World Proof (v{{ version }})</title>
        <style>
            body{font-family:Arial,sans-serif;margin:20px;background:#f5f5f5;}
            h2{text-align:center;margin-bottom:30px;}
            .container{display:flex;flex-wrap:wrap;gap:20px;justify-content:center;}
            .image-box{border:2px solid #333;padding:15px;background:#fff;box-shadow:0 2px 4px rgba(0,0,0,.1);max-width:640px;}
            .image-box h3{margin-top:0;border-bottom:2px solid #333;padding-bottom:10px;}
            .screenshot-container{min-height:200px;background:#fafafa;padding:10px;border-radius:4px;}
            img{max-width:100%;height:auto;border:1px solid #ddd;display:block;margin:10px 0;}
            .success{color:green;font-weight:bold;}
            .error{color:red;font-weight:bold;padding:10px;background:#fee;border:1px solid red;border-radius:4px;}
        </style>
        </head>
        <body>
        <h2>Camping World Compliance Capture — v{{ version }}</h2>
        <div class="container">
        <div class="image-box">
            <h3>Price Hover — Full Page</h3>
            <div class="screenshot-container">
                {% if price_exists %}
                    <img src="/screenshot/{{ price_id }}" />
                    <p class="success">&#10003; Screenshot captured successfully</p>
                {% else %}
                    <p class="error">&#10007; Failed to capture Price Hover screenshot.</p>
                {% endif %}
            </div>
        </div>
        <div class="image-box">
            <h3>Payment Hover — Full Page</h3>
            <div class="screenshot-container">
                {% if payment_exists %}
                    <img src="/screenshot/{{ payment_id }}" />
                    <p class="success">&#10003; Screenshot captured successfully</p>
                {% else %}
                    <p class="error">&#10007; Failed to capture Payment Hover screenshot.</p>
                {% endif %}
            </div>
        </div>
        </div>
        </body></html>
        """,
        version=version, price_exists=price_exists, payment_exists=payment_exists,
        price_id=price_id, payment_id=payment_id)
        return Response(html, mimetype="text/html")

    except Exception as e:
        traceback.print_exc()
        return Response(f"Error: {e}", status=500)

# --- Helpers ---
def sha256_file(path:str)->str:
    if not os.path.exists(path): return "N/A"
    h=hashlib.sha256()
    with open(path,"rb") as f:
        for chunk in iter(lambda:f.read(1024*1024),b""):h.update(chunk)
    return h.hexdigest()

def https_date():
    try:
        r=requests.head("https://cloudflare.com",timeout=5)
        return r.headers.get("Date")
    except Exception:return None

# --- Core logic ---
def do_capture(stock:str, version:str):
    url=f"https://rv.campingworld.com/rv/{stock}"
    tmpdir=tempfile.mkdtemp(prefix=f"cw-{stock}-")
    price_png_path=os.path.join(tmpdir,f"cw_{stock}_price_{version}.png")
    payment_png_path=os.path.join(tmpdir,f"cw_{stock}_payment_{version}.png")

    with sync_playwright() as p:
        browser=p.chromium.launch(headless=True,args=["--no-sandbox","--disable-setuid-sandbox","--disable-dev-shm-usage"])
        ctx=browser.new_context(viewport={"width":1920,"height":1080})
        page=ctx.new_page()
        page.goto(url,wait_until="domcontentloaded")
        try: page.wait_for_load_state("networkidle",timeout=30000)
        except: pass

        # Set Oregon ZIP
        page.evaluate("""(zip)=>{localStorage.setItem('cw_zip',zip);document.cookie='cw_zip='+zip+';path=/';}""",OREGON_ZIP)
        page.reload(wait_until="domcontentloaded")
        try: page.wait_for_load_state("networkidle",timeout=15000)
        except: pass

        # ---- PRICE ----
        print("\n=== Capturing Price Hover ===")
        try:
            el=page.locator(".MuiTypography-root.MuiTypography-subtitle1").first
            el.scroll_into_view_if_needed()
            el.hover(force=True)
            page.wait_for_timeout(1500)
            page.screenshot(path=price_png_path,full_page=True)
            print(f"✅ Price screenshot saved: {price_png_path}")
        except Exception as e:
            print(f"Price hover failed: {e}")

        # ---- PAYMENT ----
        print("\n=== Capturing Payment Hover ===")
        try:
            ts=datetime.datetime.utcnow().strftime("%Y%m%d-%H%M%S")
            attempt_tag=f"payment-{ts}"
            dom_path=os.path.join(tmpdir,f"{attempt_tag}-dom.html")
            Path(dom_path).write_text(page.content())

            # Search text-based first
            payment_candidates=[
                page.get_by_text("/mo",exact=False),
                page.get_by_text("payment",exact=False),
                page.get_by_text("monthly",exact=False),
                page.locator("[data-testid*='payment']"),
            ]
            visible=None
            for cand in payment_candidates:
                try:
                    count=cand.count()
                    for i in range(count):
                        el=cand.nth(i)
                        if el.is_visible():
                            txt=(el.text_content() or "").lower()
                            if any(k in txt for k in["payment","monthly","/mo","per month","$"]):
                                visible=el
                                print(f"Candidate chosen #{i}: {txt}")
                                raise StopIteration
                except StopIteration:break
                except Exception:pass

            if not visible:
                print("❌ Payment element not found")
            else:
                trigger=visible
                try:
                    icon=visible.locator("xpath=following::*[(self::button or self::*[name()='svg'] or contains(@class,'MuiSvgIcon-root'))][1]")
                    if icon.count()>0 and icon.first.is_visible():
                        trigger=icon.first
                        print("Using adjacent icon trigger.")
                except Exception:pass

                trigger.scroll_into_view_if_needed(timeout=5000)
                page.wait_for_timeout(300)

                tooltip=page.locator("[role='tooltip'],.MuiTooltip-popper,[data-popper-placement],.MuiPopover-root")
                activated=False
                def tip_visible():
                    try:return tooltip.first.is_visible()
                    except:return False

                # Hover
                try:
                    trigger.hover(force=True,timeout=5000)
                    page.wait_for_timeout(400)
                    activated=tip_visible()
                    print(f"Hover activation: {activated}")
                except Exception as e: print(e)

                # Focus fallback
                if not activated:
                    try:
                        trigger.focus()
                        page.wait_for_timeout(300)
                        activated=tip_visible()
                        print(f"Focus activation: {activated}")
                    except Exception as e: print(e)

                # JS events fallback
                if not activated:
                    try:
                        page.evaluate("""(el)=>{['pointerover','mouseenter','mouseover'].forEach(t=>el.dispatchEvent(new MouseEvent(t,{bubbles:true}))) }""",trigger)
                        page.wait_for_timeout(300)
                        activated=tip_visible()
                        print(f"JS activation: {activated}")
                    except Exception as e: print(e)

                # Click fallback
                if not activated:
                    try:
                        trigger.click(timeout=2000)
                        page.wait_for_timeout(300)
                        activated=tip_visible()
                        print(f"Click activation: {activated}")
                    except Exception as e: print(e)

                # Capture full page
                page.wait_for_timeout(600)
                page.screenshot(path=payment_png_path,full_page=True)
                if os.path.exists(payment_png_path):
                    print(f"✅ Payment screenshot saved: {payment_png_path}")
                else:
                    print("❌ Payment screenshot not found after capture")
        except Exception as e:
            print(f"Payment hover failed: {e}")
            traceback.print_exc()

        browser.close()
        print("Browser closed.")
    return price_png_path,payment_png_path,url

if __name__=="__main__":
    app.run(host="0.0.0.0",port=PORT)
