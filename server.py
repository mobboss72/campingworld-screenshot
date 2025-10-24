# server.py
import os
import sys
import hashlib
import datetime
import tempfile
import traceback
import requests
from flask import Flask, request, send_from_directory, Response, render_template_string, send_file
from playwright.sync_api import sync_playwright, TimeoutError as PWTimeout

# ---------------- Config ----------------
os.environ.setdefault("PLAYWRIGHT_BROWSERS_PATH", "/app/ms-playwright")
PORT = int(os.getenv("PORT", "8080"))
OREGON_ZIP = os.getenv("OREGON_ZIP", "97201")
APP_VERSION = "2025-10-24T01:45Z  dual-tooltips + payment-proximity (evaluate-args fix)"

# ---------------- App ----------------
app = Flask(__name__, static_folder=None)
screenshot_cache = {}

@app.get("/")
def root():
    return send_from_directory(".", "index.html")

@app.get("/version")
def version():
    return APP_VERSION, 200, {"Content-Type": "text/plain"}

@app.get("/screenshot/<sid>")
def screenshot(sid):
    path = screenshot_cache.get(sid)
    if not path or not os.path.exists(path):
        return Response("Not found", status=404)
    return send_file(path, mimetype="image/png")

@app.post("/capture")
def capture():
    try:
        stock = (request.form.get("stock") or "").strip()
        if not stock.isdigit():
            return Response("Invalid stock number", status=400)

        price_path, pay_path, url, log = do_capture(stock)

        utc_now = datetime.datetime.utcnow().strftime("%Y-%m-%d %H:%M:%S UTC")
        https_dt = get_https_date()
        ts = int(datetime.datetime.utcnow().timestamp())

        price_ok = os.path.exists(price_path)
        pay_ok = os.path.exists(pay_path)

        sha_price = sha256_file(price_path) if price_ok else "N/A"
        sha_pay = sha256_file(pay_path) if pay_ok else "N/A"

        price_id = f"price_{stock}_{ts}"
        pay_id = f"payment_{stock}_{ts}"
        if price_ok: screenshot_cache[price_id] = price_path
        if pay_ok:   screenshot_cache[pay_id] = pay_path

        html = render_template_string(HTML_TEMPLATE,
            stock=stock, url=url, utc_now=utc_now, hdate=https_dt,
            price_exists=price_ok, payment_exists=pay_ok,
            price_id=price_id, payment_id=pay_id,
            sha_price=sha_price, sha_payment=sha_pay,
            log_text="\n".join(log[-250:]), version=APP_VERSION
        )
        return Response(html, mimetype="text/html")
    except Exception as e:
        print("❌ /capture failed:", e, file=sys.stderr)
        traceback.print_exc()
        return Response(f"Error: {e}", status=500)

# ---------------- Helpers ----------------
def sha256_file(path: str) -> str:
    if not os.path.exists(path):
        return "N/A"
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()

def get_https_date() -> str | None:
    try:
        r = requests.head("https://cloudflare.com", timeout=8)
        return r.headers.get("Date")
    except Exception:
        return None

def _fsize(path):
    try: return f"{os.path.getsize(path)} bytes"
    except Exception: return "n/a"

