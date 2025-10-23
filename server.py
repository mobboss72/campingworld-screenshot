from flask import Flask, request, send_file, render_template_string
import os, shutil, hashlib
from playwright.sync_api import sync_playwright
import datetime
from zipfile import ZipFile

app = Flask(__name__)

@app.route('/')
def index():
    return open("index.html").read()

@app.route('/capture', methods=['POST'])
def capture():
    stock = request.form['stock']
    url = f"https://rv.campingworld.com/rv/{stock}"
    out_dir = f"captures/{stock}"
    os.makedirs(out_dir, exist_ok=True)
    screenshot_path = f"{out_dir}/screenshot.png"
    ts = datetime.datetime.utcnow().strftime("%Y-%m-%d %H:%M:%S UTC")

    with sync_playwright() as p:
        browser = p.chromium.launch()
        page = browser.new_page()
        page.goto(url)
        page.hover('text="$"')  # generic hover for tooltip
        page.wait_for_timeout(1500)  # wait for tooltip
        page.screenshot(path=screenshot_path, full_page=True)
        browser.close()

    sha256 = hashlib.sha256(open(screenshot_path, "rb").read()).hexdigest()
    manifest = f"""URL: {url}
Timestamp (UTC): {ts}
SHA-256: {sha256}
"""

    with open(f"{out_dir}/manifest.txt", "w") as f:
        f.write(manifest)

    zip_path = f"{stock}_bundle.zip"
    with ZipFile(zip_path, 'w') as zipf:
        zipf.write(screenshot_path, arcname=f"screenshot.png")
        zipf.write(f"{out_dir}/manifest.txt", arcname="manifest.txt")

    return send_file(zip_path, as_attachment=True)

if __name__ == "__main__":
    app.run(host='0.0.0.0', port=8080)
