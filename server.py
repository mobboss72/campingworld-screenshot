# server.py
import os
import sys
import hashlib
import datetime
import tempfile
import traceback
import requests

from flask import Flask, request, send_from_directory, Response, render_template_string, send_file
from playwright.sync_api import sync_playwright

os.environ.setdefault("PLAYWRIGHT_BROWSERS_PATH", "/app/ms-playwright")

PORT = int(os.getenv("PORT", "8080"))
OREGON_ZIP = os.getenv("OREGON_ZIP", "97201")

screenshot_cache = {}
app = Flask(__name__, static_folder=None)

@app.get("/")
def root():
    return send_from_directory(".", "index.html")

@app.get("/screenshot/<sid>")
def serve_shot(sid):
    path = screenshot_cache.get(sid)
    if not path or not os.path.exists(path):
        return Response("Screenshot not found", status=404)
    return send_file(path, mimetype="image/png")

@app.post("/capture")
def capture():
    try:
        stock = (request.form.get("stock") or "").strip()
        if not stock.isdigit():
            return Response("Invalid stock number", status=400)

        price_png, pay_png, url = do_capture(stock)

        utc_now = datetime.datetime.utcnow().strftime("%Y-%m-%d %H:%M:%S UTC")
        https_hdr_date = https_date()

        price_ok = os.path.exists(price_png)
        pay_ok = os.path.exists(pay_png)

        sha_price = sha256_file(price_png) if price_ok else "N/A"
        sha_payment = sha256_file(pay_png) if pay_ok else "N/A"

        price_id = f"price_{stock}_{int(datetime.datetime.utcnow().timestamp())}"
        pay_id = f"payment_{stock}_{int(datetime.datetime.utcnow().timestamp())}"
        if price_ok: screenshot_cache[price_id] = price_png
        if pay_ok: screenshot_cache[pay_id] = pay_png

        html = render_template_string("""
<!doctype html>
<html>
<head>
<meta charset="utf-8">
<title>Camping World Compliance Capture</title>
<style>
  body{font-family:system-ui,-apple-system,Segoe UI,Roboto,Arial,sans-serif;background:#f5f5f7;margin:24px;}
  h1{margin:0 0 14px;}
  .hdr{background:#fff;border:1px solid #ddd;border-radius:10px;padding:14px 16px;margin-bottom:18px}
  .grid{display:grid;grid-template-columns:1fr 1fr;gap:18px}
  .card{background:#fff;border:1px solid #ddd;border-radius:10px;padding:14px 16px}
  .card h2{margin:0 0 10px}
  .frame{border:1px solid #e5e5e5;background:#fafafa;min-height:260px;padding:10px;border-radius:8px}
  img{max-width:100%;height:auto;display:block;border:1px solid #ddd}
  .ok{color:#137333;font-weight:600}
  .err{color:#b00020;font-weight:600}
  code{font-family:ui-monospace,Menlo,Consolas,monospace}
  .kv{margin:2px 0}
</style>
</head>
<body>
  <h1>Camping World Compliance Capture</h1>
  <div class="hdr">
    <div class="kv"><strong>Stock:</strong> {{stock}}</div>
    <div class="kv"><strong>URL:</strong> <a href="{{url}}" target="_blank">{{url}}</a></div>
    <div class="kv"><strong>UTC:</strong> {{utc_now}}</div>
    <div class="kv"><strong>HTTPS Date:</strong> {{https_hdr_date or 'unavailable'}}</div>
  </div>

  <div class="grid">
    <div class="card">
      <h2>Price Tooltip — Full Page</h2>
      <div class="frame">
        {% if price_ok %}
          <img src="/screenshot/{{price_id}}" alt="Price tooltip">
          <div class="ok">✔ Captured</div>
        {% else %}
          <div class="err">✗ Could not capture price tooltip.</div>
        {% endif %}
      </div>
      <div class="kv"><strong>SHA-256:</strong> <code>{{sha_price}}</code></div>
    </div>

    <div class="card">
      <h2>Payment Tooltip — Full Page</h2>
      <div class="frame">
        {% if pay_ok %}
          <img src="/screenshot/{{pay_id}}" alt="Payment tooltip">
          <div class="ok">✔ Captured</div>
        {% else %}
          <div class="err">✗ Could not capture payment tooltip.</div>
        {% endif %}
      </div>
      <div class="kv"><strong>SHA-256:</strong> <code>{{sha_payment}}</code></div>
    </div>
  </div>
</body>
</html>
        """, stock=stock, url=url, utc_now=utc_now, https_hdr_date=https_hdr_date,
           price_ok=price_ok, pay_ok=pay_ok, price_id=price_id, pay_id=pay_id,
           sha_price=sha_price, sha_payment=sha_payment)

        return Response(html, mimetype="text/html")
    except Exception as e:
        print("❌ /capture failed:", e, file=sys.stderr)
        traceback.print_exc()
        return Response(f"Error: {e}", status=500)

