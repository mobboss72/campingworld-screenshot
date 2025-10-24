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
from playwright.sync_api import sync_playwright

# Persist Playwright browsers between deploys
os.environ.setdefault("PLAYWRIGHT_BROWSERS_PATH", "/app/ms-playwright")

PORT = int(os.getenv("PORT", "8080"))
OREGON_ZIP = os.getenv("OREGON_ZIP", "97201")

# Simple in-memory map from id -> file path (prod: use storage/db)
screenshot_cache: dict[str, str] = {}

app = Flask(__name__, static_folder=None)


# ---------------------------
# Routes
# ---------------------------
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

        price_png_path, payment_png_path, url, log_lines = do_capture(stock)

        utc_now = datetime.datetime.utcnow().strftime("%Y-%m-%d %H:%M:%S UTC")
        hdate = https_date()

        price_exists = os.path.exists(price_png_path)
        payment_exists = os.path.exists(payment_png_path)

        sha_price = sha256_file(price_png_path) if price_exists else "N/A (capture failed)"
        sha_payment = sha256_file(payment_png_path) if payment_exists else "N/A (capture failed)"

        # store paths for GET /screenshot
        ts = int(datetime.datetime.utcnow().timestamp())
        price_id = f"price_{stock}_{ts}"
        payment_id = f"payment_{stock}_{ts}"
        if price_exists:
            screenshot_cache[price_id] = price_png_path
        if payment_exists:
            screenshot_cache[payment_id] = payment_png_path

        html = render_template_string(
            """
<!doctype html>
<html>
<head>
  <meta charset="utf-8" />
  <title>Compliance Screenshots</title>
  <style>
    body{font-family:Arial,sans-serif;background:#f7f7f9;margin:20px}
    h1{margin:0 0 16px}
    .grid{display:flex;flex-wrap:wrap;gap:20px}
    .card{background:#fff;border:1px solid #ddd;border-radius:8px;padding:14px;max-width:640px;flex:1 1 420px;box-shadow:0 2px 6px rgba(0,0,0,.06)}
    .card h3{margin:0 0 10px;padding-bottom:8px;border-bottom:1px solid #eee}
    img{max-width:100%;height:auto;border:1px solid #e5e5e5}
    .meta{font-size:12px;color:#444;margin-top:10px;line-height:1.5}
    .ok{color:#137333;font-weight:600}
    .err{color:#b00020;font-weight:600}
    pre.log{white-space:pre-wrap;background:#0b1021;color:#eaeef7;border-radius:6px;padding:10px;font-size:12px;max-height:280px;overflow:auto;border:1px solid #1e2744}
    code{word-break:break-all}
  </style>
</head>
<body>
  <h1>Camping World Proof (Compliance Capture)</h1>
  <div class="grid">
    <div class="card">
      <h3>Price Hover — Full Page</h3>
      {% if price_exists %}
        <img src="/screenshot/{{ price_id }}" alt="Price Hover" />
        <div class="ok">✓ Price screenshot captured</div>
      {% else %}
        <div class="err">✗ Price screenshot failed</div>
      {% endif %}
      <div class="meta">
        <div><strong>Stock:</strong> {{ stock }}</div>
        <div><strong>URL:</strong> <a href="{{ url }}" target="_blank" rel="noopener">{{ url }}</a></div>
        <div><strong>UTC:</strong> {{ utc_now }}</div>
        <div><strong>HTTPS Date:</strong> {{ hdate or 'unavailable' }}</div>
        <div><strong>SHA-256:</strong> <code>{{ sha_price }}</code></div>
      </div>
    </div>

    <div class="card">
      <h3>Payment Hover — Full Page</h3>
      {% if payment_exists %}
        <img src="/screenshot/{{ payment_id }}" alt="Payment Hover" />
        <div class="ok">✓ Payment screenshot captured</div>
      {% else %}
        <div class="err">✗ Payment screenshot failed</div>
      {% endif %}
      <div class="meta">
        <div><strong>Stock:</strong> {{ stock }}</div>
        <div><strong>URL:</strong> <a href="{{ url }}" target="_blank" rel="noopener">{{ url }}</a></div>
        <div><strong>UTC:</strong> {{ utc_now }}</div>
        <div><strong>HTTPS Date:</strong> {{ hdate or 'unavailable' }}</div>
        <div><strong>SHA-256:</strong> <code>{{ sha_payment }}</code></div>
      </div>
    </div>

    <div class="card" style="flex-basis:100%;">
      <h3>Capture Log</h3>
      <pre class="log">{{ log_text }}</pre>
    </div>
  </div>
</body>
</html>
            """,
            stock=stock,
            url=url,
            utc_now=utc_now,
            hdate=hdate,
            price_exists=price_exists,
            payment_exists=payment_exists,
            price_id=price_id,
            payment_id=payment_id,
            sha_price=sha_price,
            sha_payment=sha_payment,
            log_text="\n".join(log_lines[-120:]),  # tail to keep page tidy
        )
        return Response(html, mimetype="text/html")
    except Exception as e:
        print("❌ /capture failed:", e, file=sys.stderr)
        traceback.print_exc()
        return Response(f"Error: {e}", status=500)