# ---------------- Core Capture ----------------
def do_capture(stock: str):
    url = f"https://rv.campingworld.com/rv/{stock}"
    tmpdir = tempfile.mkdtemp(prefix=f"cw-{stock}-")
    price_png = os.path.join(tmpdir, f"cw_{stock}_price.png")
    pay_png   = os.path.join(tmpdir, f"cw_{stock}_payment.png")
    log = [f"goto domcontentloaded: {url}"]

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

        # Navigate
        page.goto(url, wait_until="domcontentloaded")
        try:
            page.wait_for_load_state("networkidle", timeout=30_000)
            log.append("networkidle reached")
        except Exception:
            log.append("networkidle timeout (ignored)")

        # ZIP (Oregon)
        try:
            page.evaluate(
                """(zip)=>{
                    try { localStorage.setItem('cw_zip', zip); } catch(e){}
                    document.cookie='cw_zip='+zip+';path=/;SameSite=Lax';
                }""",
                OREGON_ZIP,
            )
            log.append(f"ZIP injected {OREGON_ZIP}")
            try:
                page.reload(wait_until="domcontentloaded")
                page.wait_for_load_state("networkidle", timeout=20_000)
                log.append("post-zip networkidle reached")
            except PWTimeout as e:
                log.append(f"ZIP injection/reload issue: {e}")
        except Exception as e:
            log.append(f"ZIP injection error: {e}")

        # Hide overlays/popups that block hover
        try:
            page.add_style_tag(content="""
                .convertflow-cta, .cf-overlay, .cf-prehook-popup,
                [id^="cta_"], [id^="cta"], .cf-section-overlay-outer,
                #onetrust-banner-sdk, .ot-sdk-container {
                    display:none!important; visibility:hidden!important; pointer-events:none!important;
                }
            """)
            log.append("overlay-hiding CSS injected")
        except Exception as e:
            log.append(f"overlay CSS error: {e}")

        # ----- PRICE (known-good) -----
        try:
            capture_price(page, price_png, log)
        except Exception as e:
            log.append(f"price block error: {e}")

        # Reset hover/focus before next capture
        try:
            page.mouse.move(5, 5)
            page.keyboard.press("Escape")
        except Exception:
            pass

        # ----- PAYMENT (proximity hunt) -----
        try:
            capture_payment(page, pay_png, log)
        except Exception as e:
            log.append(f"payment block error: {e}")

        browser.close()

    log.append(f"price file: {'exists' if os.path.exists(price_png) else 'missing'} ({_fsize(price_png)})")
    log.append(f"payment file: {'exists' if os.path.exists(pay_png) else 'missing'} ({_fsize(pay_png)})")
    return price_png, pay_png, url, log

# ---------------- Price Hover ----------------
def capture_price(page, out_path, log):
    sel = ".MuiTypography-root.MuiTypography-subtitle1"
    try:
        page.wait_for_selector(sel, state="visible", timeout=5000)
    except Exception:
        log.append("price subtitle1 wait timeout (continue)")

    elems = page.locator(sel)
    count = elems.count()
    log.append(f"price candidates={count}")
    if count == 0:
        log.append("price: no elements")
        return

    # Prefer the last $ element (often the breakdown row is second)
    idx = 0
    for i in range(count):
        t = (elems.nth(i).text_content() or "").strip()
        if "$" in t:
            idx = i
    target = elems.nth(idx)
    ttxt = (target.text_content() or "").strip()
    log.append(f"price use idx={idx} text='{ttxt}'")

    target.scroll_into_view_if_needed()

    # Adjacent info icon if present
    icon = target.locator(
        "xpath=following::*[contains(@class,'MuiSvgIcon-root') or contains(@data-testid,'Info') or contains(@class,'MuiTooltip')][1]"
    )
    trigger = icon.first if icon.count() > 0 else target
    if icon.count() > 0:
        log.append("price: using adjacent info icon")
    else:
        log.append("price: icon not found, using text node")

    _show_tooltip(page, trigger, log, label="price")
    _wait_tooltip_visible(page, log, label="price")
    page.wait_for_timeout(300)
    page.screenshot(path=out_path, full_page=True)
    log.append(f"price screenshot saved {out_path} size={_fsize(out_path)}")

