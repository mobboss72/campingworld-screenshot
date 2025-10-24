import os
import hashlib
import asyncio
from datetime import datetime, timezone
from flask import Flask, request, send_file, render_template_string
from playwright.async_api import async_playwright

app = Flask(__name__)

# HTML template for results
HTML_TEMPLATE = """
<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8">
<title>Camping World Compliance Capture</title>
<style>
body { font-family: Arial, sans-serif; background-color: #f4f4f4; color: #333; }
.container { max-width: 1200px; margin: 40px auto; background: white; padding: 20px; border-radius: 8px; box-shadow: 0 0 10px rgba(0,0,0,0.1); }
h1 { color: #111; }
h2 { color: #000; border-bottom: 2px solid #eee; padding-bottom: 5px; }
.info { margin-bottom: 20px; }
img { width: 100%; border-radius: 5px; }
.status { font-weight: bold; margin-top: 10px; }
.success { color: green; }
.fail { color: red; }
section { display: flex; gap: 20px; justify-content: space-between; }
.card { flex: 1; border: 2px solid #ccc; border-radius: 8px; padding: 10px; background: #fafafa; }
a { color: #0055cc; text-decoration: none; }
a:hover { text-decoration: underline; }
</style>
</head>
<body>
<div class="container">
    <h1>Camping World Compliance Capture</h1>
    <div class="info">
        <p><strong>Stock:</strong> {{stock}}</p>
        <p><strong>URL:</strong> <a href="{{url}}">{{url}}</a></p>
        <p><strong>UTC:</strong> {{utc}}</p>
        <p><strong>HTTPS Date:</strong> {{https_date}}</p>
    </div>
    <section>
        <div class="card">
            <h2>Price Tooltip — Full Page</h2>
            {% if price_ok %}
                <img src="{{price_img}}" alt="Price Tooltip Screenshot">
                <p class="status success">✔ Captured</p>
                <p><strong>SHA-256:</strong> {{price_hash}}</p>
            {% else %}
                <p class="status fail">✗ Failed</p>
                <p><strong>SHA-256:</strong> N/A</p>
            {% endif %}
        </div>
        <div class="card">
            <h2>Payment Tooltip — Full Page</h2>
            {% if pay_ok %}
                <img src="{{pay_img}}" alt="Payment Tooltip Screenshot">
                <p class="status success">✔ Captured</p>
                <p><strong>SHA-256:</strong> {{pay_hash}}</p>
            {% else %}
                <p class="status fail">✗ Could not capture payment tooltip.</p>
                <p><strong>SHA-256:</strong> N/A</p>
            {% endif %}
        </div>
    </section>
</div>
</body>
</html>
"""

def sha256sum(filename):
    h = hashlib.sha256()
    with open(filename, "rb") as f:
        for chunk in iter(lambda: f.read(4096), b""):
            h.update(chunk)
    return h.hexdigest()

