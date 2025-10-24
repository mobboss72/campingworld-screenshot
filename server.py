# server.py
import os
import sys
import hashlib
import datetime
import tempfile
import traceback
import requests
from pathlib import Path
from typing import Optional, Tuple

from flask import Flask, request, send_from_directory, Response, render_template_string, send_file
from playwright.sync_api import sync_playwright, Page, Locator

# -------------------------------------------------
# Environment / constants
# -------------------------------------------------
os.environ.setdefault("PLAYWRIGHT_BROWSERS_PATH", "/app/ms-playwright")
PORT = int(os.getenv("PORT", "8080"))
OREGON_ZIP = os.getenv("OREGON_ZIP", "97201")

app = Flask(__name__, static_folder=None)
screenshot_cache: dict[str, str] = {}


# ---------------------------- Utilities --------------------------------
def https_date() -> Optional[str]:
    try:
        r = requests.head("https://cloudflare.com", timeout=8)
        return r.headers.get("Date")
    except Exception:
        return None


def sha256_file(path: str) -> str:
    if not os.path.exists(path):
        return "N/A (file not found)"
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(1024 * 1024), b""):
            h.update(chunk)
    return h.hexdigest()


# ---------- small helpers used inside Playwright page context ----------
def _inject_banner(page: Page, label: str, utc_now: str, hdate: Optional[str]) -> None:
    page.evaluate(
        """(text)=>{
            const id='cw-proof-banner';
            let el=document.getElementById(id);
            if(!el){
              el=document.createElement('div');
              el.id=id;
              Object.assign(el.style,{
                position:'fixed', top:'10px', right:'10px', zIndex: 2147483647,
                background:'rgba(0,0,0,0.75)', color:'#fff',
                padding:'8px 10px', font:'12px/1.35 -apple-system,Segoe UI,Arial',
                borderRadius:'6px', boxShadow:'0 2px 8px rgba(0,0,0,.35)',
                pointerEvents:'none', maxWidth:'420px'
              });
              document.body.appendChild(el);
            }
            el.textContent=text;
        }""",
        f"{label} • UTC {utc_now} • HTTPS Date {hdate or 'unavailable'}",
    )


def _remove_banner(page: Page) -> None:
    page.evaluate("""()=>{ const el=document.getElementById('cw-proof-banner'); if(el) el.remove(); }""")


def _tooltip_visible(page: Page) -> bool:
    t = page.locator("[role='tooltip'], .MuiTooltip-popper, [data-popper-placement], .MuiPopover-root")
    try:
        return t.first.is_visible()
    except Exception:
        return False


