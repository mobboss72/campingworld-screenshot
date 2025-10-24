# server.py
import os
import sys
import re
import hashlib
import datetime
import tempfile
import traceback
import json
from pathlib import Path

import requests
from flask import Flask, request, send_from_directory, Response, render_template_string, send_file
from playwright.sync_api import sync_playwright, TimeoutError as PWTimeout

# -------- App / env --------
APP_VERSION = "2025-10-24T03:20Z  • dual-tooltip stable • icon-trigger for payment • separate-page captures"
os.environ.setdefault("PLAYWRIGHT_BROWSERS_PATH", "/app/ms-playwright")

PORT = int(os.getenv("PORT", "8080"))
OREGON_ZIP = os.getenv("OREGON_ZIP", "97201")

# In-memory map of screenshot ids -> file paths
screenshot_cache: dict[str, str] = {}

app = Flask(__name__, static_folder=None)


# -------- Routes --------
@app.get("/")
def root():
    return send_from_directory(".", "index.html")


@app.get("/screenshot/<screenshot_id>")
def serve_screenshot(screenshot_id):
    p = screenshot_cache.get(screenshot_id)
    if not p or not os.path.exists(p):
        return Response("Screenshot not found", status=404)
    return send_file(p, mimetype="image/png")


@app.post("/capture")
def capture():
    try:
        stock = (request.form.get("stock") or "").strip()
        if not stock.isdigit():
            return Response("Invalid stock number", status=400)

        price_path, payment_path, url, diag = do_capture_both(stock)

        utc_now = datetime.datetime.utcnow().strftime("%Y-%m-%d %H:%M:%S UTC")
        hdate = https_date()

        price_exists = os.path.exists(price_path)
        payment_exists = os.path.exists(payment_path)

        sha_price = sha256_file(price_path) if price_exists else "N/A"
        sha_payment = sha256_file(payment_path) if payment_exists else "N/A"

        # expose via cache
        ts = int(datetime.datetime.utcnow().timestamp())
        pid = f"price_{stock}_{ts}"
        qid = f"payment_{stock}_{ts}"
        if price_exists:
            screenshot_cache[pid] = price_path
        if payment_exists:
            screenshot_cache[qid] = payment_path

        html = render_template_string(
            HTML_TEMPLATE,
            version=APP_VERSION,
            stock=stock,
            url=url,
            utc_now=utc_now,
            hdate=hdate,
            price_exists=price_exists,
            payment_exists=payment_exists,
            price_id=pid,
            pay_id=qid,
            sha_price=sha_price,
            sha_payment=sha_payment,
            diag=diag,
        )
        return Response(html, mimetype="text/html")

    except Exception as e:
        print("❌ /capture failed:", e, file=sys.stderr)
        traceback.print_exc()
        return Response(f"Error: {e}", status=500)


# -------- Helpers --------
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


def do_capture_both(stock: str) -> tuple[str, str, str, dict]:
    """
    Returns: (price_png_path, payment_png_path, url, diagnostics_dict)
    """
    url = f"https://rv.campingworld.com/rv/{stock}"
    tmpdir = tempfile.mkdtemp(prefix=f"cw-{stock}-")
    price_path = os.path.join(tmpdir, f"cw_{stock}_price.png")
    payment_path = os.path.join(tmpdir, f"cw_{stock}_payment.png")

    diag: dict[str, list[str] | str] = {"price": [], "payment": [], "meta": []}
    diag["meta"] = [f"tmpdir={tmpdir}", f"version={APP_VERSION}", f"url={url}"]

    print(f"\n===== START capture stock={stock} tmpdir={tmpdir} =====")

    with sync_playwright() as p:
        browser = p.chromium.launch(
            headless=True,
            args=["--no-sandbox", "--disable-setuid-sandbox", "--disable-dev-shm-usage"],
        )

        # --- PRICE TOOLTIP (own fresh page) ---
        try:
            ctx = browser.new_context(
                locale="en-US",
                geolocation={"latitude": 45.5122, "longitude": -122.6587},
                permissions=["geolocation"],
                viewport={"width": 1920, "height": 1080},
                user_agent="Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
                           "(KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
            )
            page = ctx.new_page()
            prep_page(page, url, diag["price"])
            capture_price_tooltip(page, price_path, diag["price"])
        except Exception as e:
            diag["price"].append(f"ERROR price: {e}")
            traceback.print_exc()
        finally:
            try:
                ctx.close()
            except Exception:
                pass

        # --- PAYMENT TOOLTIP (own fresh page; avoids auto-close interference) ---
        try:
            ctx2 = browser.new_context(
                locale="en-US",
                geolocation={"latitude": 45.5122, "longitude": -122.6587},
                permissions=["geolocation"],
                viewport={"width": 1920, "height": 1080},
                user_agent="Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
                           "(KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
            )
            page2 = ctx2.new_page()
            prep_page(page2, url, diag["payment"])
            capture_payment_tooltip(page2, payment_path, diag["payment"])
        except Exception as e:
            diag["payment"].append(f"ERROR payment: {e}")
            traceback.print_exc()
        finally:
            try:
                ctx2.close()
            except Exception:
                pass

        browser.close()

    print("===== END capture =====\n")
    return price_path, payment_path, url, diag


