# server.py
import os, sys, hashlib, datetime, tempfile, traceback, requests, time
from flask import Flask, request, send_from_directory, Response, render_template_string, send_file
from playwright.sync_api import sync_playwright, TimeoutError as PlaywrightTimeout

# Persist Playwright downloads
os.environ.setdefault("PLAYWRIGHT_BROWSERS_PATH", "/app/ms-playwright")

PORT = int(os.getenv("PORT", "8080"))
OREGON_ZIP = os.getenv("OREGON_ZIP", "97201")

screenshot_cache: dict[str, str] = {}

app = Flask(__name__, static_folder=None)

# -------------------- Routes --------------------

@app.get("/")
def root():
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

        price_path, pay_path, url, debug_info = do_capture(stock)

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
    @media(max-width:768px){.grid{grid-template-columns:1fr}}
    .card{background:#fff;border:1px solid #e5e7eb;border-radius:12px;padding:16px;min-height:420px}
    .title{font-size:20px;font-weight:700;margin:0 0 12px}
    .ok{color:#059669;font-weight:700}
    .bad{color:#dc2626;font-weight:700}
    img{width:100%;height:auto;border:1px solid #e5e7eb;border-radius:8px;margin-bottom:12px}
    code{font-family:ui-monospace, SFMono-Regular, Menlo, Monaco, Consolas, monospace;font-size:11px;word-break:break-all}
    a{color:#2563eb;text-decoration:none} a:hover{text-decoration:underline}
    .debug{background:#fef3c7;border:1px solid #fbbf24;padding:12px;border-radius:8px;margin-top:12px;font-size:13px}
    .debug pre{margin:8px 0;white-space:pre-wrap;word-wrap:break-word}
    button{background:#2563eb;color:#fff;padding:10px 16px;border:none;border-radius:6px;cursor:pointer;margin-top:12px}
    button:hover{background:#1d4ed8}
  </style>
  <script>
    function printPage(){window.print()}
  </script>
</head>
<body>
  <h1>Camping World Compliance Capture</h1>
  <div class="meta">
    <div><strong>Stock:</strong> {{stock}}</div>
    <div><strong>URL:</strong> <a href="{{url}}" target="_blank" rel="noopener">{{url}}</a></div>
    <div><strong>Capture UTC:</strong> {{utc}}</div>
    <div><strong>HTTPS Date (Cloudflare):</strong> {{hdate or 'unavailable'}}</div>
    <button onclick="printPage()">🖨️ Print/Save as PDF</button>
  </div>
  <div class="grid">
    <div class="card">
      <div class="title">Price Tooltip – Full Page</div>
      {% if price_ok %}
        <img src="/screenshot/{{pid}}" alt="Price Tooltip"/>
        <p class="ok">✓ Captured</p>
        <div><strong>SHA-256:</strong><br><code>{{sha_price}}</code></div>
      {% else %}
        <p class="bad">✗ Failed to capture</p>
        <div><strong>SHA-256:</strong> N/A</div>
      {% endif %}
    </div>
    <div class="card">
      <div class="title">Payment Tooltip – Full Page</div>
      {% if pay_ok %}
        <img src="/screenshot/{{mid}}" alt="Payment Tooltip"/>
        <p class="ok">✓ Captured</p>
        <div><strong>SHA-256:</strong><br><code>{{sha_pay}}</code></div>
      {% else %}
        <p class="bad">✗ Could not capture payment tooltip.</p>
        <div><strong>SHA-256:</strong> N/A</div>
      {% endif %}
    </div>
  </div>
  {% if debug_info %}
  <div class="debug">
    <strong>Debug Information:</strong>
    <pre>{{debug_info}}</pre>
  </div>
  {% endif %}
</body>
</html>
        """, stock=stock, url=url, utc=utc_now, hdate=hdate,
           price_ok=price_ok, pay_ok=pay_ok, pid=pid, mid=mid,
           sha_price=sha_price, sha_pay=sha_pay, debug_info=debug_info)
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

def find_and_trigger_tooltip(page, label_text: str, tooltip_name: str):
    """
    Enhanced tooltip triggering with multiple fallback strategies.
    Returns success boolean and debug info.
    """
    debug = []
    debug.append(f"Attempting to trigger {tooltip_name} tooltip for label: '{label_text}'")
    
    try:
        # Strategy 1: Find label and look for nearby info icon
        label_selector = f"text={label_text}"
        page.wait_for_selector(label_selector, state="visible", timeout=10000)
        debug.append(f"✓ Found label: {label_text}")
        
        # Scroll to label
        label = page.locator(label_selector).first
        label.scroll_into_view_if_needed(timeout=5000)
        page.wait_for_timeout(500)
        debug.append("✓ Scrolled to label")
        
        # Strategy 1a: Try to find SVG icon next to the label
        try:
            # Look for info icon SVG near the label
            icon_selectors = [
                f"{label_selector} + svg",
                f"{label_selector} >> xpath=following-sibling::*[1][name()='svg']",
                f"{label_selector} >> xpath=..//*[name()='svg']",
            ]
            
            icon_found = False
            for selector in icon_selectors:
                try:
                    icon = page.locator(selector).first
                    if icon.count() > 0:
                        debug.append(f"✓ Found icon with selector: {selector}")
                        icon.click(timeout=3000)
                        debug.append("✓ Clicked icon")
                        icon_found = True
                        break
                except:
                    continue
            
            if not icon_found:
                debug.append("⚠ No icon found, trying hover on label")
                label.hover(timeout=3000)
                debug.append("✓ Hovered on label")
        
        except Exception as e:
            debug.append(f"⚠ Icon click failed: {e}, falling back to hover")
            label.hover(timeout=3000)
        
        # Wait for tooltip to appear
        tooltip_selectors = [
            "[role='tooltip']",
            ".MuiTooltip-popper",
            ".MuiTooltip-tooltip",
            ".base-Popper-root",
        ]
        
        tooltip_visible = False
        for selector in tooltip_selectors:
            try:
                page.wait_for_selector(selector, state="visible", timeout=5000)
                debug.append(f"✓ Tooltip visible with selector: {selector}")
                tooltip_visible = True
                break
            except:
                continue
        
        if not tooltip_visible:
            debug.append("⚠ Tooltip not detected, but continuing anyway")
        
        # Extra wait for animation
        page.wait_for_timeout(800)
        debug.append("✓ Waited for tooltip animation")
        
        return True, "\n".join(debug)
        
    except Exception as e:
        debug.append(f"❌ Error: {str(e)}")
        return False, "\n".join(debug)

def do_capture(stock: str) -> tuple[str | None, str | None, str, str]:
    url = f"https://rv.campingworld.com/rv/{stock}"
    tmpdir = tempfile.mkdtemp(prefix=f"cw-{stock}-")
    price_png = os.path.join(tmpdir, f"cw_{stock}_price.png")
    pay_png   = os.path.join(tmpdir, f"cw_{stock}_payment.png")
    
    all_debug = []
    all_debug.append(f"Starting capture for stock: {stock}")
    all_debug.append(f"URL: {url}")

    print(f"🚀 Starting capture: {url}")
    
    try:
        with sync_playwright() as p:
            browser = p.chromium.launch(
                headless=True,
                args=[
                    "--no-sandbox",
                    "--disable-setuid-sandbox",
                    "--disable-dev-shm-usage",
                    "--disable-blink-features=AutomationControlled"
                ],
            )
            
            context = browser.new_context(
                viewport={"width": 1920, "height": 1080},
                user_agent=("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                           "(KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"),
                locale="en-US",
            )
            
            page = context.new_page()
            
            # Navigate
            all_debug.append("Navigating to page...")
            page.goto(url, wait_until="domcontentloaded", timeout=60_000)
            
            # Wait for network idle
            try:
                page.wait_for_load_state("networkidle", timeout=30_000)
                all_debug.append("✓ Network idle reached")
            except:
                all_debug.append("⚠ Network idle timeout (continuing anyway)")
            
            # Inject Oregon ZIP
            try:
                page.evaluate(f"""
                    localStorage.setItem('cw_zip', '{OREGON_ZIP}');
                    document.cookie = 'cw_zip={OREGON_ZIP};path=/;SameSite=Lax';
                """)
                all_debug.append(f"✓ Injected ZIP: {OREGON_ZIP}")
                page.reload(wait_until="load", timeout=30_000)
                page.wait_for_timeout(2000)
                all_debug.append("✓ Reloaded page with ZIP")
            except Exception as e:
                all_debug.append(f"⚠ ZIP injection issue: {e}")
            
            # Hide overlays
            page.add_style_tag(content="""
                [id*="intercom"], [class*="livechat"], [class*="chat"],
                .cf-overlay, .cf-powered-by, .cf-cta,
                .MuiBackdrop-root, [role="dialog"]:not([role="tooltip"]) {
                    display: none !important;
                    visibility: hidden !important;
                    opacity: 0 !important;
                    pointer-events: none !important;
                }
            """)
            all_debug.append("✓ Overlay-hiding CSS injected")
            
            # Wait for page content
            page.wait_for_timeout(2000)
            
            # ----- CAPTURE PRICE TOOLTIP -----
            all_debug.append("\n--- Capturing Price Tooltip ---")
            success, debug_info = find_and_trigger_tooltip(page, "Total Price", "price")
            all_debug.append(debug_info)
            
            if success:
                try:
                    page.screenshot(path=price_png, full_page=True)
                    size = os.path.getsize(price_png)
                    all_debug.append(f"✓ Price screenshot saved: {size} bytes")
                    print(f"✓ Price screenshot: {size} bytes")
                except Exception as e:
                    all_debug.append(f"❌ Price screenshot failed: {e}")
                    price_png = None
            else:
                price_png = None
            
            # Small delay between captures
            page.wait_for_timeout(1000)
            
            # ----- CAPTURE PAYMENT TOOLTIP -----
            all_debug.append("\n--- Capturing Payment Tooltip ---")
            success, debug_info = find_and_trigger_tooltip(page, "Est. Payment", "payment")
            all_debug.append(debug_info)
            
            if success:
                try:
                    page.screenshot(path=pay_png, full_page=True)
                    size = os.path.getsize(pay_png)
                    all_debug.append(f"✓ Payment screenshot saved: {size} bytes")
                    print(f"✓ Payment screenshot: {size} bytes")
                except Exception as e:
                    all_debug.append(f"❌ Payment screenshot failed: {e}")
                    pay_png = None
            else:
                pay_png = None
            
            browser.close()
            all_debug.append("\n✓ Browser closed")
    
    except Exception as e:
        all_debug.append(f"\n❌ CRITICAL ERROR: {str(e)}")
        all_debug.append(traceback.format_exc())
        print(f"❌ Critical error in do_capture: {e}")
        traceback.print_exc()
    
    debug_output = "\n".join(all_debug)
    return price_png, pay_png, url, debug_output

# -------------------- Entrypoint --------------------

if __name__ == "__main__":
    app.run(host="0.0.0.0", port=PORT, debug=False)