# ------------------------------ Flask ----------------------------------
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

        price_png, payment_png, url, tmpdir = do_capture(stock)

        utc_now = datetime.datetime.utcnow().strftime("%Y-%m-%d %H:%M:%S UTC")
        hdate = https_date()

        price_exists = os.path.exists(price_png)
        payment_exists = os.path.exists(payment_png)

        sha_price = sha256_file(price_png) if price_exists else "N/A"
        sha_payment = sha256_file(payment_png) if payment_exists else "N/A"

        tsid = str(int(datetime.datetime.utcnow().timestamp()))
        price_id = f"price_{stock}_{tsid}"
        pay_id = f"payment_{stock}_{tsid}"
        if price_exists:
            screenshot_cache[price_id] = price_png
        if payment_exists:
            screenshot_cache[pay_id] = payment_png

        html = render_template_string(
            """
<!doctype html>
<html>
<head>
<meta charset="utf-8"/>
<title>Camping World Compliance Capture</title>
<style>
 body{font-family:Arial,Helvetica,sans-serif;background:#f5f5f5;margin:20px}
 h2{text-align:center;margin:0 0 14px}
 .meta{max-width:1080px;margin:0 auto 18px;background:#fff;border:1px solid #ddd;padding:12px;border-radius:6px}
 .meta p{margin:6px 0}
 .grid{display:flex;flex-wrap:wrap;gap:16px;justify-content:center}
 .card{background:#fff;border:2px solid #333;border-radius:6px;max-width:680px;padding:12px;box-shadow:0 2px 4px rgba(0,0,0,.08)}
 .card h3{margin:0 0 8px;border-bottom:2px solid #333;padding-bottom:6px}
 .imgwrap{background:#fafafa;border:1px solid #ddd;border-radius:4px;padding:10px;min-height:220px}
 img{max-width:100%;height:auto;display:block}
 .ok{color:green;font-weight:600}
 .err{color:#b00020;font-weight:600}
 code{word-break:break-all}
</style>
</head>
<body>
  <h2>Camping World Compliance Capture</h2>
  <div class="meta">
    <p><strong>Stock:</strong> {{stock}}</p>
    <p><strong>URL:</strong> <a href="{{url}}" target="_blank" rel="noopener">{{url}}</a></p>
    <p><strong>UTC:</strong> {{utc}}</p>
    <p><strong>HTTPS Date:</strong> {{hdate or 'unavailable'}}</p>
  </div>

  <div class="grid">
    <div class="card">
      <h3>Price Tooltip — Full Page</h3>
      <div class="imgwrap">
        {% if price_exists %}
          <img src="/screenshot/{{price_id}}" alt="Price Tooltip"/>
          <p class="ok">✓ Captured</p>
        {% else %}
          <p class="err">✗ Could not capture price tooltip.</p>
        {% endif %}
      </div>
      <p><strong>SHA-256:</strong> <code>{{sha_price}}</code></p>
    </div>

    <div class="card">
      <h3>Payment Tooltip — Full Page</h3>
      <div class="imgwrap">
        {% if payment_exists %}
          <img src="/screenshot/{{pay_id}}" alt="Payment Tooltip"/>
          <p class="ok">✓ Captured</p>
        {% else %}
          <p class="err">✗ Could not capture payment tooltip.</p>
        {% endif %}
      </div>
      <p><strong>SHA-256:</strong> <code>{{sha_payment}}</code></p>
    </div>
  </div>
</body>
</html>
            """,
            stock=stock,
            url=url,
            utc=utc_now,
            hdate=hdate,
            price_exists=price_exists,
            payment_exists=payment_exists,
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


# --------------------------- Core capture ------------------------------
def do_capture(stock: str) -> Tuple[str, str, str, str]:
    """
    Returns (price_png_path, payment_png_path, url, tmpdir)
    """
    url = f"https://rv.campingworld.com/rv/{stock}"
    tmpdir = tempfile.mkdtemp(prefix=f"cw-{stock}-")
    ts = datetime.datetime.utcnow().strftime("%Y%m%d-%H%M%S")
    price_png_path = os.path.join(tmpdir, f"cw_{stock}_price_{ts}.png")
    payment_png_path = os.path.join(tmpdir, f"cw_{stock}_payment_{ts}.png")

    print(f"\n=== Capture start stock={stock} ===")
    print(f"URL: {url}\nTMP: {tmpdir}")

    utc_now = datetime.datetime.utcnow().strftime("%Y-%m-%d %H:%M:%S UTC")
    hdate = https_date()

    with sync_playwright() as p:
        browser = p.chromium.launch(
            headless=True,
            args=["--no-sandbox", "--disable-setuid-sandbox", "--disable-dev-shm-usage"],
        )
        ctx = browser.new_context(
            viewport={"width": 1920, "height": 1080},
            permissions=["geolocation"],
            geolocation={"latitude": 45.5122, "longitude": -122.6587},
            locale="en-US",
            user_agent="Mozilla/5.0 Chrome",
        )
        page = ctx.new_page()

        # Navigate and settle
        page.goto(url, wait_until="domcontentloaded")
        try:
            page.wait_for_load_state("networkidle", timeout=30000)
        except Exception as e:
            print(f"networkidle wait timed out: {e}")

        # Force Oregon ZIP (pricing rules)
        try:
            page.evaluate(
                """(zip)=>{
                    try{localStorage.setItem('cw_zip', zip);}catch(e){}
                    document.cookie='cw_zip='+zip+';path=/;SameSite=Lax';
                }""",
                OREGON_ZIP,
            )
            page.reload(wait_until="domcontentloaded")
            try:
                page.wait_for_load_state("networkidle", timeout=20000)
            except Exception:
                pass
        except Exception as e:
            print(f"ZIP inject failed: {e}")

        # ---- PRICE (your proven snippet) ----
        print("\n--- PRICE tooltip ---")
        try:
            page.wait_for_selector(".MuiTypography-root.MuiTypography-subtitle1", state="visible", timeout=15000)
        except Exception:
            pass

        price_selector = ".MuiTypography-root.MuiTypography-subtitle1:visible"
        price_elements = page.locator(price_selector)
        try:
            print(f"Number of price elements found: {price_elements.count()}")
        except Exception:
            pass

        visible_price: Optional[Locator] = None
        try:
            cnt = price_elements.count()
            for i in range(cnt):
                elem = price_elements.nth(i)
                if elem.is_visible():
                    visible_price = elem
                    print(f"Visible price element found at index {i}")
                    break
        except Exception as e:
            print(f"Iterating price elements failed: {e}")

        if visible_price:
            try:
                visible_price.scroll_into_view_if_needed(timeout=5000)
                # try icon next to price label first (helps when tooltip is on info icon)
                icon = visible_price.locator("xpath=following::*[(self::button or self::*[name()='svg'] or contains(@class,'MuiSvgIcon-root'))][1]")
                trigger = icon.first if (icon.count() and icon.first.is_visible()) else visible_price
                trigger.hover(timeout=10000, force=True)
                page.wait_for_timeout(800)
                # Fallbacks if tooltip didn’t appear
                if not _tooltip_visible(page):
                    try:
                        trigger.focus(); page.wait_for_timeout(200)
                    except Exception:
                        pass
                    if not _tooltip_visible(page):
                        try:
                            trigger.click(timeout=3000); page.wait_for_timeout(200)
                        except Exception:
                            pass
                _inject_banner(page, "Price Tooltip", utc_now, hdate)
                page.screenshot(path=price_png_path, full_page=True)
                _remove_banner(page)
                print(f"Price full page screenshot saved: {price_png_path}  (tooltip_open={_tooltip_visible(page)})")
            except Exception as e:
                print(f"Price hover failed: {e}")
        else:
            print("No visible price element found")

        # Ensure any price popover doesn’t block payment
        try:
            page.keyboard.press("Escape"); page.wait_for_timeout(150)
        except Exception:
            pass

        # ---- PAYMENT (robust trigger + wait for tooltip) ----
        print("\n--- PAYMENT tooltip ---")
        # prefer text like "payment", "/mo", "monthly" then adjacent icon
        payment_selector = ".MuiTypography-root.MuiTypography-subtitle2:visible"
        payment_elems = page.locator(payment_selector)
        try:
            count = payment_elems.count()
            print(f"Found {count} payment elements")
        except Exception:
            count = 0

        visible_payment: Optional[Locator] = None
        for i in range(count):
            try:
                el = payment_elems.nth(i)
                if el.is_visible():
                    txt = (el.text_content() or "").lower()
                    if any(k in txt for k in ("payment", "/mo", "per month", "monthly")):
                        visible_payment = el
                        print(f"Using payment element at index {i} with text: {txt}")
                        break
            except Exception:
                continue

        if visible_payment:
            try:
                visible_payment.scroll_into_view_if_needed(timeout=5000)
                trigger: Locator = visible_payment
                icon = visible_payment.locator("xpath=following::*[(self::button or self::*[name()='svg'] or contains(@class,'MuiSvgIcon-root'))][1]")
                if icon.count() and icon.first.is_visible():
                    trigger = icon.first

                # open tooltip
                opened = False
                for action in ("hover", "focus", "click", "synthetic"):
                    try:
                        if action == "hover":
                            trigger.hover(timeout=10000, force=True)
                        elif action == "focus":
                            trigger.focus()
                        elif action == "click":
                            trigger.click(timeout=4000)
                        else:
                            page.evaluate(
                                """(el)=>{
                                  ['pointerover','mouseenter','mouseover','focusin'].forEach(
                                    t=>el.dispatchEvent(new MouseEvent(t,{bubbles:true}))
                                  );
                                }""",
                                trigger,
                            )
                        page.wait_for_timeout(250)
                        if _tooltip_visible(page):
                            opened = True
                            break
                    except Exception:
                        pass

                _inject_banner(page, "Payment Tooltip", utc_now, hdate)
                page.screenshot(path=payment_png_path, full_page=True)
                _remove_banner(page)
                print(f"Payment full page screenshot saved: {payment_png_path}  (tooltip_open={opened})")
            except Exception as e:
                print(f"Payment hover failed: {e}")
        else:
            print("❌ Payment element not found")

        browser.close()
        print("=== Capture done ===")

    return price_png_path, payment_png_path, url, tmpdir


# ------------------------------ main -----------------------------------
if __name__ == "__main__":
    app.run(host="0.0.0.0", port=PORT)
