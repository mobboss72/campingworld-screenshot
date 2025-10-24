# server.py
import os
import sys
import json
import hashlib
import datetime
import tempfile
import traceback
import requests
from pathlib import Path

from flask import Flask, request, send_from_directory, Response, render_template_string, send_file, jsonify
from playwright.sync_api import sync_playwright, TimeoutError as PlaywrightTimeout

# Persist Playwright browsers in a writable path
os.environ.setdefault("PLAYWRIGHT_BROWSERS_PATH", "/app/ms-playwright")

PORT = int(os.getenv("PORT", "8080"))
OREGON_ZIP = os.getenv("OREGON_ZIP", "97201")

DATA_DIR = Path(os.getenv("DATA_DIR", "/app/data")).resolve()
DATA_DIR.mkdir(parents=True, exist_ok=True)
VERSION_FILE = DATA_DIR / "version.json"
VERSIONS_LOG = DATA_DIR / "versions_log.jsonl"

# Simple in-memory path cache for screenshots
screenshot_cache = {}

app = Flask(__name__, static_folder=None)

# ---------------- Versioning ----------------
def load_version():
    if VERSION_FILE.exists():
        try:
            return json.loads(VERSION_FILE.read_text())
        except Exception:
            pass
    return {"major": 1, "minor": 0, "patch": 0}

def save_version(ver):
    VERSION_FILE.write_text(json.dumps(ver))

def bump_patch_and_get_version():
    ver = load_version()
    ver["patch"] = int(ver.get("patch", 0)) + 1
    save_version(ver)
    return f"{ver['major']}.{ver['minor']}.{ver['patch']}"

def log_version_entry(entry: dict):
    entry = dict(entry)
    try:
        with VERSIONS_LOG.open("a") as f:
            f.write(json.dumps(entry) + "\n")
    except Exception:
        pass

@app.get("/versions")
def list_versions():
    items = []
    if VERSIONS_LOG.exists():
        try:
            with VERSIONS_LOG.open() as f:
                for line in f:
                    items.append(json.loads(line))
        except Exception:
            pass
    # newest first
    items = list(reversed(items[-100:]))
    return jsonify(items)