# ---------------------------
# Helpers
# ---------------------------
def sha256_file(path: str) -> str:
    if not os.path.exists(path):
        return "N/A (file not found)"
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


def _fsize(path: str) -> str:
    try:
        return f"{os.path.getsize(path)} bytes"
    except Exception:
        return "n/a"


# ---------------------------
# Core capture
# ---------------------------
def do_capture(stock: str) -> tuple[str, str, str, list[str]]:
    """
    Navigate to the RV detail page, hide overlays, set ZIP,
    capture price tooltip (full-page), then payment tooltip (full-page).
    """
    url = f"https://rv.campingworld.com/rv/{stock}"
    tmpdir = tempfile.mkdtemp(prefix=f"cw-{stock}-")
    price_png_path = os.path.join(tmpdir, f"cw_{stock}_price.png")
    payment_png_path = os.path.join(tmpdir, f"cw_{stock}_payment.png")

    log: list[str] = []
    log.append(f"goto domcontentloaded: {url}")

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

        # Load + stabilize
        page.goto(url, wait_until="domcontentloaded")
        try:
            page.wait_for_load_state("networkidle", timeout=30_000)
            log.append("networkidle reached")
        except Exception as e:
            log.append(f"networkidle timeout (continue): {e}")

        # Set ZIP in cookie/localStorage, then reload to get OR pricing/payments
        try:
            page.evaluate(
                """(zip) => {
                    try { localStorage.setItem('cw_zip', zip); } catch(e) {}
                    document.cookie = 'cw_zip=' + zip + ';path=/;SameSite=Lax';
                }""",
                OREGON_ZIP,
            )
            log.append(f"ZIP injected {OREGON_ZIP}")
            page.reload(wait_until="domcontentloaded")
            page.wait_for_load_state("networkidle", timeout=20_000)
            log.append("post-zip networkidle reached")
        except Exception as e:
            log.append(f"ZIP injection/reload issue: {e}")

        # Kill common overlays that block hover (ConvertFlow / etc.)
        try:
            page.add_style_tag(
                content="""
                .convertflow-cta, .cf-overlay, .cf-prehook-popup, .cf-section-overlay-outer,
                [id^="cta_"], [id^="cta"] { display: none !important; visibility: hidden !important; }
                #trustarc-banner, #onetrust-banner-sdk, .ot-sdk-container { display: none !important; }
                """
            )
            log.append("overlay-hiding CSS injected")
        except Exception as e:
            log.append(f"overlay CSS fail: {e}")

        # PRICE (first)
        try:
            capture_price_tooltip(page, price_png_path, log)
        except Exception as e:
            log.append(f"price: unexpected exception: {e}")
            traceback.print_exc()

        # Clear tooltip state before moving on
        try:
            page.mouse.move(5, 5)  # far corner, away from icons
            page.keyboard.press("Escape")
            page.wait_for_timeout(300)
        except Exception:
            pass

        # PAYMENT (second)
        try:
            capture_payment_tooltip(page, payment_png_path, log)
        except Exception as e:
            log.append(f"payment: unexpected exception: {e}")
            traceback.print_exc()

        browser.close()

    # Final file existence report in logs
    log.append(
        f"price file: {'exists' if os.path.exists(price_png_path) else 'missing'} ({_fsize(price_png_path)})"
    )
    log.append(
        f"payment file: {'exists' if os.path.exists(payment_png_path) else 'missing'} ({_fsize(payment_png_path)})"
    )
    return price_png_path, payment_png_path, url, log


