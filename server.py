# server.py
import os
import sys
import hashlib
import datetime as dt
import tempfile
import traceback
from pathlib import Path

import requests
from flask import Flask, request, send_from_directory, Response, render_template_string, send_file
from playwright.sync_api import sync_playwright, TimeoutError as PWTimeout

# ──────────────────────────────────────────────────────────────────────────────
# Config
# ──────────────────────────────────────────────────────────────────────────────
APP_VERSION = "1.7.0"
PORT = int(os.getenv("PORT", "8080"))
OREGON_ZIP = os.getenv("OREGON_ZIP", "97201")
ENABLE_TRACE = os.getenv("PLAYWRIGHT_TRACE", "0") in ("1", "true", "True")

# Persist Playwright browser binaries in a writable path (Railway/Heroku-friendly)
os.environ.setdefault("PLAYWRIGHT_BROWSERS_PATH", "/app/ms-playwright")

# In-memory map of id → file path (simple cache for serving screenshots)
screenshot_cache: dict[str, str] = {}

app = Flask(__name__, static_folder=None)

# ──────────────────────────────────────────────────────────────────────────────
# Routes
# ──────────────────────────────────────────────────────────────────────────────
@app.get("/")
def root():
    return send_from_directory(".", "index.html")

@app.get("/screenshot/<screenshot_id>")
def serve_screenshot(screenshot_id: str):
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

        price_png, payment_png, url = do_capture(stock)

        utc_now = dt.datetime.utcnow().strftime("%Y-%m-%d %H:%M:%S UTC")
        https_hdr = https_date()

        price_exists = Path(price_png).exists()
        pay_exists   = Path(payment_png).exists()

        sha_price   = sha256_file(price_png) if price_exists else "N/A"
        sha_payment = sha256_file(payment_png) if pay_exists   else "N/A"

        price_id = f"price_{stock}_{int(dt.datetime.utcnow().timestamp())}"
        pay_id   = f"payment_{stock}_{int(dt.datetime.utcnow().timestamp())}"
        if price_exists: screenshot_cache[price_id] = price_png
        if pay_exists:   screenshot_cache[pay_id]   = payment_png

        html = render_template_string(
            """
<!DOCTYPE html>
<html>
<head>
  <meta charset="utf-8"/>
  <title>Camping World Compliance Capture</title>
  <style>
    :root { --card:#fff; --ink:#111; --muted:#666; --line:#ddd; --ok:#138000; --err:#b00020; --bg:#f6f7fb; }
    body{ font-family:system-ui,-apple-system,Segoe UI,Roboto,Inter,Helvetica,Arial,sans-serif; margin:24px; background:var(--bg); color:var(--ink);}
    h1{ margin:0 0 16px; }
    .meta{ background:var(--card); border:1px solid var(--line); border-radius:12px; padding:16px; margin-bottom:18px; }
    .meta b{ display:inline-block; width:110px; color:#333; }
    .grid{ display:grid; grid-template-columns: 1fr 1fr; gap:18px; }
    .card{ background:var(--card); border:1px solid var(--line); border-radius:12px; overflow:hidden;}
    .card h2{ margin:0; padding:14px 16px; border-bottom:1px solid var(--line); font-size:20px;}
    .body{ padding:14px 16px;}
    img{ max-width:100%; display:block; border:1px solid #eee; }
    .ok{ color:var(--ok); font-weight:700; margin-top:8px;}
    .err{ color:var(--err); font-weight:700; margin-top:8px;}
    code{ background:#f0f2f5; padding:2px 6px; border-radius:6px; }
    .sha{ margin-top:10px; color:#333;}
    .small{ color:var(--muted); font-size:12px;}
  </style>
</head>
<body>
  <h1>Camping World Compliance Capture</h1>
  <div class="meta">
    <div><b>Stock:</b> {{stock}}</div>
    <div><b>URL:</b> <a href="{{url}}" target="_blank" rel="noopener">{{url}}</a></div>
    <div><b>UTC:</b> {{utc}}</div>
    <div><b>HTTPS Date:</b> {{https or 'unavailable'}}</div>
    <div class="small">App v{{version}}</div>
  </div>

  <div class="grid">
    <div class="card">
      <h2>Price Tooltip — Full Page</h2>
      <div class="body">
        {% if price_exists %}
          <img src="/screenshot/{{price_id}}" alt="Price tooltip full page"/>
          <div class="ok">✓ Captured</div>
          <div class="sha"><b>SHA-256:</b> <code>{{sha_price}}</code></div>
        {% else %}
          <div class="err">✗ Could not capture price tooltip.</div>
          <div class="sha"><b>SHA-256:</b> <code>{{sha_price}}</code></div>
        {% endif %}
      </div>
    </div>

    <div class="card">
      <h2>Payment Tooltip — Full Page</h2>
      <div class="body">
        {% if pay_exists %}
          <img src="/screenshot/{{pay_id}}" alt="Payment tooltip full page"/>
          <div class="ok">✓ Captured</div>
          <div class="sha"><b>SHA-256:</b> <code>{{sha_payment}}</code></div>
        {% else %}
          <div class="err">✗ Could not capture payment tooltip.</div>
          <div class="sha"><b>SHA-256:</b> <code>{{sha_payment}}</code></div>
        {% endif %}
      </div>
    </div>
  </div>
</body>
</html>
            """,
            stock=stock,
            url=url,
            utc=utc_now,
            https=https_hdr,
            version=APP_VERSION,
            price_exists=price_exists,
            pay_exists=pay_exists,
            price_id=price_id,
            pay_id=pay_id,
            sha_price=sha_price,
            sha_payment=sha_payment,
        )
        return Response(html, mimetype="text/html")

    except Exception as e:
        print("❌ /capture failed:", e, file=sys.stderr)
        traceback.print_exc()
        return Response(f"Error: {e}", status=500)

