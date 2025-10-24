# server.py
import os
import sys
import io
import hashlib
import datetime as dt
import tempfile
import traceback
from pathlib import Path
from typing import Optional, Tuple

import requests
from flask import Flask, request, send_from_directory, Response, render_template_string, send_file

from playwright.sync_api import sync_playwright, TimeoutError as PWTimeout

# ---------- runtime/env ----------
os.environ.setdefault("PLAYWRIGHT_BROWSERS_PATH", "/app/ms-playwright")
PORT = int(os.getenv("PORT", "8080"))
OREGON_ZIP = os.getenv("OREGON_ZIP", "97201")
DEBUG_SHOTS = os.getenv("DEBUG_SHOTS", "0") == "1"
ENABLE_TRACE = True  # always record a small trace for post-mortem
VERSION = "v1.7.3 - dual tooltip, trace, robust selectors, timestamps restored"

# ---------- flask ----------
app = Flask(__name__, static_folder=None)

# simple in-mem registry for files we want to serve back
ARTIFACTS: dict[str, str] = {}  # id -> absolute path


# ---------- routes ----------
@app.get("/")
def root():
    return send_from_directory(".", "index.html")


@app.get("/screenshot/<artifact_id>")
def serve_screenshot(artifact_id: str):
    path = ARTIFACTS.get(artifact_id)
    if not path or not os.path.exists(path):
        return Response("Not found", 404)
    return send_file(path, mimetype="image/png")


@app.get("/artifact/<artifact_id>")
def serve_artifact(artifact_id: str):
    path = ARTIFACTS.get(artifact_id)
    if not path or not os.path.exists(path):
        return Response("Not found", 404)
    # best-effort content type
    mime = "application/zip" if path.endswith(".zip") else "application/octet-stream"
    return send_file(path, mimetype=mime, as_attachment=True, download_name=Path(path).name)


@app.post("/capture")
def capture():
    try:
        stock = (request.form.get("stock") or "").strip()
        if not stock.isdigit():
            return Response("Invalid stock number", 400)

        price_png, pay_png, url, trace_zip = do_capture(stock)

        utc_now = dt.datetime.utcnow().strftime("%Y-%m-%d %H:%M:%S UTC")
        https_hdr_date = https_date()

        price_ok = os.path.exists(price_png)
        pay_ok = os.path.exists(pay_png)

        sha_price = sha256_file(price_png) if price_ok else "N/A"
        sha_pay = sha256_file(pay_png) if pay_ok else "N/A"

        price_id = f"price_{stock}_{int(dt.datetime.utcnow().timestamp())}"
        pay_id = f"payment_{stock}_{int(dt.datetime.utcnow().timestamp())}"
        if price_ok:
            ARTIFACTS[price_id] = price_png
        if pay_ok:
            ARTIFACTS[pay_id] = pay_png

        trace_id = None
        if trace_zip and os.path.exists(trace_zip):
            trace_id = f"trace_{stock}_{int(dt.datetime.utcnow().timestamp())}"
            ARTIFACTS[trace_id] = trace_zip

        html = render_template_string(
            HTML_TEMPLATE,
            stock=stock,
            url=url,
            utc_now=utc_now,
            https_date=https_hdr_date,
            price_ok=price_ok,
            pay_ok=pay_ok,
            price_id=price_id,
            pay_id=pay_id,
            sha_price=sha_price,
            sha_pay=sha_pay,
            trace_id=trace_id,
            version=VERSION,
        )
        return Response(html, mimetype="text/html")
    except Exception as e:
        print("❌ /capture failed:", e, file=sys.stderr)
        traceback.print_exc()
        return Response(f"Error: {e}", 500)