# ---------------------------
# Price capture
# ---------------------------
def capture_price_tooltip(page, out_path: str, log: list[str]) -> None:
    """
    Hover the price label (subtitle1) or nearby info icon to reveal tooltip/popover,
    then save a full-page screenshot including the floating layer.
    """
    # Prefer explicit subtitle1 elements
    sel = ".MuiTypography-root.MuiTypography-subtitle1"
    try:
        page.wait_for_selector(sel, state="visible", timeout=6000)
    except Exception:
        log.append("price subtitle1 wait timeout (continue)")

    candidates = page.locator(sel)
    count = candidates.count()
    log.append(f"price candidates={count}")

    target = None
    for i in range(count):
        el = candidates.nth(i)
        if not el.is_visible():
            continue
        text = (el.text_content() or "").strip()
        target = el
        log.append(f"price use idx={i} text='{text}'")
        break

    if not target:
        log.append("price: no visible subtitle1 found; trying generic amount text")
        # Fallback: any element with a large dollar amount (rough)
        try:
            page.wait_for_selector('xpath=//*[contains(normalize-space(.), "$")]', state="visible", timeout=4000)
            target = page.locator('xpath=//*[contains(normalize-space(.), "$")]').first
        except Exception:
            pass

    if not target:
        log.append("price: no anchor, skip")
        return

    try:
        target.scroll_into_view_if_needed(timeout=5000)
    except Exception:
        pass

    # Try to hover an info icon next to price if exists, else hover the price text itself
    trigger = None
    icon = target.locator("xpath=following::*[contains(@class,'MuiSvgIcon-root') or contains(@data-testid,'Info')][1]")
    if icon.count() > 0 and icon.first.is_visible():
        trigger = icon.first
        log.append("price: using adjacent info icon")
    else:
        trigger = target
        log.append("price: using price text as trigger")

    # Move the mouse to actually fire MUI tooltip
    try:
        bb = trigger.bounding_box()
        if not bb:
            log.append("price: trigger has no bounding box")
            return
        cx, cy = bb["x"] + bb["width"] / 2, bb["y"] + bb["height"] / 2
        page.mouse.move(cx - 1, cy - 1)
        page.mouse.move(cx, cy, steps=6)
        page.mouse.move(cx + 1, cy + 1, steps=4)

        # Wait for tooltip-ish element
        try:
            page.wait_for_selector('[role="tooltip"], .MuiTooltip-popper, [data-popper-placement]', state="visible", timeout=5000)
            log.append("price tooltip appeared")
        except Exception:
            log.append("price tooltip not detected (timeout)")

        page.wait_for_timeout(600)
        page.screenshot(path=out_path, full_page=True)
        log.append(f"price screenshot saved {out_path} size={_fsize(out_path)}")
    except Exception as e:
        log.append(f"price hover/screenshot failed: {e}")
        traceback.print_exc()