def prep_page(page, url: str, log: list[str]) -> None:
    log.append("goto domcontentloaded")
    page.goto(url, wait_until="domcontentloaded")

    # prefer network idle, but don't die if site keeps polling
    try:
        page.wait_for_load_state("networkidle", timeout=30_000)
        log.append("networkidle reached")
    except PWTimeout:
        log.append("networkidle timeout (ignored)")

    # set zip/cookie to Oregon & reload
    try:
        page.evaluate(
            """(zip) => {
                try { localStorage.setItem('cw_zip', zip); } catch(e){}
                document.cookie = `cw_zip=${zip};path=/;SameSite=Lax`;
            }""",
            OREGON_ZIP,
        )
        log.append(f"ZIP injected {OREGON_ZIP}")
        page.reload(wait_until="domcontentloaded")
        try:
            page.wait_for_load_state("networkidle", timeout=20_000)
            log.append("post-zip networkidle reached")
        except PWTimeout:
            log.append("post-zip networkidle timeout (ignored)")
    except Exception as e:
        log.append(f"ZIP inject error: {e}")

    # hide chat / sticky overlays that can intercept hover
    try:
        page.add_style_tag(
            content="""
            [id*="intercom"], .intercom-lightweight-app, .intercom-launcher,
            [class*="LiveHelp"], [data-testid*="chat"], [class*="cookie"], 
            iframe[src*="livechat"], .sticky, [role="dialog"] { z-index: 1 !important; pointer-events:none !important; opacity:0.001 !important; }
        """
        )
        log.append("overlay-hiding CSS injected")
    except Exception as e:
        log.append(f"overlay CSS error: {e}")


# ---------- Tooltip Captures ----------
def capture_price_tooltip(page, out_path: str, log: list[str]) -> None:
    """
    Keep the known-good price capture: hover the visible .MuiTypography-subtitle1,
    pause for tooltip, screenshot full page.
    """
    try:
        page.wait_for_selector(".MuiTypography-root.MuiTypography-subtitle1", state="visible", timeout=15_000)
        log.append("price subtitle1 visible")
    except PWTimeout:
        log.append("price subtitle1 wait timeout (continue)")

    sel = ".MuiTypography-root.MuiTypography-subtitle1:visible"
    loc = page.locator(sel)
    count = loc.count()
    log.append(f"price candidates={count}")

    target = None
    for i in range(count):
        e = loc.nth(i)
        if e.is_visible():
            target = e
            log.append(f"price use idx={i} text='{(e.text_content() or '').strip()[:80]}'")
            break

    if not target:
        log.append("no visible price element")
        return

    try:
        target.scroll_into_view_if_needed(timeout=5000)
        target.hover(timeout=10_000, force=True)
        page.wait_for_timeout(1000)
        # wait until a tooltip-like popper appears
        try:
            page.wait_for_selector('[role="tooltip"], .MuiTooltip-popper, [data-popper-placement]', timeout=3000)
            log.append("price tooltip appeared")
        except PWTimeout:
            log.append("price tooltip not detected (may still be embedded text)")
        page.screenshot(path=out_path, full_page=True)
        log.append(f"price screenshot saved {out_path} size={_fsize(out_path)}")
    except Exception as e:
        log.append(f"price hover failed: {e}")
        traceback.print_exc()