# ---------- core capture ----------
def do_capture(stock: str) -> Tuple[str, str, str, Optional[str]]:
    url = f"https://rv.campingworld.com/rv/{stock}"
    tmpdir = Path(tempfile.mkdtemp(prefix=f"cw-{stock}-"))
    price_png = str(tmpdir / f"cw_{stock}_price.png")
    pay_png = str(tmpdir / f"cw_{stock}_payment.png")
    trace_zip: Optional[str] = None

    print(f"\n==== START capture {stock} ====")
    print(f"Version: {VERSION}")
    print(f"Temp dir: {tmpdir}")
    print(f"URL: {url}")

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
            user_agent="Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
            "(KHTML, like Gecko) Chrome/118 Safari/537.36",
            ignore_https_errors=True,
            record_har_path=str(tmpdir / "network.har") if ENABLE_TRACE else None,
        )

        # Browser console & page errors -> server logs
        def _log_console(msg):
            try:
                print(f"[BROWSER:{msg.type()}] {msg.text}", flush=True)
            except Exception:
                pass

        def _log_pageerr(e):
            print(f"[PAGEERR] {e}", flush=True)

        if ENABLE_TRACE:
            ctx.tracing.start(screenshots=True, snapshots=True, sources=True)

        page = ctx.new_page()
        page.on("console", _log_console)
        page.on("pageerror", _log_pageerr)

        goto_with_idle(page, url)

        set_zip_cookie(page, OREGON_ZIP)
        goto_with_idle(page, url)  # reload to apply zip

        hide_overlay_junk(page)

        # --- PRICE TOOLTIP ---
        try:
            print("\n--- PRICE tooltip phase ---")
            capture_price_tooltip(page, price_png)
            print("✅ price screenshot:", price_png, os.path.exists(price_png) and os.path.getsize(price_png), "bytes")
        except Exception as e:
            print(f"❌ price capture error: {e}")
            if DEBUG_SHOTS:
                safe_snap(page, tmpdir / "price_error.png")

        # Close any open tooltip before switching
        hard_close_tooltips(page)

        # --- PAYMENT TOOLTIP ---
        try:
            print("\n--- PAYMENT tooltip phase ---")
            capture_payment_tooltip(page, pay_png)
            print("✅ payment screenshot:", pay_png, os.path.exists(pay_png) and os.path.getsize(pay_png), "bytes")
        except Exception as e:
            print(f"❌ payment capture error: {e}")
            if DEBUG_SHOTS:
                safe_snap(page, tmpdir / "payment_error.png")

        # wrap up
        if ENABLE_TRACE:
            trace_zip = str(tmpdir / f"trace_{stock}.zip")
            try:
                ctx.tracing.stop(path=trace_zip)
                print(f"Trace saved: {trace_zip}")
            except Exception as e:
                print("trace stop failed:", e)

        ctx.close()
        browser.close()
        print("==== END capture ====\n")

    # Append a tiny changelog line for your “automatic versioning”
    try:
        (tmpdir / "CHANGELOG.txt").write_text(
            f"[{utc_now()}] {VERSION} stock={stock} "
            f"price={'ok' if os.path.exists(price_png) else 'fail'} "
            f"payment={'ok' if os.path.exists(pay_png) else 'fail'}\n",
            encoding="utf-8",
        )
    except Exception:
        pass

    return price_png, pay_png, url, trace_zip


# ---------- sub-routines ----------
def goto_with_idle(page, url: str, timeout_ms: int = 35_000):
    print(f"Navigating: {url}")
    page.goto(url, wait_until="domcontentloaded", timeout=timeout_ms)
    try:
        page.wait_for_load_state("networkidle", timeout=timeout_ms)
    except PWTimeout:
        print("networkidle timeout (continuing)")

def set_zip_cookie(page, zip_code: str):
    try:
        page.evaluate(
            """(zip) => {
                try { localStorage.setItem('cw_zip', String(zip)); } catch(e) {}
                document.cookie = 'cw_zip=' + String(zip) + ';path=/;SameSite=Lax';
            }""",
            zip_code,
        )
        print(f"ZIP set -> {zip_code}")
    except Exception as e:
        print("zip set failed:", e)

def hide_overlay_junk(page):
    # Common chat/overlay annoyances that cover tooltips
    js = """
    (function(){
      const sel = [
        '#launcher', '#intercom-container', '[id*="launcher"]',
        'iframe[src*="intercom"]','iframe[id*="launcher"]','[data-test-id="live-chat"]',
        '.fc-consent-root', '.osano-cm-dialog', '.osano-cm-widget', '.zEWidget-webWidget'
      ];
      for (const s of sel){
        document.querySelectorAll(s).forEach(el => { el.style.display='none'; el.remove(); });
      }
    })();
    """
    try:
        page.evaluate(js)
    except Exception:
        pass