# ---------------------------
# Payment capture
# ---------------------------
def capture_payment_tooltip(page, out_path: str, log: list[str]) -> None:
    """
    Anchor on payment text (/mo | monthly | per month), then find nearby info icon,
    hover via precise mouse moves, wait for tooltip, full-page screenshot.
    """
    # 1) Find an anchor by text
    payment_xpaths = [
        r'xpath=//*[contains(normalize-space(.), "/mo")]',
        r'xpath=//*[matches(normalize-space(.), "(?i)\bmonthly\b")]',
        r'xpath=//*[matches(normalize-space(.), "(?i)\bper\s*month\b")]',
    ]

    anchor = None
    for xp in payment_xpaths:
        try:
            page.wait_for_selector(xp, state="visible", timeout=4000)
            loc = page.locator(xp)
            if loc.count() > 0 and loc.first.is_visible():
                anchor = loc.first
                log.append(f"payment anchor via {xp}")
                break
        except Exception:
            pass

    # Fallback: MUI subtitle2 with payment-y text
    if not anchor:
        try:
            sel = (
                '.MuiTypography-root.MuiTypography-subtitle2:has-text("/mo"), '
                '.MuiTypography-subtitle2:has-text("monthly"), '
                '.MuiTypography-subtitle2:has-text("per month")'
            )
            page.wait_for_selector(sel, state="visible", timeout=4000)
            loc = page.locator(sel)
            if loc.count() > 0 and loc.first.is_visible():
                anchor = loc.first
                log.append("payment anchor via subtitle2")
        except Exception:
            pass

    if not anchor:
        log.append("payment: no visible payment text anchor found")
        return

    try:
        anchor.scroll_into_view_if_needed(timeout=5000)
        log.append("payment anchor scrolled into view")
    except Exception:
        pass

    # 2) Try to find the “info” icon next to the payment text
    icon_queries = [
        'xpath=following::*[self::button or self::span or self::svg][contains(@class,"Info") or contains(@class,"MuiSvgIcon-root") or contains(@data-testid,"Info")][1]',
        'xpath=ancestor::*[contains(@class,"Mui")][1]//*[self::button or self::span or self::svg][contains(@class,"Info") or contains(@class,"MuiSvgIcon-root") or contains(@data-testid,"Info")][1]',
        'xpath=following::*[@aria-label and (contains(translate(@aria-label,"INFO","info"),"info") or contains(translate(@aria-label,"PAYMENT","payment"),"payment") or contains(translate(@aria-label,"MORE INFORMATION","more information"),"more information"))][1]',
        'xpath=ancestor::*[contains(@class,"Mui")][1]//*[@aria-describedby][1]',
    ]

    trigger = None
    for q in icon_queries:
        try:
            cand = anchor.locator(q)
            if cand.count() > 0 and cand.first.is_visible():
                trigger = cand.first
                log.append(f"payment icon via '{q}'")
                break
        except Exception:
            pass

    # Geometric fallback: pick nearest visible icon to right of anchor
    if not trigger:
        try:
            anc_box = anchor.bounding_box()
            icons = page.locator('svg.MuiSvgIcon-root, [data-testid*="Info"], span.MuiSvgIcon-root, button[aria-label*="info" i]')
            n = icons.count()
            best_i, best_dx = -1, 1e9
            for i in range(n):
                el = icons.nth(i)
                if not el.is_visible():
                    continue
                bb = el.bounding_box()
                if not bb or not anc_box:
                    continue
                # right-of and roughly aligned vertically
                if bb["x"] > anc_box["x"] and abs((bb["y"] + bb["height"]/2) - (anc_box["y"] + anc_box["height"]/2)) < 60:
                    dx = bb["x"] - anc_box["x"]
                    if 0 < dx < best_dx:
                        best_dx, best_i = dx, i
            if best_i >= 0:
                trigger = icons.nth(best_i)
                log.append(f"payment icon via geometric search idx={best_i} dx≈{best_dx:.1f}")
        except Exception as e:
            log.append(f"geom search error: {e}")

    if not trigger:
        log.append("payment: no icon trigger found")
        return

    # 3) Hover the icon (use real mouse moves for tiny glyphs)
    try:
        bb = trigger.bounding_box()
        if not bb:
            log.append("payment trigger has no bounding box")
            return

        cx, cy = bb["x"] + bb["width"]/2, bb["y"] + bb["height"]/2
        page.mouse.move(cx - 1, cy - 1)
        page.mouse.move(cx, cy, steps=6)
        page.mouse.move(cx + 1, cy + 1, steps=4)
        log.append(f"payment mouse hovered icon at ({cx:.1f},{cy:.1f}) w={bb['width']:.1f} h={bb['height']:.1f}")

        try:
            page.wait_for_selector('[role="tooltip"], .MuiTooltip-popper, [data-popper-placement]', state="visible", timeout=5000)
            log.append("payment tooltip appeared")
        except Exception:
            log.append("payment tooltip not detected (timeout)")

        page.wait_for_timeout(600)
        page.screenshot(path=out_path, full_page=True)
        log.append(f"payment screenshot saved {out_path} size={_fsize(out_path)}")
    except Exception as e:
        log.append(f"payment hover/screenshot failed: {e}")
        traceback.print_exc()


# ---------------------------
# Entrypoint
# ---------------------------
if __name__ == "__main__":
    # For local runs only; Railway uses gunicorn via Dockerfile
    app.run(host="0.0.0.0", port=PORT)