# ──────────────────────────────────────────────────────────────────────────────
# Helpers
# ──────────────────────────────────────────────────────────────────────────────
def sha256_file(path: str) -> str:
    try:
        h = hashlib.sha256()
        with open(path, "rb") as f:
            for chunk in iter(lambda: f.read(1 << 20), b""):
                h.update(chunk)
        return h.hexdigest()
    except Exception:
        return "N/A"

def https_date() -> str | None:
    try:
        r = requests.head("https://cloudflare.com", timeout=8)
        return r.headers.get("Date")
    except Exception:
        return None

# ──────────────────────────────────────────────────────────────────────────────
# Core capture
# ──────────────────────────────────────────────────────────────────────────────
def do_capture(stock: str) -> tuple[str, str, str]:
    url = f"https://rv.campingworld.com/rv/{stock}"
    tmp = Path(tempfile.mkdtemp(prefix=f"cw-{stock}-"))
    price_png   = str(tmp / f"cw_{stock}_price.png")
    payment_png = str(tmp / f"cw_{stock}_payment.png")

    print(f"\n--- capture {stock} ---")
    print(f"goto domcontentloaded: {url}")

    with sync_playwright() as p:
        browser = p.chromium.launch(
            headless=True,
            args=["--no-sandbox", "--disable-setuid-sandbox", "--disable-dev-shm-usage"],
        )
        ctx = browser.new_context(
            viewport={"width": 1920, "height": 1080},
            locale="en-US",
            geolocation={"latitude": 45.5122, "longitude": -122.6587},
            permissions=["geolocation"],
            user_agent="Mozilla/5.0 Chrome",
        )
        page = ctx.new_page()

        # Optional tracing (set PLAYWRIGHT_TRACE=1)
        if ENABLE_TRACE:
            ctx.tracing.start(screenshots=True, snapshots=True, sources=True)

        page.goto(url, wait_until="domcontentloaded")
        try:
            page.wait_for_load_state("networkidle", timeout=30_000)
            print("networkidle reached")
        except PWTimeout:
            print("networkidle timeout (ignored)")

        # Force Oregon zip; some pages don’t refetch XHR on reload reliably, so ignore timeout
        try:
            page.evaluate(
                """(zip) => {
                   try { localStorage.setItem('cw_zip', String(zip)); } catch {}
                   document.cookie = 'cw_zip='+String(zip)+';path=/;SameSite=Lax';
                }""",
                OREGON_ZIP,
            )
            print(f"ZIP injected {OREGON_ZIP}")
            page.reload(wait_until="load")
            page.wait_for_load_state("networkidle", timeout=20_000)
            print("post-zip networkidle reached")
        except Exception as e:
            print(f"ZIP injection/reload issue: {e}\n=========================== logs ===========================")
            print('"load" event fired')
            print("============================================================")

        # Hide sticky overlays that can obscure tooltips
        inject_overlay_hider(page)

        # PRICE TOOLTIP (known-good path)
        try:
            capture_price_tooltip(page, price_png)
        except Exception:
            print("❌ price capture block failed:")
            traceback.print_exc()

        # PAYMENT TOOLTIP (robust search for the little “i” near /mo or payment text)
        try:
            capture_payment_tooltip(page, payment_png)
        except Exception:
            print("❌ payment capture block failed:")
            traceback.print_exc()

        # finalize trace
        if ENABLE_TRACE:
            trace_path = str(tmp / "trace.zip")
            try:
                ctx.tracing.stop(path=trace_path)
                print(f"trace saved: {trace_path}")
            except Exception as e:
                print(f"trace stop error: {e}")

        browser.close()

    print(f"price file: {'exists' if Path(price_png).exists() else 'missing'} "
          f"({Path(price_png).stat().st_size if Path(price_png).exists() else 'n/a'} bytes)")
    print(f"payment file: {'exists' if Path(payment_png).exists() else 'missing'} "
          f"({Path(payment_png).stat().st_size if Path(payment_png).exists() else 'n/a'} bytes)")
    return price_png, payment_png, url