def capture_payment_tooltip(page, out_path: str, log: list[str]) -> None:
    """
    Robustly find & hover the *small inverted exclamation / info icon* that opens
    the payment breakdown tooltip next to the $/mo text. We:
      1) Find a payment text node (~ '/mo', 'monthly', 'per month').
      2) From its nearest meaningful container, search for a likely info icon:
         - [data-testid*="Info"] or svg with 'Info' in class
         - element with aria-label including 'info' or 'more information'
         - an inline element with a circular 'i' glyph and class 'MuiSvgIcon-root'
      3) Hover that icon using mouse coordinates (works even if hover() is blocked).
      4) Wait for a MUI tooltip popper ([role=tooltip] / .MuiTooltip-popper).
    """
    # 1) Find payment text
    payment_text_locator = page.locator(
        'xpath=//*[matches(normalize-space(.), "(?i)(\\$\\s?\\d[\\d,\\.]*\\s*/\\s*mo)|(per\\s*month)|(monthly)")]'
    )

    try:
        page.wait_for_function(
            "(loc) => loc.count() > 0",
            arg=payment_text_locator,
            timeout=10_000
        )
    except PWTimeout:
        log.append("no payment text match by regex; trying class-based subtitle2")
        payment_text_locator = page.locator('.MuiTypography-root.MuiTypography-subtitle2:has-text("/mo"), .MuiTypography-subtitle2:has-text("monthly")')

    count = payment_text_locator.count()
    log.append(f"payment text candidates={count}")
    if count == 0:
        log.append("payment: give up (no text anchor)")
        return

    anchor = payment_text_locator.first
    try:
        anchor.scroll_into_view_if_needed(timeout=5000)
        log.append("payment anchor scrolled into view")
    except Exception:
        pass

    # 2) Find the *icon trigger* near the anchor.
    # Try progressively broader XPaths around the anchor.
    icon_candidates = [
        # immediate following siblings with common icon classes
        'xpath=following::*[self::button or self::span or self::svg][contains(@class,"Info") or contains(@class,"MuiSvgIcon-root") or contains(@data-testid,"Info")][1]',
        # within same container
        'xpath=ancestor::*[contains(@class,"Mui")][1]//*[self::button or self::span or self::svg][contains(@class,"Info") or contains(@class,"MuiSvgIcon-root") or contains(@data-testid,"Info")][1]',
        # any nearby element with an info-ish aria label
        'xpath=following::*[@aria-label and (contains(translate(@aria-label, "INFO", "info"), "info") or contains(translate(@aria-label,"PAYMENT","payment"),"payment"))][1]',
        # last resort: anything that will get a tooltip when hovered (attribute appears after hover, but some sites pre-set it)
        'xpath=ancestor::*[contains(@class,"Mui")][1]//*[@aria-describedby][1]'
    ]

    trigger = None
    for xp in icon_candidates:
        cand = anchor.locator(xp)
        if cand.count() > 0 and cand.first.is_visible():
            trigger = cand.first
            log.append(f"payment icon via '{xp}'")
            break

    # If nothing obvious, brute-force: look for any small icon right of the anchor by x-position.
    if not trigger:
        try:
            anc_box = anchor.bounding_box()
            icons = page.locator('svg.MuiSvgIcon-root, [data-testid*="Info"], span.MuiSvgIcon-root')
            n = icons.count()
            best_i = -1
            best_dx = 99999
            for i in range(n):
                el = icons.nth(i)
                if not el.is_visible():
                    continue
                bb = el.bounding_box()
                if not bb or not anc_box:
                    continue
                # to the right & roughly aligned vertically
                if bb["x"] > anc_box["x"] and abs((bb["y"] + bb["height"]/2) - (anc_box["y"] + anc_box["height"]/2)) < 60:
                    dx = bb["x"] - anc_box["x"]
                    if 0 < dx < best_dx:
                        best_dx = dx
                        best_i = i
            if best_i >= 0:
                trigger = icons.nth(best_i)
                log.append(f"payment icon via geometric search index={best_i} dx≈{best_dx:.1f}")
        except Exception as e:
            log.append(f"geom search error: {e}")

    if not trigger:
        log.append("payment: no icon trigger found")
        return

    # 3) Hover using precise mouse coordinates (more reliable for icon glyphs)
    try:
        bb = trigger.bounding_box()
        if not bb:
            log.append("payment trigger has no bounding box")
            return

        # small nudge pattern to ensure hover fires
        cx = bb["x"] + bb["width"] / 2
        cy = bb["y"] + bb["height"] / 2
        page.mouse.move(cx - 1, cy - 1)
        page.mouse.move(cx, cy, steps=6)
        page.mouse.move(cx + 1, cy + 1, steps=4)
        log.append(f"mouse hovered icon at ({cx:.1f},{cy:.1f}) w={bb['width']:.1f} h={bb['height']:.1f}")

        # 4) Wait for tooltip
        page.wait_for_selector('[role="tooltip"], .MuiTooltip-popper, [data-popper-placement]', state="visible", timeout=5_000)
        page.wait_for_timeout(600)  # small settle
        page.screenshot(path=out_path, full_page=True)
        log.append(f"payment screenshot saved {out_path} size={_fsize(out_path)}")
    except PWTimeout:
        log.append("payment tooltip did not appear after hover (timeout)")
    except Exception as e:
        log.append(f"payment hover/screenshot failed: {e}")
        traceback.print_exc()


