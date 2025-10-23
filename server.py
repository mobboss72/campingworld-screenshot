# server.py
import os
import io
import sys
import zipfile
import hashlib
import datetime
import tempfile
import traceback
import requests

from flask import Flask, request, send_file, Response, send_from_directory
from playwright.sync_api import sync_playwright

# --- Important: install path for Playwright browsers in Railway (writable) ---
os.environ.setdefault("PLAYWRIGHT_BROWSERS_PATH", "/app/ms-playwright")

PORT = int(os.getenv("PORT", "8080"))
OREGON_ZIP = os.getenv("OREGON_ZIP", "97201")

# We serve index.html from the REPO ROOT (not /public). Keep index.html at repo root.
app = Flask(__name__, static_folder=None)


# ---- Routes -----------------------------------------------------------------
@app.get("/")
def root():
    # Serve the GUI from the repo root.
    return send_from_directory(".", "index.html")


@app.post("/capture")
def capture():
    try:
        stock = (request.form.get("stock") or "").strip()
        if not stock.isdigit():
            return Response("Invalid stock number", status=400)

        png_path, url = do_capture(stock)

        utc_now = datetime.datetime.utcnow().strftime("%Y-%m-%d %H:%M:%S UTC")
        hdate = https_date()
        sha = sha256_file(png_path)

        # Build a ZIP in-memory (PNG + manifest)
        mem = io.BytesIO()
        with zipfile.ZipFile(mem, "w", zipfile.ZIP_DEFLATED) as z:
            z.write(png_path, arcname=f"cw_{stock}.png")
            manifest = "\n".join([
                "Camping World Proof (minimal server demo)",
                f"Stock: {stock}",
                f"URL: {url}",
                f"UTC: {utc_now}",
                f"HTTPS Date: {hdate or 'unavailable'}",
                f"SHA-256: {sha}",
                ""
            ])
            z.writestr("manifest.txt", manifest)
        mem.seek(0)

        return send_file(
            mem,
            mimetype="application/zip",
            as_attachment=True,
            download_name=f"cw_{stock}_bundle.zip"
        )

    except Exception as e:
        # Log full traceback to Railway logs for quick debugging
        print("❌ /capture failed:", e, file=sys.stderr)
        traceback.print_exc()
        return Response(f"Error: {e}", status=500)


# ---- Helpers ----------------------------------------------------------------
def sha256_file(path: str) -> str:
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


def do_capture(stock: str) -> tuple[str, str]:
    """
    Minimal capture to prove runtime works on Railway.
    (Once stable, we can drop in the tooltip logic & PDF/TSA packaging.)
    """
    url = f"https://rv.campingworld.com/rv/{stock}"
    tmpdir = tempfile.mkdtemp(prefix=f"cw-{stock}-")
    png_path = os.path.join(tmpdir, f"cw_{stock}.png")

    with sync_playwright() as p:
        # Launch Chromium with flags required in containerized environments
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
            geolocation={"latitude": 45.5122, "longitude": -122.6587},  # Oregon-ish
            permissions=["geolocation"],
            viewport={"width": 1920, "height": 1080},
            user_agent="Mozilla/5.0 Chrome",
        )
        page = ctx.new_page()

        # Load unit page
        page.goto(url, wait_until="domcontentloaded")
        try:
            page.wait_for_load_state("networkidle", timeout=30_000)
        except Exception:
            pass

        # Force Oregon ZIP in storage/cookie, then reload so tooltips/pricing respect location
        try:
            page.evaluate(
                "(zip)=>{try{localStorage.setItem('cw_zip',zip);}catch(e){};document.cookie='cw_zip='+zip+';path=/;SameSite=Lax';}",
                OREGON_ZIP,
            )
            page.reload(wait_until="domcontentloaded")
            try:
                page.wait_for_load_state("networkidle", timeout=20_000)
            except Exception:
                pass
        except Exception:
            pass

        # Simple page screenshot (we’ll add tooltip hover + side-by-side once base deploy is stable)
        page.screenshot(path=png_path, full_page=False)
        browser.close()

    return png_path, url


# ---- Entry point -------------------------------------------------------------
if __name__ == "__main__":
    app.run(host="0.0.0.0", port=PORT)
