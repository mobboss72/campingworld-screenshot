import os, io, zipfile, hashlib, datetime, requests, tempfile, sys, traceback
from flask import Flask, request, send_file, Response
from playwright.sync_api import sync_playwright

# Serve static files from the root directory (index.html goes in the same directory as this script)
app = Flask(__name__, static_folder=".", static_url_path="")

PORT = int(os.getenv("PORT", "8080"))
OREGON_ZIP = os.getenv("OREGON_ZIP", "97201")

@app.get("/")
def root():
    # serves ./index.html directly from root directory
    return app.send_static_file("index.html")

def https_date():
    try:
        r = requests.head("https://cloudflare.com", timeout=8)
        return r.headers.get("Date")
    except Exception:
        return None

def do_capture(stock: str):
    url = f"https://rv.campingworld.com/rv/{stock}"
    tmpdir = tempfile.mkdtemp(prefix=f"cw-{stock}-")
    png_path = os.path.join(tmpdir, f"cw_{stock}.png")

    with sync_playwright() as p:
        browser = p.chromium.launch(
            headless=True,
            args=["--no-sandbox", "--disable-setuid-sandbox"]
        )
        ctx = browser.new_context(
            locale="en-US",
            geolocation={"latitude": 45.5122, "longitude": -122.6587},
            permissions=["geolocation"],
            viewport={"width": 1920, "height": 1080},
            user_agent="Mozilla/5.0 Chrome"
        )
        page = ctx.new_page()

        page.goto(url, wait_until="domcontentloaded")
        try:
            page.wait_for_load_state("networkidle", timeout=30000)
        except Exception:
            pass

        try:
            page.evaluate(
                "(zip)=>{try{localStorage.setItem('cw_zip',zip);}catch(e){};document.cookie='cw_zip='+zip+';path=/;SameSite=Lax';}",
                OREGON_ZIP
            )
            page.reload(wait_until="domcontentloaded")
            try:
                page.wait_for_load_state("networkidle", timeout=20000)
            except Exception:
                pass
        except Exception:
            pass

        page.screenshot(path=png_path, full_page=False)
        browser.close()

    return png_path, url

@app.post("/capture")
def capture():
    try:
        stock = (request.form.get("stock") or "").strip()
        if not stock.isdigit():
            return Response("Invalid stock number", status=400)

        png_path, url = do_capture(stock)

        utc_now = datetime.datetime.utcnow().strftime("%Y-%m-%d %H:%M:%S UTC")
        hdate = https_date()
        sha = hashlib.sha256(open(png_path, "rb").read()).hexdigest()

        mem = io.BytesIO()
        with zipfile.ZipFile(mem, "w", zipfile.ZIP_DEFLATED) as z:
            z.write(png_path, arcname=f"cw_{stock}.png")
            manifest = "\n".join([
                "Camping World Proof (root server demo)",
                f"Stock: {stock}",
                f"URL: {url}",
                f"UTC: {utc_now}",
                f"HTTPS Date: {hdate or 'unavailable'}",
                f"SHA-256: {sha}",
                ""
            ])
            z.writestr("manifest.txt", manifest)
        mem.seek(0)

        return send_file(mem, mimetype="application/zip",
                         as_attachment=True,
                         download_name=f"cw_{stock}_bundle.zip")

    except Exception as e:
        print("❌ /capture failed:", e, file=sys.stderr)
        traceback.print_exc()
        return Response(f"Error: {e}", status=500)

if __name__ == "__main__":
    app.run(host="0.0.0.0", port=PORT)