def _fsize(path: str) -> str:
    try:
        return f"{os.path.getsize(path)} bytes"
    except Exception:
        return "0"


# -------- HTML Template --------
HTML_TEMPLATE = """
<!doctype html>
<html>
<head>
  <meta charset="utf-8" />
  <title>Camping World Compliance Capture</title>
  <style>
    :root { --bg:#f5f6fa; --card:#fff; --ink:#111; --muted:#666; --ok:#0a7a2a; --bad:#b00020; --border:#ddd; }
    body { margin:24px; font: 15px/1.45 -apple-system,BlinkMacSystemFont,Segoe UI,Roboto,Helvetica,Arial; color:var(--ink); background:var(--bg);}
    h1 { font-size:28px; margin:0 0 14px; }
    .meta { background:#fff; border:1px solid var(--border); border-radius:10px; padding:14px 18px; margin-bottom:18px; }
    .meta a { color:#0b69c7; text-decoration:none; }
    .grid { display:grid; grid-template-columns: 1fr 1fr; gap:18px; }
    .card { background:var(--card); border:1px solid var(--border); border-radius:12px; overflow:hidden; }
    .card h2 { margin:0; padding:14px 16px; border-bottom:1px solid var(--border); font-size:20px; }
    .body { padding:14px 16px; }
    .shot { background:#fafafa; border:1px solid #e6e6e6; border-radius:8px; padding:10px; min-height:220px; display:flex; align-items:center; justify-content:center; }
    img { max-width:100%; height:auto; display:block; }
    .ok { color:var(--ok); font-weight:600; }
    .bad { color:var(--bad); font-weight:600; }
    code { background:#f0f3f7; padding:2px 6px; border-radius:4px; }
    .footer { color:var(--muted); margin-top:8px; font-size:12px; }
    .diag { font-family: ui-monospace,SFMono-Regular,Consolas,monospace; font-size:12px; white-space:pre-wrap; background:#f9fafc; border-top:1px dashed #e5e7eb; padding:10px 14px; color:#344054; }
    @media (max-width:1100px){ .grid{ grid-template-columns:1fr; } }
  </style>
</head>
<body>
  <h1>Camping World Compliance Capture</h1>
  <div class="meta">
    <div><strong>Stock:</strong> {{stock}}</div>
    <div><strong>URL:</strong> <a href="{{url}}" target="_blank">{{url}}</a></div>
    <div><strong>UTC:</strong> {{utc_now}}</div>
    <div><strong>HTTPS Date:</strong> {{hdate or 'unavailable'}}</div>
    <div class="footer">Build: {{version}}</div>
  </div>

  <div class="grid">
    <div class="card">
      <h2>Price Tooltip — Full Page</h2>
      <div class="body">
        <div class="shot">
          {% if price_exists %}
            <img src="/screenshot/{{price_id}}" alt="Price Tooltip Screenshot">
          {% else %}
            <div class="bad">✗ Could not capture price tooltip.</div>
          {% endif %}
        </div>
        <div style="margin-top:10px;"><strong>SHA-256:</strong> <code>{{sha_price}}</code></div>
        <div class="diag">{{ '\n'.join(diag['price']) }}</div>
      </div>
    </div>

    <div class="card">
      <h2>Payment Tooltip — Full Page</h2>
      <div class="body">
        <div class="shot">
          {% if payment_exists %}
            <img src="/screenshot/{{pay_id}}" alt="Payment Tooltip Screenshot">
          {% else %}
            <div class="bad">✗ Could not capture payment tooltip.</div>
          {% endif %}
        </div>
        <div style="margin-top:10px;"><strong>SHA-256:</strong> <code>{{sha_payment}}</code></div>
        <div class="diag">{{ '\n'.join(diag['payment']) }}</div>
      </div>
    </div>
  </div>
</body>
</html>
"""


# -------- Entrypoint --------
if __name__ == "__main__":
    print(f"Launching server on 0.0.0.0:{PORT}  ({APP_VERSION})")
    app.run(host="0.0.0.0", port=PORT)