def hard_close_tooltips(page):
    try:
        page.keyboard.press("Escape")
        page.mouse.move(5, 5)
        page.evaluate("""() => {
            document.querySelectorAll('[role=tooltip], .MuiTooltip-popper, [id*="mui-tooltip"]').forEach(el=>{
              el.style.display='none';
              if (el.remove) el.remove();
            });
        }""")
        page.wait_for_timeout(150)
    except Exception:
        pass

def safe_snap(page, path: Path):
    try:
        page.screenshot(path=str(path), full_page=True)
        print(f"[debug] saved {path}")
    except Exception:
        pass

def wait_tooltip_visible(page, timeout_ms: int = 5000):
    # union of likely tooltip containers, plus aria-describedby shadow
    sel = "[role=tooltip], .MuiTooltip-popper, [id*='mui-tooltip'], [data-popper-placement], span[aria-describedby*='mui-tooltip']"
    return page.locator(sel).first.wait_for(state="visible", timeout=timeout_ms)

def small_hover_bump(page, locator):
    bb = locator.bounding_box()
    if not bb:
        return
    x = bb["x"] + bb["width"] / 2
    y = bb["y"] + bb["height"] / 2
    page.mouse.move(x, y)
    page.mouse.move(x + 2, y + 2)  # nudge often helps tooltips

# ---- PRICE (keeps your confirmed-working snippet) ----
def capture_price_tooltip(page, out_png: str):
    price_selector = ".MuiTypography-root.MuiTypography-subtitle1:visible"
    price_elements = page.locator(price_selector)
    count = price_elements.count()
    print(f"Price elements: {count}")

    visible_price = None
    for i in range(count):
        el = price_elements.nth(i)
        if el.is_visible():
            visible_price = el
            print(f"Using price element index={i}")
            break

    if not visible_price:
        raise RuntimeError("No visible price element found")

    # Try hovering icon next to price first; fall back to text
    trigger = visible_price
    icon = visible_price.locator("xpath=following::*[contains(@class,'MuiSvgIcon-root')][1]")
    if icon.count() > 0 and icon.first.is_visible():
        print("Found price info icon, will hover icon")
        trigger = icon.first

    visible_price.scroll_into_view_if_needed(timeout=5000)
    small_hover_bump(page, trigger)
    try:
        trigger.hover(timeout=10_000, force=True)
    except PWTimeout:
        # fallback to dispatch mouseover
        page.evaluate("(el)=>el.dispatchEvent(new MouseEvent('mouseover',{bubbles:true}))", trigger)

    page.wait_for_timeout(900)
    wait_tooltip_visible(page, timeout_ms=5_000)
    page.screenshot(path=out_png, full_page=True)

# ---- PAYMENT (robust with multiple heuristics, including your aria-describedby idea) ----
def capture_payment_tooltip(page, out_png: str):
    # Primary: subtitle2 near $/mo or 'payment'
    base = page.locator(".MuiTypography-root.MuiTypography-subtitle2:visible")
    count = base.count()
    print(f"Payment subtitle2 elements: {count}")

    payment = None
    for i in range(count):
        el = base.nth(i)
        if not el.is_visible():
            continue
        txt = (el.text_content() or "").lower().strip()
        if any(k in txt for k in ["$ /mo", "$/mo", "per month", "payment", "est. payment", "mo"]):
            payment = el
            print(f"Picked payment idx={i} text={txt!r}")
            break

    # Fallback 1: explicit label
    if payment is None:
        payment = page.locator("text=/est\\.?\\s*payment/i").first
        if payment and payment.count() and payment.is_visible():
            print("Fallback match: text=Est. Payment")

    if payment is None or not payment.is_visible():
        raise RuntimeError("Payment element not found/visible")

    # Prefer the little info icon to trigger tooltip
    trigger = payment
    icon = payment.locator("xpath=following::*[contains(@class,'MuiSvgIcon-root') or contains(@class,'Info')][1]")
    if icon.count() > 0 and icon.first.is_visible():
        trigger = icon.first
        print("Using payment info icon as trigger")

    payment.scroll_into_view_if_needed(timeout=5000)
    small_hover_bump(page, trigger)

    try:
        trigger.hover(timeout=10_000, force=True)
    except PWTimeout:
        page.evaluate("(el)=>el.dispatchEvent(new MouseEvent('mouseover',{bubbles:true}))", trigger)

    # Wait for tooltip (union selector incl. aria-describedby)
    try:
        wait_tooltip_visible(page, timeout_ms=5_000)
    except PWTimeout:
        # last-ditch: try hovering the text itself then icon again
        try:
            payment.hover(timeout=5_000, force=True)
            page.wait_for_timeout(700)
            wait_tooltip_visible(page, timeout_ms=3_000)
        except Exception as e:
            raise RuntimeError(f"tooltip not visible: {e}")

    page.wait_for_timeout(400)
    page.screenshot(path=out_png, full_page=True)


