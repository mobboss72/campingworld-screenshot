# server.py
import os, sys, hashlib, datetime, tempfile, traceback, requests
from flask import Flask, request, send_from_directory, Response, render_template_string, send_file
from playwright.sync_api import sync_playwright

# Persist Playwright downloads
os.environ.setdefault("PLAYWRIGHT_BROWSERS_PATH", "/app/ms-playwright")

PORT = int(os.getenv("PORT", "8080"))
OREGON_ZIP = os.getenv("OREGON_ZIP", "97201")

screenshot_cache: dict[str, str] = {}

app = Flask(__name__, static_folder=None)

# -------------------- Routes --------------------

@app.get("/")
def root():
    # Serve your existing index.html (expects a form with only `stock`)
    return send_from_directory(".", "index.html")

@app.get("/screenshot/<sid>")
def serve_shot(sid: str):
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

        price_path, pay_path, url = do_capture(stock)

        utc_now = datetime.datetime.utcnow().strftime("%Y-%m-%d %H:%M:%S UTC")
        hdate = https_date()

        price_ok = bool(price_path and os.path.exists(price_path))
        pay_ok   = bool(pay_path   and os.path.exists(pay_path))

        sha_price = sha256_file(price_path) if price_ok else "N/A"
        sha_pay   = sha256_file(pay_path)   if pay_ok   else "N/A"

        pid = f"price_{stock}_{int(datetime.datetime.utcnow().timestamp())}"
        mid = f"payment_{stock}_{int(datetime.datetime.utcnow().timestamp())}"
        if price_ok: screenshot_cache[pid] = price_path
        if pay_ok:   screenshot_cache[mid] = pay_path

        html = render_template_string("""
<!doctype html>
<html>
<head>
  <meta charset="utf-8"/>
  <title>Camping World Compliance Capture</title>
  <style>
    body{font-family:Inter,Arial,sans-serif;background:#f3f4f6;margin:0;padding:24px;color:#111}
    h1{margin:0 0 16px}
    .meta{background:#fff;border:1px solid #e5e7eb;border-radius:10px;padding:14px 16px;margin-bottom:18px}
    .grid{display:grid;grid-template-columns:1fr 1fr;gap:18px}
    .card{background:#fff;border:1px solid #e5e7eb;border-radius:12px;padding:16px;min-height:420px}
    .title{font-size:20px;font-weight:700;margin:0 0 12px}
    .ok{color:#059669;font-weight:700}
    .bad{color:#dc2626;font-weight:700}
    img{width:100%;height:auto;border:1px solid #e5e7eb;border-radius:8px}
    code{font-family:ui-monospace, SFMono-Regular, Menlo, Monaco, Consolas, monospace}
    a{color:#2563eb;text-decoration:none} a:hover{text-decoration:underline}
  </style>
</head>
<body>
  <h1>Camping World Compliance Capture</h1>
  <div class="meta">
    <div><strong>Stock:</strong> {{stock}}</div>
    <div><strong>URL:</strong> <a href="{{url}}" target="_blank" rel="noopener">{{url}}</a></div>
    <div><strong>UTC:</strong> {{utc}}</div>
    <div><strong>HTTPS Date:</strong> {{hdate or 'unavailable'}}</div>
  </div>
  <div class="grid">
    <div class="card">
      <div class="title">Price Tooltip — Full Page</div>
      {% if price_ok %}
        <img src="/screenshot/{{pid}}" alt="Price Tooltip"/>
        <p class="ok">✔ Captured</p>
        <div><strong>SHA-256:</strong> <code>{{sha_price}}</code></div>
      {% else %}
        <p class="bad">✗ Failed</p>
        <div><strong>SHA-256:</strong> N/A</div>
      {% endif %}
    </div>
    <div class="card">
      <div class="title">Payment Tooltip — Full Page</div>
      {% if pay_ok %}
        <img src="/screenshot/{{mid}}" alt="Payment Tooltip"/>
        <p class="ok">✔ Captured</p>
        <div><strong>SHA-256:</strong> <code>{{sha_pay}}</code></div>
      {% else %}
        <p class="bad">✗ Could not capture payment tooltip.</p>
        <div><strong>SHA-256:</strong> N/A</div>
      {% endif %}
    </div>
  </div>
</body>
</html>
        """, stock=stock, url=url, utc=utc_now, hdate=hdate,
           price_ok=price_ok, pay_ok=pay_ok, pid=pid, mid=mid,
           sha_price=sha_price, sha_pay=sha_pay)
        return Response(html, mimetype="text/html")
    except Exception as e:
        print("❌ /capture failed:", e, file=sys.stderr)
        traceback.print_exc()
        return Response(f"Error: {e}", status=500)

# -------------------- Helpers --------------------

def sha256_file(path: str) -> str:
    if not path or not os.path.exists(path): return "N/A"
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(1024*1024), b""): h.update(chunk)
    return h.hexdigest()