# ---------------- Payment Hover (proximity + fallbacks) ----------------
def capture_payment(page, out_path, log):
    # 1) Find a visible payment anchor line
    anchor_xpath = ("xpath=//*[contains(normalize-space(.), '/mo') "
                    "or contains(translate(normalize-space(.),'PAYMENT','payment'),'payment') "
                    "or contains(translate(normalize-space(.),'MONTH','month'),'month')]")
    anchor = page.locator(anchor_xpath)
    log.append("payment anchor via " + anchor_xpath)

    if anchor.count() == 0:
        log.append("payment: no text anchor found")
        return

    vis = None
    for i in range(anchor.count()):
        cand = anchor.nth(i)
        if cand.is_visible():
            vis = cand
            break
    if vis is None:
        log.append("payment: all anchors invisible")
        return

    vis.scroll_into_view_if_needed()
    log.append("payment anchor scrolled into view")

    # 2) Proximity trigger finder (pass ONE arg object to evaluate)
    data_key = f"data-paytrigger-{int(datetime.datetime.utcnow().timestamp())}"
    found = False
    try:
        found = page.evaluate(
            """({anchorSel, dataKey}) => {
                const anchor = document.evaluate(anchorSel, document, null, XPathResult.FIRST_ORDERED_NODE_TYPE, null).singleNodeValue;
                if (!anchor) return false;

                let row = anchor.closest('[class*="MuiStack"], [class*="MuiBox"], [class*="MuiGrid"], [class*="MuiTypography"]') || anchor.parentElement;
                if (!row) row = anchor.parentElement || document.body;

                const cands = Array.from(row.querySelectorAll('[aria-describedby], [title], [aria-label], svg, button, span, i'));
                const ar = anchor.getBoundingClientRect();
                let best = null, bestScore = 1e9;

                for (const el of cands) {
                  if (!(el instanceof HTMLElement)) continue;
                  const r = el.getBoundingClientRect();
                  if (!r.width || !r.height) continue;

                  const area = r.width * r.height;
                  if (area > 50000) continue;

                  const cx = r.left + r.width/2, cy = r.top + r.height/2;
                  const ax = ar.left + ar.width*0.8, ay = ar.top + ar.height/2;
                  const dx = cx - ax, dy = cy - ay;
                  const dist = Math.hypot(dx, dy);

                  const hasAD = el.hasAttribute('aria-describedby');
                  const looksInfo = /info|ⓘ|!/i.test((el.getAttribute('aria-label')||'')+(el.getAttribute('title')||'')+(el.className||''));
                  let score = dist + (hasAD ? -40 : 0) + (looksInfo ? -20 : 0) + (area/400.0);

                  if (Math.abs(dy) > 60) score += 200;

                  if (!best || score < bestScore) { best = el; bestScore = score; }
                }

                if (best) {
                  best.setAttribute(dataKey, "1");
                  return true;
                }
                return false;
            }""",
            {"anchorSel": anchor_xpath.replace("xpath=", ""), "dataKey": data_key},
        )
    except Exception as e:
        log.append(f"payment proximity eval error: {e}")

    trigger = None
    if found:
        trigger = page.locator(f'[{data_key}="1"]').first
        if trigger.count() > 0 and trigger.first.is_visible():
            log.append("payment: using proximity trigger (aria-describedby/info-nearby)")
    else:
        # 3) No proximity trigger: try an obvious adjacent icon after the anchor
        icon = vis.locator(
            "xpath=following::*[contains(@class,'MuiSvgIcon-root') or contains(@data-testid,'Info') or @aria-label][1]"
        )
        if icon.count() > 0 and icon.first.is_visible():
            trigger = icon.first
            log.append("payment: using adjacent info icon")
        else:
            # 4) Last fallback: use the anchor text itself
            trigger = vis
            log.append("payment: fallback to text as trigger")

    # 5) Try to show tooltip (hover → focus → sweep → click)
    _show_tooltip(page, trigger, log, label="payment")

    # 6) Wait & capture if something appeared
    if _wait_tooltip_visible(page, log, label="payment", soft=True):
        page.wait_for_timeout(300)
        page.screenshot(path=out_path, full_page=True)
        log.append(f"payment screenshot saved {out_path} size={_fsize(out_path)}")
        return

    # 7) As a last diagnostic nudge: sweep a tight hitbox around the row
    try:
        box = vis.bounding_box()
        if box:
            y = box["y"] + box["height"] * 0.5
            for xfac in [0.1, 0.25, 0.4, 0.55, 0.7, 0.85, 0.95]:
                page.mouse.move(box["x"] + box["width"]*xfac, y)
                page.wait_for_timeout(120)
                if _wait_tooltip_visible(page, log, label="payment", soft=True):
                    page.wait_for_timeout(200)
                    page.screenshot(path=out_path, full_page=True)
                    log.append(f"payment screenshot saved (sweep) {out_path} size={_fsize(out_path)}")
                    return
    except Exception:
        pass

    log.append("payment: tooltip never appeared")

# ---------------- Tooltip helpers ----------------
def _wait_tooltip_visible(page, log, label="", soft=False):
    sel = "[role='tooltip'], .MuiTooltip-popper, [data-popper-placement], .MuiPopover-root .MuiPaper-root"
    try:
        page.wait_for_selector(sel, state="visible", timeout=5000)
        log.append(f"{label} tooltip appeared")
        return True
    except PWTimeout:
        if not soft:
            raise
        return False