# ---------- utilities ----------
def sha256_file(path: str) -> str:
    if not os.path.exists(path):
        return "N/A"
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(1024 * 1024), b""):
            h.update(chunk)
    return h.hexdigest()

def https_date() -> Optional[str]:
    try:
        r = requests.head("https://cloudflare.com", timeout=8)
        return r.headers.get("Date")
    except Exception:
        return None

def utc_now() -> str:
    return dt.datetime.utcnow().strftime("%Y-%m-%d %H:%M:%S UTC")


# ---------- HTML ----------
HTML_TEMPLATE = """
<!doctype html>
<html>
<head>
  <meta charset="utf-8"/>
  <title>Camping World Compliance Capture</title>
  <style>
    body{font-family:system-ui,-apple-system,Segoe UI,Roboto,Helvetica,Arial,sans-serif;background:#f6f7fb;color:#111;margin:0;padding:24px}
    h1{font-size:28px;margin:0 0 16px}
    .meta{background:#fff;border:1px solid #e6e8ef;border-radius:10px;padding:14px 16px;margin-bottom:18px}
    .row{display:grid;grid-template-columns:1fr 1fr;gap:16px}
    .panel{background:#fff;border:1px solid #e6e8ef;border-radius:12px;padding:14px}
    .panel h2{margin:0 0 12px;font-size:20px}
    .ok{color:#1f7a39;font-weight:700}
    .err{color:#b00020;font-weight:700}
    img{max-width:100%;border:1px solid #ddd;border-radius:6px}
    code{background:#f1f3f8;padding:2px 6px;border-radius:4px}
    .foot{margin-top:18px;font-size:12px;color:#555}
    a{color:#0a67d0;text-decoration:none}
  </style>
</head>
<body>
  <h1>Camping World Compliance Capture</h1>
  <div class="meta">
    <div><strong>Stock:</strong> {{stock}}</div>
    <div><strong>URL:</strong> <a href="{{url}}" target="_blank">{{url}}</a></div>
    <div><strong>UTC:</strong> {{utc_now}}</div>
    <div><strong>HTTPS Date:</strong> {{https_date or 'unavailable'}}</div>
    {% if trace_id %}
      <div><strong>Trace:</strong> <a href="/artifact/{{trace_id}}">Download trace</a> (open in <code>npx playwright show-trace</code>)</div>
    {% endif %}
    <div class="foot">Build {{version}}</div>
  </div>

  <div class="row">
    <div class="panel">
      <h2>Price Tooltip — Full Page</h2>
      {% if price_ok %}
        <img src="/screenshot/{{price_id}}" alt="Price tooltip screenshot"/>
        <p class="ok">✓ Captured</p>
        <p><strong>SHA-256:</strong> <code>{{sha_price}}</code></p>
      {% else %}
        <p class="err">✗ Could not capture price tooltip.</p>
        <p><strong>SHA-256:</strong> <code>N/A</code></p>
      {% endif %}
    </div>

    <div class="panel">
      <h2>Payment Tooltip — Full Page</h2>
      {% if pay_ok %}
        <img src="/screenshot/{{pay_id}}" alt="Payment tooltip screenshot"/>
        <p class="ok">✓ Captured</p>
        <p><strong>SHA-256:</strong> <code>{{sha_pay}}</code></p>
      {% else %}
        <p class="err">✗ Could not capture payment tooltip.</p>
        <p><strong>SHA-256:</strong> <code>N/A</code></p>
      {% endif %}
    </div>
  </div>
</body>
</html>
"""

# ---------- main ----------
if __name__ == "__main__":
    app.run(host="0.0.0.0", port=PORT)
