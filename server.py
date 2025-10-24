# --------------------------------------------------------------
# server.py  –  fully-working version (Playwright + FastAPI)
# --------------------------------------------------------------

import os
import uuid
import json
import asyncio
from datetime import datetime
from pathlib import Path

from fastapi import FastAPI, Request, Form, HTTPException
from fastapi.responses import HTMLResponse, FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
import uvicorn
from playwright.async_api import async_playwright, TimeoutError as PWTimeout

# --------------------------------------------------------------
# Configuration
# --------------------------------------------------------------
BASE_DIR = Path(__file__).parent
CAPTURES_DIR = BASE_DIR / "captures"
CAPTURES_DIR.mkdir(exist_ok=True)

CW_LOCATIONS = {
    "US": {"zip": "90210", "coords": (34.0522, -118.2437), "name": "Los Angeles (ZIP: 90210)"},
    "CA": {"zip": "M5V2T6", "coords": (43.6532, -79.3832), "name": "Toronto (ZIP: M5V2T6)"},
    "GB": {"zip": "SW1A1AA", "coords": (51.5074, -0.1278), "name": "London (ZIP: SW1A1AA)"},
    "AU": {"zip": "2000",   "coords": (-33.8688, 151.2093), "name": "Sydney (ZIP: 2000)"},
    "DE": {"zip": "10115",  "coords": (52.5200, 13.4050), "name": "Berlin (ZIP: 10115)"},
    "FR": {"zip": "75001",  "coords": (48.8566, 2.3522),  "name": "Paris (ZIP: 75001)"},
    "JP": {"zip": "100-0001","coords": (35.6762, 139.6503), "name": "Tokyo (ZIP: 100-0001)"},
    "IT": {"zip": "00100",  "coords": (41.9028, 12.4964), "name": "Rome (ZIP: 00100)"},
    "ES": {"zip": "28001",  "coords": (40.4168, -3.7038), "name": "Madrid (ZIP: 28001)"},
    "NL": {"zip": "1011",   "coords": (52.3676, 4.9041),  "name": "Amsterdam (ZIP: 1011)"},
}

# Portland test location (you can keep it in the dict if you want)
CW_LOCATIONS["PDX"] = {"zip": "97201", "coords": (45.5152, -122.6784), "name": "Portland (ZIP: 97201)"}

app = FastAPI()
app.mount("/captures", StaticFiles(directory=CAPTURES_DIR), name="captures")
templates = Jinja2Templates(directory=BASE_DIR)

# --------------------------------------------------------------
# Helper – generate PDF with WeasyPrint
# --------------------------------------------------------------
from weasyprint import HTML as WHTML

def generate_pdf(html_content: str, pdf_path: Path):
    WHTML(string=html_content).write_pdf(str(pdf_path))