def _show_tooltip(page, trigger, log, label=""):
    # 1) Hover
    try:
        trigger.hover(timeout=8000, force=True)
        page.wait_for_timeout(350)
        if _wait_tooltip_visible(page, log, label=label, soft=True):
            return
    except Exception:
        pass

    # 2) Focus
    try:
        trigger.focus()
        page.wait_for_timeout(200)
        if _wait_tooltip_visible(page, log, label=label, soft=True):
            return
    except Exception:
        pass

    # 3) Mouse move sweep over the trigger box
    try:
        box = trigger.bounding_box()
        if box:
            steps = [
                (box["x"] + box["width"] * 0.15, box["y"] + box["height"] * 0.5),
                (box["x"] + box["width"] * 0.5,  box["y"] + box["height"] * 0.5),
                (box["x"] + box["width"] * 0.85, box["y"] + box["height"] * 0.5),
            ]
            for (mx, my) in steps:
                page.mouse.move(mx, my)
                page.wait_for_timeout(180)
                if _wait_tooltip_visible(page, log, label=label, soft=True):
                    return
    except Exception:
        pass

    # 4) Click as last resort
    try:
        trigger.click(timeout=3000, force=True)
        page.wait_for_timeout(250)
        if _wait_tooltip_visible(page, log, label=label, soft=True):
            return
    except Exception:
        pass

# ---------------- HTML ----------------
HTML_TEMPLATE = """
<!DOCTYPE html>
<html>
<head>
  <meta charset="utf-8" />
  <title>Compliance Screenshots</title>
  <style>
    body { font-family: Arial, sans-serif; margin: 20px; background: #f5f5f5; }
    h2 { text-align: center; margin-bottom: 10px; }
    .version { text-align:center; color:#666; margin-bottom:20px; font-size: 12px; }
    .container { display: flex; flex-wrap: wrap; gap: 20px; justify-content: center; }
    .box { border: 2px solid #333; background:#fff; padding: 15px; max-width: 620px; box-shadow: 0 2px 4px rgba(0,0,0,0.1); }
    .box h3 { margin: 0 0 10px 0; border-bottom: 2px solid #333; padding-bottom: 8px; }
    img { max-width: 100%; height: auto; border:1px solid #ddd; display:block; margin: 10px 0; }
    .info { font-size: 0.9em; line-height: 1.5; }
    .success { color: #128a12; font-weight: 600; }
    .error { color: #c00; font-weight: 600; }
    .log { white-space: pre-wrap; font-family: ui-monospace, SFMono-Regular, Menlo, Consolas, monospace; background:#fafafa; border:1px solid #ddd; padding:10px; max-width:1280px; margin: 25px auto 0; }
  </style>
</head>
<body>
  <h2>Camping World – Proof (Compliance Capture)</h2>
  <div class="version">Version: {{ version }}</div>

  <div class="container">
    <div class="box">
      <h3>Price Hover – Full Page</h3>
      {% if price_exists %}
        <img src="/screenshot/{{ price_id }}" alt="Price Hover Full Page" />
        <div class="success">✓ Screenshot captured</div>
      {% else %}
        <div class="error">✗ Failed to capture Price hover</div>
      {% endif %}
      <div class="info">
        <div><b>Stock:</b> {{ stock }}</div>
        <div><b>URL:</b> <a href="{{ url }}" target="_blank" rel="noopener">{{ url }}</a></div>
        <div><b>UTC:</b> {{ utc_now }}</div>
        <div><b>HTTPS Date:</b> {{ hdate or 'unavailable' }}</div>
        <div><b>SHA-256:</b> <code>{{ sha_price }}</code></div>
      </div>
    </div>

    <div class="box">
      <h3>Payment Hover – Full Page</h3>
      {% if payment_exists %}
        <img src="/screenshot/{{ payment_id }}" alt="Payment Hover Full Page" />
        <div class="success">✓ Screenshot captured</div>
      {% else %}
        <div class="error">✗ Failed to capture Payment hover</div>
      {% endif %}
      <div class="info">
        <div><b>Stock:</b> {{ stock }}</div>
        <div><b>URL:</b> <a href="{{ url }}" target="_blank" rel="noopener">{{ url }}</a></div>
        <div><b>UTC:</b> {{ utc_now }}</div>
        <div><b>HTTPS Date:</b> {{ hdate or 'unavailable' }}</div>
        <div><b>SHA-256:</b> <code>{{ sha_payment }}</code></div>
      </div>
    </div>
  </div>

  <div class="log"><b>Log tail</b>\n\n{{ log_text }}</div>
</body>
</html>
"""

# ---------------- Main ----------------
if __name__ == "__main__":
    print("Starting app version:", APP_VERSION)
    app.run(host="0.0.0.0", port=PORT)