def https_date() -> str | None:
    try:
        r = requests.head("https://cloudflare.com", timeout=8)
        return r.headers.get("Date")
    except Exception:
        return None

def _click_or_hover_icon(page, label_text: str):
    """
    Click the info icon near a visible label (e.g., 'Est. Payment', 'Total Price').
    Falls back to hovering the label if the icon isn't clickable.
    Returns the chosen trigger element.
    """
    # Anchor by visible label text
    label = page.locator(f"xpath=//*[normalize-space(.)='{label_text}']").first
    label.wait_for(state="visible", timeout=8000)
    label.scroll_into_view_if_needed(timeout=5000)

    # Prefer the immediate following SVG (MUI info icon)
    icon = label.locator("xpath=following::*[name()='svg' and contains(@class,'MuiSvgIcon-root')][1]")
    trigger = icon.first if icon.count() > 0 else label

    try:
        if icon.count() > 0:
            trigger.click(timeout=4000)
        else:
            trigger.hover(timeout=4000, force=True)
    except Exception:
        # Fallback: synthetic hover
        page.evaluate("""
          (el)=>{
            const evts=['pointerover','mouseover','mouseenter','mousemove','focus','pointerenter'];
            for(const t of evts) el.dispatchEvent(new MouseEvent(t,{bubbles:true,cancelable:true,view:window}));
          }
        """, trigger)
    return trigger

def do_capture(stock: str) -> tuple[str | None, str | None, str]:
    url = f"https://rv.campingworld.com/rv/{stock}"
    tmpdir = tempfile.mkdtemp(prefix=f"cw-{stock}-")
    price_png = os.path.join(tmpdir, f"cw_{stock}_price.png")
    pay_png   = os.path.join(tmpdir, f"cw_{stock}_payment.png")

    print(f"goto domcontentloaded: {url}")
    with sync_playwright() as p:
        browser = p.chromium.launch(
            headless=True,
            args=["--no-sandbox", "--disable-setuid-sandbox", "--disable-dev-shm-usage"],
        )
        page = browser.new_page(
            viewport={"width":1920,"height":1080},
            user_agent=("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                        "(KHTML, like Gecko) Chrome Safari"),
            locale="en-US",
        )
        page.goto(url, wait_until="domcontentloaded", timeout=45_000)
        try:
            page.wait_for_load_state("networkidle", timeout=30_000)
            print("networkidle reached")
        except Exception:
            print("networkidle timeout (ignored)")

        # Force Oregon ZIP (for finance copy)
        try:
            page.evaluate("""(zip)=>{
                localStorage.setItem('cw_zip', zip);
                document.cookie = 'cw_zip='+zip+';path=/;SameSite=Lax';
            }""", OREGON_ZIP)
            print(f"ZIP injected {OREGON_ZIP}")
            try:
                page.reload(wait_until="networkidle", timeout=20_000)
                print("post-zip networkidle reached")
            except Exception as e:
                print("ZIP injection/reload issue:", e)
        except Exception as e:
            print("ZIP set failed:", e)

        # Hide chat/overlays that intercept pointer events
        page.add_style_tag(content="""
            [id*="intercom"], [class*="livechat"], [class*="chat"], .cf-overlay,
            .cf-powered-by, .cf-cta, .MuiBackdrop-root, [role="dialog"] {
                display: none !important; visibility: hidden !important; opacity: 0 !important;
                pointer-events: none !important;
            }
        """)
        print("overlay-hiding CSS injected")

        # ----- PRICE (click icon near 'Total Price') -----
        try:
            _click_or_hover_icon(page, "Total Price")
            page.wait_for_selector(
                "[role='tooltip'], .MuiTooltip-popper, .base-Popper-root",
                state="visible", timeout=8000
            )
            page.wait_for_timeout(400)
            page.screenshot(path=price_png, full_page=True)
            print(f"price screenshot saved {price_png} size={os.path.getsize(price_png)}")
        except Exception as e:
            print("ERROR price:", e)
            price_png = None

        # ----- PAYMENT (click icon near 'Est. Payment') -----
        try:
            _click_or_hover_icon(page, "Est. Payment")
            page.wait_for_selector(
                "[role='tooltip'], .MuiTooltip-popper, .MuiTooltip-popperInteractive, .base-Popper-root",
                state="visible", timeout=8000
            )
            page.wait_for_timeout(400)
            page.screenshot(path=pay_png, full_page=True)
            print(f"payment screenshot saved {pay_png} size={os.path.getsize(pay_png)}")
        except Exception as e:
            print("ERROR payment:", e)
            pay_png = None

        browser.close()

    return price_png, pay_png, url

# -------------------- Entrypoint --------------------

if __name__ == "__main__":
    app.run(host="0.0.0.0", port=PORT)