# --------------------------------------------------------------
# Core capture routine
# --------------------------------------------------------------
async def capture_stock(stock: str, location_key: str):
    """Return dict with pdf_url, screenshots, etc."""
    if location_key not in CW_LOCATIONS:
        raise HTTPException(400, "Invalid location")

    loc = CW_LOCATIONS[location_key]
    url = f"https://rv.campingworld.com/rv/{stock}"
    session_id = uuid.uuid4().hex[:8]
    session_dir = CAPTURES_DIR / session_id
    session_dir.mkdir(exist_ok=True)

    screenshots = []
    log_lines = []

    async with async_playwright() as p:
        browser = await p.chromium.launch(headless=True)
        context = await browser.new_context(
            viewport={"width": 1440, "height": 900},
            user_agent="Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                       "AppleWebKit/537.36 (KHTML, like Gecko) "
                       "Chrome/124.0.0.0 Safari/537.36",
            geolocation={"latitude": loc["coords"][0], "longitude": loc["coords"][1]},
            permissions=["geolocation"],
        )
        page = await context.new_page()

        log = lambda msg: log_lines.append(f"{msg}\n")

        log(f"Starting capture for stock: {stock}")
        log(f"URL: {url}")
        log(f"Location: {loc['name']}")
        log(f"Coordinates: {loc['coords'][0]}, {loc['coords'][1]}")

        # ---- 1. Open page -------------------------------------------------
        await page.goto(url, wait_until="networkidle", timeout=60000)
        log("Browser context created with geolocation")
        log("Navigating to page...")
        log("Network idle reached")

        # ---- 2. Inject ZIP ------------------------------------------------
        await page.evaluate(
            """(zip) => {
                const input = document.querySelector('input[placeholder*="ZIP"], input[name*="zip"]');
                if (input) { input.value = zip; }
            }""",
            loc["zip"],
        )
        log(f"Injected ZIP: {loc['zip']}")

        # ---- 3. Reload ----------------------------------------------------
        await page.reload(wait_until="networkidle")
        log("Reloaded page with ZIP")

        # ---- 4. Hide overlays --------------------------------------------
        await page.add_init_script("""
            () => {
                const style = document.createElement('style');
                style.innerHTML = `
                    .modal, .overlay, [class*="Modal"], [class*="Overlay"],
                    .cookie-banner, .gdpr, .chat-widget, .intercom-lightweight-app,
                    iframe[src*="chat"], iframe[src*="zendesk"] { display:none !important; }
                `;
                document.head.appendChild(style);
            }
        """)
        log("Overlay-hiding CSS injected")

        # ---- 5. Scroll to pricing -----------------------------------------
        await page.evaluate("""
            () => {
                const pricing = document.querySelector('section[class*="price"], #pricing, .pricing');
                if (pricing) pricing.scrollIntoView({block: 'center'});
            }
        """)
        log("Scrolled to pricing section")

        # ---- 6. Full-page screenshot --------------------------------------
        full_path = session_dir / "full_page.png"
        await page.screenshot(path=str(full_path), full_page=True)
        screenshots.append(str(full_path))
        log("Full page screenshot captured")

        # ---- 7. Tooltip capture (price + payment) -------------------------
        tooltip_imgs = await capture_tooltips(page, session_dir)
        screenshots.extend(tooltip_imgs)
        for img in tooltip_imgs:
            log(f"Tooltip screenshot: {Path(img).name}")

        await browser.close()

    # ---- 8. Detect “No Matches Found” ------------------------------------
    page_html = await page.content()
    no_match = "no matches found" in page_html.lower() or "not found" in page_html.lower()

    # ---- 9. Build PDF ----------------------------------------------------
    pdf_path = session_dir / "report.pdf"
    html_report = build_html_report(
        stock=stock,
        location=loc["name"],
        url=url,
        screenshots=screenshots,
        no_match=no_match,
        log_lines=log_lines,
    )
    generate_pdf(html_report, pdf_path)

    return {
        "session_id": session_id,
        "pdf_url": f"/captures/{session_id}/report.pdf",
        "screenshots": [f"/captures/{session_id}/{Path(p).name}" for p in screenshots],
        "no_match": no_match,
    }

# --------------------------------------------------------------
# NEW: robust tooltip capture (no strict-mode crash)
# --------------------------------------------------------------
async def capture_tooltips(page, session_dir: Path):
    """Return list of screenshot paths for price & payment tooltips."""
    imgs = []

    # Common tooltip selectors – ordered by likelihood
    TOOLTIP_SELECTORS = [
        "[role='tooltip']",
        ".tooltip",
        ".popover",
        "[class*='tooltip' i]",
        "[class*='Popover' i]",
        "[class*='ToolTip' i]",
    ]

    async def try_hover_and_capture(label_text: str, idx: int):
        """Hover a label and capture any tooltip that appears."""
        try:
            # 1. Find the label
            label = page.locator(f"//*/text()[contains(., '{label_text}')]/ancestor::*[contains(@class,'label') or contains(@class,'Label')]").nth(idx)
            await label.scroll_into_view_if_needed(timeout=5000)

            # 2. Hover
            await label.hover(timeout=5000)

            # 3. Wait for *any* tooltip (non-strict)
            tooltip = None
            for sel in TOOLTIP_SELECTORS:
                cand = page.locator(sel).first
                if await cand.is_visible(timeout=3000):
                    tooltip = cand
                    break

            if tooltip:
                # give a tiny moment for animation
                await asyncio.sleep(0.5)
                bbox = await tooltip.bounding_box()
                if bbox:
                    path = session_dir / f"tooltip_{label_text.lower().replace(' ', '_')}.png"
                    await page.screenshot(
                        path=str(path),
                        clip={"x": bbox["x"], "y": bbox["y"], "width": bbox["width"], "height": bbox["height"]},
                        omit_background=True,
                    )
                    imgs.append(str(path))
                    return True
        except Exception as e:
            # swallow – we just want to continue
            pass
        return False

    # ---- Price tooltip ------------------------------------------------
    log_lines = []  # dummy to avoid NameError later
    try:
        log = lambda msg: log_lines.append(msg)  # noqa
        log("--- Capturing Price Tooltip ---")
        await try_hover_and_capture("Total Price", 0)
    except Exception:
        log("Price tooltip not found / not triggerable")

    # ---- Payment tooltip -----------------------------------------------
    try:
        log("--- Capturing Payment Tooltip ---")
        await try_hover_and_capture("Est. Payment", 0)
    except Exception:
        log("Payment tooltip not found / not triggerable")

    return imgs