# ──────────────────────────────────────────────────────────────────────────────
# DOM helpers & capture routines
# ──────────────────────────────────────────────────────────────────────────────
def inject_overlay_hider(page):
    css = """
    /* keep tooltips visible; nuke sticky overlays that can cover them */
    .intercom-lightweight-app, .intercom-launcher, [id*="chat"], [class*="chat"], [class*="cookie"], [class*="gdpr"],
    .ReactModalPortal, .ReactModal__Overlay, .grecaptcha-badge { display:none !important; visibility:hidden !important; }
    """
    page.add_style_tag(content=css)
    print("overlay-hiding CSS injected")

def wait_for_tooltip(page, timeout_ms=5000):
    sel = "[role='tooltip'], .MuiTooltip-popper, .MuiTooltip-popperInteractive, .base-Popper-root.MuiTooltip-popper"
    page.wait_for_selector(sel, state="visible", timeout=timeout_ms)

def capture_price_tooltip(page, out_path: str):
    # Wait for price row (don’t fail hard if it times out)
    try:
        page.wait_for_selector(".MuiTypography-root.MuiTypography-subtitle1", state="visible", timeout=15_000)
    except PWTimeout:
        print("price subtitle1 wait timeout (continue)")

    price_sel = ".MuiTypography-root.MuiTypography-subtitle1:visible"
    price_elems = page.locator(price_sel)
    count = max(0, min(8, price_elems.count()))
    print(f"price candidates={count}")

    visible = None
    pick_idx = -1
    for i in range(count):
        el = price_elems.nth(i)
        if not el.is_visible():
            continue
        txt = (el.text_content() or "").strip()
        if "$" in txt:
            visible, pick_idx = el, i
            break
        if visible is None:
            visible, pick_idx = el, i
    if not visible:
        print("no visible price element found")
        return

    print(f"price use idx={pick_idx} text='{(visible.text_content() or '').strip()}'")

    # Prefer the little info icon next to the price if present
    icon = visible.locator("xpath=following::*[contains(@class,'MuiSvgIcon') or contains(@data-testid,'Info')][1]")
    trigger = icon.first if icon.count() > 0 else visible
    if icon.count() > 0:
        print("price: using adjacent info icon")

    trigger.scroll_into_view_if_needed(timeout=5000)
    trigger.hover(timeout=10_000, force=True)
    wait_for_tooltip(page, timeout_ms=5_000)
    print("price tooltip appeared")
    page.wait_for_timeout(400)
    page.screenshot(path=out_path, full_page=True)
    print(f"price screenshot saved {out_path} size={Path(out_path).stat().st_size} bytes")