# ---------------- Flask routes ----------------
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

        version = bump_patch_and_get_version()
        price_png_path, payment_png_path, url = do_capture(stock, version=version)

        utc_now = datetime.datetime.utcnow().strftime("%Y-%m-%d %H:%M:%S UTC")
        hdate = https_date()

        price_exists = os.path.exists(price_png_path)
        payment_exists = os.path.exists(payment_png_path)

        sha_price = sha256_file(price_png_path) if price_exists else "N/A (Capture failed)"
        sha_payment = sha256_file(payment_png_path) if payment_exists else "N/A (Capture failed)"

        ts = int(datetime.datetime.utcnow().timestamp())
        price_id = f"v{version}_price_{stock}_{ts}"
        payment_id = f"v{version}_payment_{stock}_{ts}"
        if price_exists:
            screenshot_cache[price_id] = price_png_path
        if payment_exists:
            screenshot_cache[payment_id] = payment_png_path

        # persist a versions log entry
        log_version_entry({
            "version": version,
            "time_utc": utc_now,
            "stock": stock,
            "url": url,
            "price_ok": price_exists,
            "payment_ok": payment_exists,
            "price_sha256": sha_price if price_exists else None,
            "payment_sha256": sha_payment if payment_exists else None,
        })

        html = render_template_string("""
            <!DOCTYPE html>
            <html>
            <head>
                <title>Compliance Screenshots — v{{ version }}</title>
                <meta charset="UTF-8">
                <style>
                    body { font-family: Arial, sans-serif; margin: 20px; background: #f5f5f5; }
                    h1, h2 { text-align: center; margin: 0 0 10px 0; }
                    .sub { text-align: center; color: #555; margin-bottom: 30px; }
                    .container { display: flex; flex-wrap: wrap; gap: 20px; justify-content: center; }
                    .image-box { border: 2px solid #333; padding: 15px; background: white; box-shadow: 0 2px 4px rgba(0,0,0,0.1); max-width: 600px; }
                    .image-box h3 { margin-top: 0; border-bottom: 2px solid #333; padding-bottom: 10px; }
                    .info { margin-top: 15px; font-size: 0.85em; line-height: 1.6; }
                    .info p { margin: 5px 0; word-wrap: break-word; }
                    .error { color: #b10000; font-weight: bold; padding: 10px; background: #fee; border: 1px solid #b10000; border-radius: 4px; }
                    .success { color: green; font-weight: bold; }
                    img { max-width: 100%; height: auto; border: 1px solid #ddd; display: block; margin: 10px 0; }
                    .screenshot-container { min-height: 200px; background: #fafafa; padding: 10px; border-radius: 4px; }
                    .topbar { text-align:center; margin-bottom: 12px; }
                    .topbar a { color:#0366d6; text-decoration:none; }
                </style>
            </head>
            <body>
                <h1>Camping World Proof</h1>
                <div class="sub">Compliance Capture — <strong>v{{ version }}</strong></div>
                <div class="topbar">
                    <a href="/versions" target="_blank">Version log</a>
                </div>
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
                            <p><strong>Version:</strong> v{{ version }}</p>
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
                            <p><strong>Version:</strong> v{{ version }}</p>
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
        version=version, stock=stock, url=url, utc_now=utc_now, hdate=hdate,
        price_exists=price_exists, payment_exists=payment_exists,
        price_id=price_id, payment_id=payment_id,
        sha_price=sha_price, sha_payment=sha_payment)

        return Response(html, mimetype="text/html")
    except Exception as e:
        print("❌ /capture failed:", e, file=sys.stderr)
        traceback.print_exc()
        return Response(f"Error: {e}", status=500)

# ---------------- Helpers ----------------
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

# ---------------- Core capture ----------------
def do_capture(stock: str, version: str) -> tuple[str, str, str]:
    url = f"https://rv.campingworld.com/rv/{stock}"
    tmpdir = tempfile.mkdtemp(prefix=f"cw-{stock}-v{version}-")
    price_png_path = os.path.join(tmpdir, f"cw_{stock}_v{version}_price.png")
    payment_png_path = os.path.join(tmpdir, f"cw_{stock}_v{version}_payment.png")

    print(f"\n=== v{version} capture for stock {stock} ===")
    print(f"Temp directory: {tmpdir}")

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

        # Force Oregon ZIP (for disclosures/payment calc)
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

        # PRICE HOVER (unchanged logic)
        try:
            page.wait_for_selector(".MuiTypography-root.MuiTypography-subtitle1", state="visible", timeout=15_000)
        except PlaywrightTimeout:
            pass

        print("\n=== Price Hover ===")
        try:
            price_selector = ".MuiTypography-root.MuiTypography-subtitle1:visible"
            price_elements = page.locator(price_selector)
            cnt = price_elements.count()
            visible_price = None
            for i in range(cnt):
                el = price_elements.nth(i)
                if el.is_visible():
                    visible_price = el
                    break
            if visible_price:
                visible_price.scroll_into_view_if_needed(timeout=5000)
                visible_price.hover(timeout=10000, force=True)
                page.wait_for_timeout(1500)
                page.screenshot(path=price_png_path, full_page=True)
                if os.path.exists(price_png_path):
                    print(f"✅ Price screenshot saved: {os.path.getsize(price_png_path)} bytes")
            else:
                print("❌ No visible price element found")
        except Exception as e:
            print(f"❌ Price hover capture failed: {e}")
            traceback.print_exc()

        # PAYMENT HOVER (more robust)
        print("\n=== Payment Hover ===")
        try:
            trigger = resolve_payment_trigger(page)
            if trigger is None:
                print("❌ Payment trigger not found")
            else:
                # Ensure visible and not covered
                trigger.scroll_into_view_if_needed(timeout=5000)
                suppress_overlays(page)

                # Try both physical mouse move and element.hover
                box = trigger.bounding_box()
                if box:
                    page.mouse.move(box["x"] + box["width"]/2, box["y"] + box["height"]/2)

                try:
                    trigger.hover(force=True, timeout=10_000)
                except Exception as e:
                    print(f"trigger.hover() failed: {e}")

                # Fire JS events that MUI tooltips listen to
                page.evaluate(
                    """(el) => {
                        const evts = ["pointerover","mouseover","mouseenter","focus"];
                        for (const t of evts) {
                            el.dispatchEvent(new Event(t, {bubbles:true, cancelable:true}));
                        }
                    }""",
                    trigger,
                )

                # Wait for tooltip/popover to appear
                if not wait_for_tooltip_any(page, timeout=5000):
                    # last resort: click (some popovers open on click)
                    try:
                        trigger.click(timeout=2000)
                    except Exception:
                        pass
                    wait_for_tooltip_any(page, timeout=2000)

                page.wait_for_timeout(500)
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

# ---------- Payment helpers ----------
def resolve_payment_trigger(page):
    """
    Look for a payment text or its nearby info icon.
    Search in page + iframes. Return an ElementHandle-like locator.
    """
    frames = [page] + list(page.frames)
    print(f"Searching payment trigger across {len(frames)} frame(s)")

    text_candidates = [
        "text=/payment|monthly|\\/mo/i",
        ".MuiTypography-root.MuiTypography-subtitle2",
        "[data-testid*=payment], [id*=payment], [class*=payment]"
    ]
    icon_rel_xpath = "xpath=following::*[contains(@class,'MuiSvgIcon-root') or contains(@class,'Info')][1]"

    for ctx in frames:
        for sel in text_candidates:
            try:
                ctx.wait_for_selector(sel, state="visible", timeout=3000)
            except PlaywrightTimeout:
                continue
            loc = ctx.locator(sel)
            count = loc.count()
            for i in range(count):
                cand = loc.nth(i)
                if not cand.is_visible():
                    continue
                txt = (cand.text_content() or "").lower()
                if any(k in txt for k in ("payment","/mo","monthly")) or sel != text_candidates[0]:
                    # prefer the adjacent icon as the real hover trigger
                    icon = cand.locator(icon_rel_xpath)
                    if icon.count() > 0 and icon.first.is_visible():
                        print("Using adjacent info icon as trigger")
                        return icon.first
                    print("Using payment text as trigger")
                    return cand
    return None

def wait_for_tooltip_any(page, timeout=3000) -> bool:
    sels = [
        "[role=tooltip]",
        ".MuiTooltip-popper",
        ".MuiPopover-root",
        "[data-popper-placement]",
        ".MuiTooltip-tooltip"
    ]
    deadline = datetime.datetime.now().timestamp() + (timeout/1000.0)
    while datetime.datetime.now().timestamp() < deadline:
        for s in sels:
            try:
                if page.locator(s).first.is_visible():
                    print(f"Tooltip visible by selector: {s}")
                    return True
            except Exception:
                pass
        page.wait_for_timeout(150)
    print("Tooltip not detected within timeout")
    return False

def suppress_overlays(page):
    """Hide chat widgets or sticky bars that can capture pointer events."""
    try:
        page.evaluate("""
            () => {
                const hide = (sel) => {
                    const el = document.querySelector(sel);
                    if (el) el.style.display = 'none';
                };
                hide('#crisp-chatbox'); hide('.crisp-client');
                hide('[id*=launcher], [class*=launcher]');
                hide('[class*=LiveChat], [id*=LiveChat]');
                // also ensure any sticky bottom bar doesn't cover hover target
                const bars = document.querySelectorAll('[class*="sticky"], [class*="Sticky"], [class*="fixed"]');
                bars.forEach(b => { if (b.clientHeight > 60) b.style.display = 'none'; });
            }
        """)
    except Exception:
        pass

# ---------------- Main ----------------
if __name__ == "__main__":
    app.run(host="0.0.0.0", port=PORT)