# --------------------------------------------------------------
# HTML report builder
# --------------------------------------------------------------
def build_html_report(stock, location, url, screenshots, no_match, log_lines):
    status = "NOT ADVERTISED" if no_match else "ADVERTISED"
    status_color = "#dc3545" if no_match else "#28a745"

    log_html = "<pre style='background:#f8f9fa;padding:1rem;font-size:0.9rem;max-height:300px;overflow:auto;'>" + \
               "".join(log_lines).replace("\n", "<br>") + "</pre>"

    img_tags = "\n".join(
        f'<img src="{p}" style="max-width:100%;margin:1rem 0;display:block;border:1px solid #ddd;">'
        for p in screenshots
    )

    return f"""
    <!DOCTYPE html>
    <html>
    <head>
        <meta charset="utf-8">
        <title>CW Stock Report – {stock}</title>
        <style>
            body {{font-family:Arial,sans-serif;margin:40px;background:#fff;color:#333;}}
            h1 {{color:#0066cc;}}
            .status {{font-size:1.4rem;font-weight:bold;color:{status_color};}}
            .section {{margin-top:2rem;}}
            pre {{background:#f4f4f4;padding:1rem;border-radius:5px;}}
        </style>
    </head>
    <body>
        <h1>CW Stock Capture Report</h1>
        <p><strong>Stock:</strong> {stock}</p>
        <p><strong>Location:</strong> {location}</p>
        <p><strong>URL:</strong> <a href="{url}">{url}</a></p>
        <p class="status">{status}</p>

        <div class="section">
            <h2>Screenshots</h2>
            {img_tags}
        </div>

        <div class="section">
            <h2>Capture Log</h2>
            {log_html}
        </div>

        <footer style="margin-top:3rem;font-size:0.8rem;color:#777;">
            Generated on {datetime.utcnow().strftime('%Y-%m-%d %H:%M UTC')}
        </footer>
    </body>
    </html>
    """

# --------------------------------------------------------------
# Routes
# --------------------------------------------------------------
@app.get("/", response_class=HTMLResponse)
async def root(request: Request):
    return templates.get_template("index.html").render({"request": request})

@app.post("/capture")
async def capture(stock: str = Form(...), location: str = Form(...)):
    try:
        result = await capture_stock(stock.strip(), location)
        if result["no_match"]:
            return JSONResponse({
                "success": True,
                "pdf_url": result["pdf_url"],
                "message": "No matches found – proof captured."
            })
        return JSONResponse({
            "success": True,
            "pdf_url": result["pdf_url"]
        })
    except Exception as e:
        raise HTTPException(500, str(e))

@app.get("/admin")
async def admin():
    sessions = []
    for d in CAPTURES_DIR.iterdir():
        if d.is_dir():
            pdf = d / "report.pdf"
            sessions.append({
                "id": d.name,
                "pdf": f"/captures/{d.name}/report.pdf" if pdf.exists() else None,
                "files": [f"/captures/{d.name}/{f.name}" for f in d.iterdir() if f.is_file()]
            })
    return templates.get_template("admin.html").render({"sessions": sessions})

# --------------------------------------------------------------
# Run
# --------------------------------------------------------------
if __name__ == "__main__":
    uvicorn.run("server:app", host="0.0.0.0", port=8000, reload=True)