def capture_payment_tooltip(page, out_path: str):
    # Anchor text that hints “payment” block
    xp = (
        "xpath=//*[contains(normalize-space(.), '/mo') or "
        "contains(translate(normalize-space(.),'PAYMENT','payment'),'payment') or "
        "contains(translate(normalize-space(.),'MONTH','month'),'month')]"
    )
    anchor = page.locator(xp).first
    print("payment anchor via", xp)
    if anchor.count() == 0:
        print("payment: no anchor text found")
        return

    anchor.scroll_into_view_if_needed(timeout=5000)
    print("payment anchor scrolled into view")

    # Look for the “i” info icon close to the anchor
    proximity_icon = anchor.locator(
        "xpath=(following::*[self::svg or contains(@class,'MuiSvgIcon') or contains(@data-testid,'Info')][1] | "
        "preceding::*[self::svg or contains(@class,'MuiSvgIcon') or contains(@data-testid,'Info')][1])"
    )
    info_cluster = page.locator("css=span[aria-describedby*='mui'], [aria-label*='info i'], [role='img'][class*='Info']")
    icon = None
    if proximity_icon.count() > 0:
        icon = proximity_icon.first
        print("payment: using proximity trigger (aria-describedby/info-nearby)")
    else:
        # Fallback: scan all visible info icons and pick one whose nearest text includes /mo or payment
        candidates = page.locator("css=svg[data-testid*='Info'], svg[class*='InfoOutlinedIcon'], [aria-label*='info']")
        n = min(15, candidates.count())
        picked_idx = -1
        for i in range(n):
            c = candidates.nth(i)
            if not c.is_visible():
                continue
            txt = (c.locator("xpath=ancestor::*[self::div or self::span][1]").text_content() or "").lower()
            if "/mo" in txt or "payment" in txt or "month" in txt:
                picked_idx = i
                icon = c
                break
        if icon is not None:
            print(f"payment: chose global info icon idx={picked_idx}")
    trigger = icon if icon is not None and icon.count() > 0 else anchor
    if icon is None or icon.count() == 0:
        print("payment: no icon trigger found; using text as trigger")

    # Hover in a small offset toward the icon’s center if possible
    try:
        box = trigger.bounding_box()
        if box:
            trigger.hover(position={"x": max(2, int(box["width"] * 0.6)), "y": int(box["height"] // 2)}, force=True, timeout=10_000)
        else:
            trigger.hover(force=True, timeout=10_000)
    except PWTimeout:
        trigger.hover(force=True, timeout=10_000)

    # Wait for a real MUI tooltip (role=tooltip / popper)
    try:
        wait_for_tooltip(page, timeout_ms=5_000)
        print("payment tooltip appeared")
        page.wait_for_timeout(450)
        page.screenshot(path=out_path, full_page=True)
        print(f"payment screenshot saved {out_path} size={Path(out_path).stat().st_size} bytes")
    except PWTimeout:
        print("payment: tooltip never appeared")

# ──────────────────────────────────────────────────────────────────────────────
if __name__ == "__main__":
    app.run(host="0.0.0.0", port=PORT)