# ---------- helpers ----------

def sha256_file(path: str) -> str:
    if not os.path.exists(path):
        return "N/A"
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

# ---------- core capture ----------

def do_capture(stock: str) -> tuple[str, str, str]:
    url = f"https://rv.campingworld.com/rv/{stock}"
    tmpdir = tempfile.mkdtemp(prefix=f"cw-{stock}-")
    price_png = os.path.join(tmpdir, f"cw_{stock}_price.png")
    pay_png = os.path.join(tmpdir, f"cw_{stock}_payment.png")

    print(f"\n==> goto domcontentloaded: {url}")
    with sync_playwright() as p:
        browser = p.chromium.launch(
            headless=True,
            args=["--no-sandbox","--disable-setuid-sandbox","--disable-dev-shm-usage"]
        )
        ctx = browser.new_context(
            viewport={"width":1920,"height":1080},
            geolocation={"latitude":45.5122,"longitude":-122.6587},
            permissions=["geolocation"],
            locale="en-US",
            user_agent="Mozilla/5.0 Chrome"
        )
        page = ctx.new_page()

        page.goto(url, wait_until="domcontentloaded")
        try:
            page.wait_for_load_state("networkidle", timeout=30_000)
            print("networkidle reached")
        except Exception:
            print("networkidle timeout (ignored)")

        # Force Oregon ZIP (best-effort)
        try:
            page.evaluate("""(zip)=>{try{localStorage.setItem('cw_zip',zip)}catch(_){};document.cookie='cw_zip='+zip+';path=/;SameSite=Lax';}""", OREGON_ZIP)
            print(f"ZIP injected {OREGON_ZIP}")
            page.reload(wait_until="domcontentloaded")
            try:
                page.wait_for_load_state("networkidle", timeout=20_000)
                print("post-zip networkidle reached")
            except Exception as e:
                print(f"ZIP injection/reload issue: {e}")
        except Exception as e:
            print(f"ZIP script error: {e}")

        # Hide overlays (chat, recaptcha, convertflow)
        try:
            page.add_style_tag(content="""
              [id*="drift-widget"], iframe[src*="drift"], #hubspot-messages-iframe-container,
              [class*="intercom"], [id*="freshchat"], [data-testid="chat-widget"],
              .grecaptcha-badge,
              .convertflow-cta, .cf-overlay, [id^="cta"], [id*="cf-"] {
                display:none !important; visibility:hidden !important; pointer-events:none !important;
              }
            """)
            print("overlay-hiding CSS injected")
        except Exception:
            pass

        # ----- PRICE (kept as the working variant) -----
        try:
            try:
                page.wait_for_selector(".MuiTypography-root.MuiTypography-subtitle1", timeout=8_000)
            except Exception:
                print("price subtitle1 wait timeout (continue)")

            cand = page.locator(".MuiTypography-root.MuiTypography-subtitle1")
            cnt = cand.count()
            print(f"price candidates={cnt}")

            visible_price = None
            for i in range(cnt):
                e = cand.nth(i)
                if e.is_visible():
                    visible_price = e
                    txt = (e.text_content() or "").strip()
                    print(f"price use idx={i} text='{txt}'")
                    break

            if visible_price:
                icon = visible_price.locator("xpath=following::*[name()='svg' and contains(@class,'MuiSvgIcon-root')][1]")
                trigger = icon.first if icon.count() > 0 else visible_price
                if icon.count() > 0:
                    print("price: using adjacent info icon")
                trigger.scroll_into_view_if_needed(timeout=5000)
                trigger.hover(timeout=10_000, force=True)
                page.wait_for_selector("[role='tooltip'].MuiTooltip-popper, .MuiTooltip-popper", state="visible", timeout=5_000)
                print("price tooltip appeared")
                page.wait_for_timeout(350)
                page.screenshot(path=price_png, full_page=True)
                print(f"price screenshot saved {price_png} size={os.path.getsize(price_png)} bytes")
        except Exception as e:
            print(f"ERROR price: {e}")

        # ----- PAYMENT (geometry-nearest Info icon) -----
        try:
            pay_anchor = page.locator(
                "xpath=//*[contains(normalize-space(.), '/mo') "
                "or contains(translate(normalize-space(.),'PAYMENT','payment'),'payment') "
                "or contains(translate(normalize-space(.),'MONTH','month'),'month')]"
            ).first
            print("payment anchor via /mo|payment|month")

            if not pay_anchor or pay_anchor.count() == 0:
                raise RuntimeError("payment anchor not found")

            pay_anchor.scroll_into_view_if_needed(timeout=5000)
            print("payment anchor scrolled into view")

            # Find nearest info icon by distance to anchor (ElementHandle, not Locator)
            anchor_handle = pay_anchor.element_handle()
            icon_handle = page.evaluate_handle("""
                (anchor) => {
                  const rectA = anchor.getBoundingClientRect();
                  const icons = Array.from(document.querySelectorAll('svg.MuiSvgIcon-root'));
                  if (!icons.length) return null;
                  let best = null, bestD = Infinity;
                  const ax = rectA.left + rectA.width/2, ay = rectA.top + rectA.height/2;
                  for (const s of icons) {
                    const r = s.getBoundingClientRect();
                    const cx = r.left + r.width/2, cy = r.top + r.height/2;
                    const d = Math.hypot(cx-ax, cy-ay);
                    // prefer icons visually on the same horizontal band near the anchor text
                    const bandPenalty = Math.abs((r.top + r.bottom)/2 - ay) * 0.2;
                    const score = d + bandPenalty;
                    if (score < bestD) { bestD = score; best = s; }
                  }
                  return best;
                }
            """, anchor_handle)

            trigger_handle = icon_handle or anchor_handle  # fallback to text

            # Open tooltip: hover → synthetic events → focus
            try:
                trigger_handle.hover(force=True, timeout=10_000)
            except Exception:
                pass

            page.wait_for_timeout(120)
            try:
                page.evaluate("""(el) => {
                  for (const type of ['pointerover','mouseover','mouseenter','pointermove']) {
                    el.dispatchEvent(new MouseEvent(type, {bubbles:true,cancelable:true,view:window}));
                  }
                }""", trigger_handle)
            except Exception:
                pass

            try:
                trigger_handle.focus()
            except Exception:
                pass

            page.wait_for_selector("[role='tooltip'].MuiTooltip-popper, .MuiTooltip-popper", state="visible", timeout=4_000)
            print("payment tooltip appeared")
            page.wait_for_timeout(350)
            page.screenshot(path=pay_png, full_page=True)
            print(f"payment screenshot saved {pay_png} size={os.path.getsize(pay_png)} bytes")
        except Exception as e:
            print(f"ERROR payment: {e}")

        print(f"price file: {'exists' if os.path.exists(price_png) else 'missing'} ({os.path.getsize(price_png) if os.path.exists(price_png) else 'n/a'} bytes)")
        print(f"payment file: {'exists' if os.path.exists(pay_png) else 'missing'} ({os.path.getsize(pay_png) if os.path.exists(pay_png) else 'n/a'})")

        browser.close()

    return price_png, pay_png, url

if __name__ == "__main__":
    app.run(host="0.0.0.0", port=PORT)
