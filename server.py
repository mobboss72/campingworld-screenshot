# server.py
import os, sys, hashlib, datetime, tempfile, traceback, requests
from flask import Flask, request, send_from_directory, Response, render_template_string, send_file
from playwright.sync_api import sync_playwright

os.environ.setdefault("PLAYWRIGHT_BROWSERS_PATH", "/app/ms-playwright")
PORT = int(os.getenv("PORT", "8080"))
OREGON_ZIP = os.getenv("OREGON_ZIP", "97201")

app = Flask(__name__, static_folder=None)
screenshot_cache = {}

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
    stock = (request.form.get("stock") or "").strip()
    if not stock.isdigit():
        return Response("Invalid stock number", status=400)

    price_png, pay_png, url = do_capture(stock)
    utc_now = datetime.datetime.utcnow().strftime("%Y-%m-%d %H:%M:%S UTC")
    https_date = get_https_date()

    def sha(path): return sha256_file(path) if os.path.exists(path) else "N/A"
    pid, qid = f"price_{stock}_{int(datetime.datetime.utcnow().timestamp())}", f"payment_{stock}_{int(datetime.datetime.utcnow().timestamp())}"
    if os.path.exists(price_png): screenshot_cache[pid] = price_png
    if os.path.exists(pay_png): screenshot_cache[qid] = pay_png

    html = f"""
    <html><head><meta charset='utf-8'><title>Camping World Compliance Capture</title>
    <style>
      body{{font-family:sans-serif;background:#f4f4f4;margin:24px;}}
      .grid{{display:grid;grid-template-columns:1fr 1fr;gap:20px}}
      .card{{background:white;padding:15px;border:1px solid #ccc;border-radius:10px}}
      img{{max-width:100%;border:1px solid #ccc}}
      .ok{{color:green;font-weight:600}} .err{{color:red;font-weight:600}}
    </style></head><body>
      <h1>Camping World Compliance Capture</h1>
      <p><b>Stock:</b> {stock}<br>
      <b>URL:</b> <a href='{url}' target='_blank'>{url}</a><br>
      <b>UTC:</b> {utc_now}<br>
      <b>HTTPS Date:</b> {https_date or 'unavailable'}</p>
      <div class='grid'>
        <div class='card'>
          <h3>Price Tooltip — Full Page</h3>
          {"<img src='/screenshot/"+pid+"'><div class='ok'>✔ Captured</div>" if os.path.exists(price_png) else "<div class='err'>✗ Failed</div>"}
          <p><b>SHA-256:</b> {sha(price_png)}</p>
        </div>
        <div class='card'>
          <h3>Payment Tooltip — Full Page</h3>
          {"<img src='/screenshot/"+qid+"'><div class='ok'>✔ Captured</div>" if os.path.exists(pay_png) else "<div class='err'>✗ Could not capture payment tooltip.</div>"}
          <p><b>SHA-256:</b> {sha(pay_png)}</p>
        </div>
      </div></body></html>
    """
    return Response(html, mimetype="text/html")

# ---------- helpers ----------
def sha256_file(path):
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(8192), b""): h.update(chunk)
    return h.hexdigest()

def get_https_date():
    try:
        r = requests.head("https://cloudflare.com", timeout=8)
        return r.headers.get("Date")
    except: return None

# ---------- core ----------
def do_capture(stock):
    url = f"https://rv.campingworld.com/rv/{stock}"
    tmp = tempfile.mkdtemp(prefix=f"cw-{stock}-")
    price_png = os.path.join(tmp, f"price_{stock}.png")
    pay_png = os.path.join(tmp, f"payment_{stock}.png")

    with sync_playwright() as p:
        b = p.chromium.launch(headless=True, args=["--no-sandbox","--disable-dev-shm-usage"])
        ctx = b.new_context(viewport={"width":1920,"height":1080}, locale="en-US")
        pg = ctx.new_page()
        pg.goto(url, wait_until="domcontentloaded")
        try: pg.wait_for_load_state("networkidle", timeout=25000)
        except: pass

        # ZIP + overlay hide
        pg.evaluate("""(z)=>{localStorage.setItem('cw_zip',z);document.cookie='cw_zip='+z+';path=/';}""", OREGON_ZIP)
        pg.add_style_tag(content="""
          .convertflow-cta,.cf-overlay,[id^='cta'],[class*='chat'],.grecaptcha-badge{display:none!important;}
        """)

        # PRICE tooltip
        try:
            price_el = pg.locator(".MuiTypography-root.MuiTypography-subtitle1").first
            price_el.scroll_into_view_if_needed()
            icon = price_el.locator("xpath=following::*[contains(@class,'MuiSvgIcon-root')][1]")
            trigger = icon.first if icon.count() > 0 else price_el
            trigger.hover(force=True, timeout=8000)
            pg.wait_for_selector("[role='tooltip'].MuiTooltip-popper", state="visible", timeout=6000)
            pg.wait_for_timeout(300)
            pg.screenshot(path=price_png, full_page=True)
        except Exception as e:
            print("Price fail:", e)

        # PAYMENT tooltip
        try:
            pay_text = pg.locator("xpath=//*[contains(normalize-space(.), '/mo')]").first
            pay_text.scroll_into_view_if_needed()
            icon = pay_text.locator("xpath=following::*[name()='svg' and contains(@class,'MuiSvgIcon-root')][1]")
            trigger = icon.first if icon.count() > 0 else pay_text
            trigger.scroll_into_view_if_needed()
            # Fire multiple synthetic events to guarantee hover
            pg.evaluate("""
                (el)=>{
                  for(const t of ['pointerover','mouseover','mouseenter','mousemove','focus']){
                    el.dispatchEvent(new MouseEvent(t,{bubbles:true,cancelable:true,view:window}));
                  }
                }
            """, trigger)
            pg.wait_for_selector("[role='tooltip'].MuiTooltip-popper, .base-Popper-root.MuiTooltip-popper",
                                 state="visible", timeout=6000)
            pg.wait_for_timeout(400)
            pg.screenshot(path=pay_png, full_page=True)
        except Exception as e:
            print("Payment fail:", e)

        b.close()
    return price_png, pay_png, url

if __name__ == "__main__":
    app.run(host="0.0.0.0", port=PORT)