async def capture_page(stock, url):
    folder = f"/tmp/cw-{stock.lower()}-{os.urandom(4).hex()}"
    os.makedirs(folder, exist_ok=True)
    price_png = os.path.join(folder, f"cw_{stock}_price.png")
    pay_png = os.path.join(folder, f"cw_{stock}_payment.png")
    print(f"goto domcontentloaded: {url}")

    async with async_playwright() as pw:
        browser = await pw.chromium.launch(headless=True, args=["--no-sandbox"])
        page = await browser.new_page()
        await page.goto(url, wait_until="domcontentloaded", timeout=45000)
        await page.wait_for_load_state("networkidle", timeout=30000)
        print("networkidle reached")

        # Inject ZIP code for financing calc
        await page.evaluate("localStorage.setItem('cw_zip','97201')")
        print("ZIP injected 97201")
        try:
            await page.reload(wait_until="networkidle", timeout=20000)
            print("post-zip networkidle reached")
        except:
            print("ZIP injection/reload issue: Timeout 20000ms exceeded.")

        await page.add_style_tag(content="div[role='dialog'], div[class*='overlay'], div[id*='modal'] { display:none !important; }")
        print("overlay-hiding CSS injected")

        # ----- PRICE TOOLTIP -----
        try:
            price_locator = page.locator("text=$", has_text="$")
            await page.wait_for_timeout(500)
            price_candidates = await price_locator.count()
            print(f"price candidates={price_candidates}")
            idx = price_candidates - 1
            price_text = price_locator.nth(idx)
            await price_text.scroll_into_view_if_needed()
            icon = price_text.locator("xpath=following::*[name()='svg'][1]")
            if await icon.count() > 0:
                trigger = icon.first
                print("price: using adjacent info icon")
            else:
                trigger = price_text
                print("price: using text as trigger")

            await page.evaluate("""
                (el)=>{
                    for(const t of ['pointerover','mouseover','mouseenter','mousemove','focus']){
                        el.dispatchEvent(new MouseEvent(t,{bubbles:true,cancelable:true,view:window}));
                    }
                }
            """, trigger)

            await page.wait_for_selector("[role='tooltip']", state="visible", timeout=7000)
            print("price tooltip appeared")
            await page.wait_for_timeout(400)
            await page.screenshot(path=price_png, full_page=True)
            print(f"price screenshot saved {price_png} size={os.path.getsize(price_png)} bytes")
        except Exception as e:
            print("ERROR price:", e)
            price_png = None

        # ----- PAYMENT TOOLTIP -----
        try:
            pay_text = page.locator("xpath=//*[contains(normalize-space(.), '/mo') or contains(translate(normalize-space(.),'PAYMENT','payment'))]").first
            await pay_text.scroll_into_view_if_needed(timeout=5000)

            icon = pay_text.locator("xpath=following::*[name()='svg' and contains(@class,'MuiSvgIcon-root')][1]")
            trigger = icon.first if await icon.count() > 0 else pay_text
            await trigger.scroll_into_view_if_needed()

            await page.evaluate("""
                (el)=>{
                    for(const t of ['pointerover','mouseover','mouseenter','mousemove','focus','pointerenter']){
                        el.dispatchEvent(new MouseEvent(t,{bubbles:true,cancelable:true,view:window}));
                    }
                }
            """, trigger)

            await page.wait_for_selector(
                "[role='tooltip'], .MuiTooltip-popper, .MuiTooltip-popperInteractive, .base-Popper-root",
                state="visible",
                timeout=8000
            )
            print("payment tooltip appeared")
            await page.wait_for_timeout(400)
            await page.screenshot(path=pay_png, full_page=True)
            print(f"payment screenshot saved {pay_png} size={os.path.getsize(pay_png)} bytes")
        except Exception as e:
            print("ERROR payment:", e)
            pay_png = None

        await browser.close()

    utc_now = datetime.now(timezone.utc)
    https_date = utc_now.strftime("%a, %d %b %Y %H:%M:%S GMT")

    return {
        "stock": stock,
        "url": url,
        "utc": utc_now.strftime("%Y-%m-%d %H:%M:%S UTC"),
        "https_date": https_date,
        "price_img": price_png,
        "pay_img": pay_png
    }

@app.route("/", methods=["GET"])
def home():
    return """
    <form method="POST" action="/capture">
        <label>Stock #:</label><input name="stock"><br>
        <label>URL:</label><input name="url" size="60"><br>
        <button type="submit">Capture</button>
    </form>
    """

@app.route("/capture", methods=["POST"])
def capture():
    stock = request.form["stock"].strip()
    url = request.form["url"].strip()
    result = asyncio.run(capture_page(stock, url))

    price_ok = result["price_img"] and os.path.exists(result["price_img"])
    pay_ok = result["pay_img"] and os.path.exists(result["pay_img"])
    price_hash = sha256sum(result["price_img"]) if price_ok else None
    pay_hash = sha256sum(result["pay_img"]) if pay_ok else None

    html = render_template_string(
        HTML_TEMPLATE,
        stock=stock,
        url=url,
        utc=result["utc"],
        https_date=result["https_date"],
        price_ok=price_ok,
        pay_ok=pay_ok,
        price_img=result["price_img"],
        pay_img=result["pay_img"],
        price_hash=price_hash,
        pay_hash=pay_hash
    )
    html_path = f"/tmp/{stock}_capture.html"
    with open(html_path, "w") as f:
        f.write(html)
    return send_file(html_path, mimetype="text/html")

if __name__ == "__main__":
    app.run(host="0.0.0.0", port=8080)
