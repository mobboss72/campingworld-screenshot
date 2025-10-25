# server.py
import os, sys, hashlib, datetime, tempfile, traceback, requests, time, base64, io, re
from flask import Flask, request, send_from_directory, Response, render_template_string, send_file, jsonify
from playwright.sync_api import sync_playwright, TimeoutError as PlaywrightTimeout
from rfc3161ng import RemoteTimestamper  # decode import happens inside the function for compat
from reportlab.lib.pagesizes import letter
from reportlab.lib import colors
from reportlab.lib.styles import getSampleStyleSheet, ParagraphStyle
from reportlab.lib.units import inch
from reportlab.platypus import SimpleDocTemplate, Paragraph, Spacer, Image, Table, TableStyle, PageBreak
from reportlab.lib.enums import TA_LEFT, TA_CENTER
from PIL import Image as PILImage
import sqlite3
from contextlib import contextmanager
from functools import wraps
import threading

# -------------------- Config --------------------

# Admin password
ADMIN_PASSWORD = os.getenv("ADMIN_PASSWORD", "cwadmin2025")  # Change this!

# Persist Playwright downloads
os.environ.setdefault("PLAYWRIGHT_BROWSERS_PATH", "/app/ms-playwright")

PORT = int(os.getenv("PORT", "8080"))
DB_PATH = os.getenv("DB_PATH", "/app/data/captures.db")

# Storage configuration
STORAGE_MODE = os.getenv("STORAGE_MODE", "persistent")  # "persistent" or anything else for tmp
PERSISTENT_STORAGE_PATH = os.getenv("PERSISTENT_STORAGE_PATH", "/app/data/captures")
AUTO_CLEANUP_DAYS = int(os.getenv("AUTO_CLEANUP_DAYS", "90"))  # 90 days retention

# Oregon Camping World locations (alphabetical) with coordinates
CW_LOCATIONS = {
    "bend": {"name": "Bend", "zip": "97701", "lat": 44.0582, "lon": -121.3153},
    "eugene": {"name": "Eugene", "zip": "97402", "lat": 44.0521, "lon": -123.0868},
    "hillsboro": {"name": "Hillsboro", "zip": "97124", "lat": 45.5229, "lon": -122.9898},
    "medford": {"name": "Medford", "zip": "97504", "lat": 42.3265, "lon": -122.8756},
    "portland": {"name": "Portland", "zip": "97201", "lat": 45.5152, "lon": -122.6784},
}

# RFC 3161 Timestamp Authority URLs (prefer stable, public TSAs)
TSA_URLS = [
    "http://timestamp.digicert.com",
    "http://timestamp.sectigo.com",
    "http://rfc3161timestamp.globalsign.com/advanced",
    "http://tsa.swisssign.net",
]

screenshot_cache = {}

app = Flask(__name__, static_folder=None)
app.secret_key = os.getenv("SECRET_KEY", "cw-compliance-secret-key-change-me")  # Change this!

# -------------------- Database --------------------

@contextmanager
def get_db():
    """Database connection context manager"""
    os.makedirs(os.path.dirname(DB_PATH), exist_ok=True)
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    try:
        yield conn
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()

def init_db():
    """Initialize database tables"""
    with get_db() as conn:
        conn.execute("""
            CREATE TABLE IF NOT EXISTS captures (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                stock TEXT NOT NULL,
                location TEXT NOT NULL,
                zip_code TEXT NOT NULL,
                url TEXT NOT NULL,
                capture_utc TEXT NOT NULL,
                https_date TEXT,
                price_sha256 TEXT,
                payment_sha256 TEXT,
                price_screenshot_path TEXT,
                payment_screenshot_path TEXT,
                price_tsa TEXT,
                price_timestamp TEXT,
                payment_tsa TEXT,
                payment_timestamp TEXT,
                pdf_path TEXT,
                debug_info TEXT,
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            )
        """)
        conn.execute("CREATE INDEX IF NOT EXISTS idx_stock ON captures(stock)")
        conn.execute("CREATE INDEX IF NOT EXISTS idx_created_at ON captures(created_at DESC)")

init_db()

# -------------------- Automatic Cleanup Scheduler --------------------

def cleanup_old_files(days_old=90):
    """Clean up screenshot files and PDFs older than specified days"""
    try:
        cutoff_time = time.time() - (days_old * 24 * 60 * 60)
        cleaned_count = 0
        cleaned_size = 0

        # Clean up temp directories
        temp_base = tempfile.gettempdir()
        for item in os.listdir(temp_base):
            if item.startswith("cw-"):
                item_path = os.path.join(temp_base, item)
                try:
                    if os.path.isdir(item_path):
                        dir_mtime = os.path.getmtime(item_path)
                        if dir_mtime < cutoff_time:
                            for root, dirs, files in os.walk(item_path):
                                for f in files:
                                    fp = os.path.join(root, f)
                                    if os.path.exists(fp):
                                        cleaned_size += os.path.getsize(fp)
                            import shutil
                            shutil.rmtree(item_path)
                            cleaned_count += 1
                            print(f"🧹 Cleaned old temp dir: {item}")
                except Exception as e:
                    print(f"⚠ Could not clean {item_path}: {e}")

        # Clean up persistent storage
        if STORAGE_MODE == "persistent" and os.path.exists(PERSISTENT_STORAGE_PATH):
            for item in os.listdir(PERSISTENT_STORAGE_PATH):
                if item.startswith("cw-"):
                    item_path = os.path.join(PERSISTENT_STORAGE_PATH, item)
                    try:
                        if os.path.isdir(item_path):
                            dir_mtime = os.path.getmtime(item_path)
                            if dir_mtime < cutoff_time:
                                for root, dirs, files in os.walk(item_path):
                                    for f in files:
                                        fp = os.path.join(root, f)
                                        if os.path.exists(fp):
                                            cleaned_size += os.path.getsize(fp)
                                import shutil
                                shutil.rmtree(item_path)
                                cleaned_count += 1
                                print(f"🧹 Cleaned old persistent dir: {item}")
                    except Exception as e:
                        print(f"⚠ Could not clean {item_path}: {e}")

        cleaned_size_mb = cleaned_size / (1024 * 1024)
        print(f"✓ Cleanup complete: removed {cleaned_count} dirs ({cleaned_size_mb:.2f} MB)")
        return {"cleaned": cleaned_count, "size_mb": round(cleaned_size_mb, 2)}
    except Exception as e:
        print(f"❌ Cleanup failed: {e}")
        return {"cleaned": 0, "size_mb": 0}

def schedule_cleanup():
    """Run cleanup every 24 hours"""
    def cleanup_task():
        while True:
            time.sleep(24 * 60 * 60)
            print("🕐 Running scheduled cleanup...")
            try:
                result = cleanup_old_files(AUTO_CLEANUP_DAYS)
                print(f"✓ Scheduled cleanup complete: {result['cleaned']} dirs, {result['size_mb']} MB freed")
            except Exception as e:
                print(f"❌ Scheduled cleanup failed: {e}")

    cleanup_thread = threading.Thread(target=cleanup_task, daemon=True)
    cleanup_thread.start()
    print(f"✓ Automatic cleanup scheduled (every 24 hours, {AUTO_CLEANUP_DAYS} day retention)")

# Start cleanup scheduler
schedule_cleanup()

# -------------------- Admin Authentication --------------------

def check_admin_auth():
    """Check if admin is authenticated via session or basic auth"""
    from flask import session
    if session.get('admin_authenticated'):
        return True
    auth = request.authorization
    if auth and auth.password == ADMIN_PASSWORD:
        return True
    return False

def require_admin_auth(f):
    """Decorator to require admin authentication"""
    @wraps(f)
    def decorated(*args, **kwargs):
        if check_admin_auth():
            return f(*args, **kwargs)
        return Response('Authentication required', 401, {'WWW-Authenticate': 'Basic realm="Admin Panel"'})
    return decorated

# -------------------- Utilities --------------------

def sha256_file(path):
    if not path or not os.path.exists(path): return "N/A"
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(1024 * 1024), b""):
            h.update(chunk)
    return h.hexdigest()

def https_date():
    try:
        r = requests.head("https://cloudflare.com", timeout=8)
        return r.headers.get("Date")
    except Exception:
        return None

def get_rfc3161_timestamp(file_path):
    """Request an RFC 3161 timestamp token for a file.
       Tries multiple TSAs. Returns dict with timestamp, tsa, and token path, or None."""
    if not file_path or not os.path.exists(file_path):
        return None

    print(f"🕐 Getting RFC 3161 timestamp for {os.path.basename(file_path)}...")

    # Read full file bytes (preferred by most TSAs)
    with open(file_path, "rb") as f:
        file_bytes = f.read()

    # Precompute SHA-256 for hash-only fallback
    digest = hashlib.sha256(file_bytes).digest()

    try:
        # Import here for compatibility with different rfc3161ng versions
        from rfc3161ng import RemoteTimestamper, decode_timestamp_response
    except Exception as e:
        print(f"✗ rfc3161ng not available: {e}")
        return None

    for tsa_url in TSA_URLS:
        try:
            print(f"  Trying TSA: {tsa_url}")
            rt = RemoteTimestamper(tsa_url, hashname="sha256")

            tsr = None
            # 1) Try sending file bytes (with certreq if supported)
            try:
                try:
                    tsr = rt.timestamp(data=file_bytes, certreq=True)   # newer rfc3161ng
                except TypeError:
                    tsr = rt.timestamp(data=file_bytes)                 # older rfc3161ng
            except Exception as inner_e:
                print(f"    data= failed ({inner_e}); retrying with data_hash...")

            # 2) Fallback: pass the digest explicitly
            if not tsr:
                try:
                    try:
                        tsr = rt.timestamp(data_hash=digest, certreq=True)
                    except TypeError:
                        tsr = rt.timestamp(data_hash=digest)
                except Exception as inner2_e:
                    print(f"    data_hash= failed ({inner2_e})")

            if not tsr:
                print("    ✗ TSA returned no token")
                continue

            ts_info = decode_timestamp_response(tsr)
            ts_dt = getattr(ts_info, "gen_time", None)
            ts_str = ts_dt.strftime("%Y-%m-%d %H:%M:%S UTC") if ts_dt else "Unknown Time"

            token_path = file_path + ".tsr"
            with open(token_path, "wb") as tf:
                tf.write(tsr)

            print(f"  ✓ Timestamp obtained from {tsa_url}: {ts_str}")
            return {
                "timestamp": ts_str,
                "tsa": tsa_url,
                "cert_info": f"Token saved: {os.path.basename(token_path)}",
                "token_file": token_path,
            }

        except Exception as e:
            print(f"  ✗ TSA {tsa_url} failed: {e}")
            continue

    print(f"  ✗ All TSAs failed for {os.path.basename(file_path)}")
    return None

# -------------------- PDF (One-page) --------------------

def generate_pdf(
    stock, location, zip_code, url, utc_time, https_date_value,
    price_path, pay_path, sha_price, sha_pay,
    rfc_price, rfc_pay, debug_info
):
    """
    Generate a single-page LETTER PDF.
    - Title + metadata at the top (compact)
    - 1 or 2 screenshots scaled to fit the remaining page height (stacked)
    - SHA256 + RFC3161 info at the bottom
    - If used RV (inferred from debug_info) and price is absent: show the 'Used RV selected...' note
    """
    try:
        from reportlab.pdfgen import canvas as pdfcanvas
        from reportlab.lib.pagesizes import letter
        from reportlab.lib.units import inch

        page_w, page_h = letter
        margin = 0.35 * inch
        gap_small = 0.10 * inch
        gap_img   = 0.15 * inch
        title_h   = 0.25 * inch
        meta_line_h = 0.16 * inch
        footer_min = 0.90 * inch  # reserved for hashes + RFC info

        # Paths present?
        price_ok = bool(price_path and os.path.exists(price_path))
        pay_ok   = bool(pay_path and os.path.exists(pay_path))

        # Used unit note?
        is_used = bool(debug_info and "Unit type: Used" in debug_info)

        # Open images
        imgs = []
        dims = []
        if price_ok:
            im1 = PILImage.open(price_path)
            imgs.append(("Price Disclosure", price_path, im1))
            dims.append((im1.width, im1.height))
        if pay_ok:
            im2 = PILImage.open(pay_path)
            imgs.append(("Payment Disclosure", pay_path, im2))
            dims.append((im2.width, im2.height))

        # Output path (prefer an existing image dir)
        if price_ok:
            tmpdir = os.path.dirname(price_path)
        elif pay_ok:
            tmpdir = os.path.dirname(pay_path)
        else:
            tmpdir = tempfile.mkdtemp(prefix=f"cw-{stock}-")

        pdf_path = os.path.join(tmpdir, f"cw_{stock}_report.pdf")
        c = pdfcanvas.Canvas(pdf_path, pagesize=letter)

        y = page_h - margin

        # Title
        c.setFont("Helvetica-Bold", 14)
        c.drawString(margin, y, "Camping World Compliance Capture Report")
        y -= title_h

        # Metadata
        c.setFont("Helvetica", 8)
        meta_lines_list = [
            f"Stock: {stock}",
            f"Location: {location} (ZIP: {zip_code})",
            f"URL: {url or 'N/A'}",
            f"Capture Time (UTC): {utc_time}",
            f"HTTPS Date: {https_date_value or 'N/A'}",
        ]

        max_text_width = page_w - 2 * margin

        def draw_wrapped_line(text, x, y, max_width, font="Helvetica", size=8, leading=11):
            c.setFont(font, size)
            words = (text or "").split()
            line = ""
            used_y = 0
            for w in words:
                test = w if not line else (line + " " + w)
                if c.stringWidth(test, font, size) <= max_width:
                    line = test
                else:
                    c.drawString(x, y - used_y, line)
                    used_y += leading
                    line = w
            if line:
                c.drawString(x, y - used_y, line)
                used_y += leading
            return used_y

        for line in meta_lines_list:
            if line.startswith("URL: "):
                used_h = draw_wrapped_line(line, margin, y, max_text_width, size=8, leading=11)
                y -= used_h
            else:
                c.drawString(margin, y, line)
                y -= meta_line_h

        y -= gap_small

        # Used banner
        if is_used and not price_ok:
            c.setFont("Helvetica-Bold", 9)
            c.drawString(margin, y, "Used RV selected — no pricing breakdown needed.")
            y -= (gap_small + 0.05 * inch)

        # Available height for images
        available_for_imgs = max(0, y - margin - footer_min)

        # Scale & draw images (stacked)
        if imgs:
            max_img_w = page_w - 2 * margin
            scaled = []
            for (label, path, im), (w, h) in zip(imgs, dims):
                if w == 0 or h == 0:
                    scaled.append((label, path, 0, 0))
                    continue
                s = max_img_w / float(w)
                scaled.append((label, path, w * s, h * s))

            total_h = sum(h for _, _, _, h in scaled) + (len(scaled) - 1) * gap_img

            if total_h > available_for_imgs and total_h > 0:
                shrink = available_for_imgs / total_h
                scaled = [(label, path, w * shrink, h * shrink) for (label, path, w, h) in scaled]

            for idx, (label, path, dw, dh) in enumerate(scaled):
                if dw <= 0 or dh <= 0:
                    continue
                c.setFont("Helvetica", 8)
                c.drawString(margin, y, label)
                y -= 0.13 * inch
                c.drawImage(path, margin, y - dh, width=dw, height=dh, preserveAspectRatio=True, mask='auto')
                y -= dh
                if idx < len(scaled) - 1:
                    y -= gap_img
        else:
            c.setFont("Helvetica-Oblique", 9)
            c.drawString(margin, y, "No screenshots available for this capture.")
            y -= 0.25 * inch

        # Footer: SHA-256
        y -= 0.20 * inch
        c.setFont("Helvetica-Bold", 10)
        c.drawString(margin, y, "SHA-256 Verification")
        y -= 0.16 * inch
        c.setFont("Courier", 7)

        def draw_hash(label, value, y):
            txt = f"{label}: {value or 'N/A'}"
            used_h = draw_wrapped_line(txt, margin, y, max_text_width, font="Courier", size=7, leading=9)
            return y - used_h

        if sha_price and sha_price != "N/A":
            y = draw_hash("Price Disclosure", sha_price, y)
        if sha_pay and sha_pay != "N/A":
            y = draw_hash("Payment Disclosure", sha_pay, y)

        # Footer: RFC 3161
        y -= 0.15 * inch
        c.setFont("Helvetica-Bold", 10)
        c.drawString(margin, y, "RFC-3161 Timestamps")
        y -= 0.16 * inch
        c.setFont("Helvetica", 8)

        def draw_rfc(label, data, y):
            if not data:
                c.drawString(margin, y, f"{label}: N/A")
                return y - 0.14 * inch
            lines = [
                f"{label}:",
                f"  Timestamp: {data.get('timestamp', 'N/A')}",
                f"  TSA: {data.get('tsa', 'N/A')}",
            ]
            token = data.get("token_file")
            if token:
                lines.append(f"  Token: {os.path.basename(token)}")
            elif data.get("cert_info"):
                lines.append(f"  Info: {data['cert_info']}")
            used = 0
            for ln in lines:
                used += draw_wrapped_line(ln, margin, y - used, max_text_width, font="Helvetica", size=8, leading=11)
            return y - used - 2

        y = draw_rfc("Price", rfc_price, y)
        y = draw_rfc("Payment", rfc_pay, y)

        c.showPage()  # single page
        c.save()
        print(f"✓ PDF generated (one page): {pdf_path} ({os.path.getsize(pdf_path)} bytes)")
        return pdf_path

    except Exception as e:
        print(f"❌ PDF generation failed: {e}")
        traceback.print_exc()
        return None

# -------------------- Tooltip Helper --------------------

def find_and_trigger_tooltip(page, label_text, tooltip_name):
    """Enhanced tooltip triggering with multiple fallback strategies."""
    debug = []
    debug.append(f"Attempting to trigger {tooltip_name} tooltip for label: '{label_text}'")

    try:
        page.wait_for_timeout(1500)
        all_labels = page.locator(f"text={label_text}").all()
        debug.append(f"Found {len(all_labels)} instances of '{label_text}'")

        if len(all_labels) == 0:
            debug.append("❌ No instances found - element may not exist on page")
            return False, "\n".join(debug)

        success = False
        for idx, label in enumerate(all_labels):
            try:
                is_visible = label.is_visible(timeout=1000)
                if not is_visible:
                    debug.append(f"  Instance {idx}: not visible, skipping")
                    continue

                debug.append(f"  Instance {idx}: visible, attempting trigger")
                label.scroll_into_view_if_needed(timeout=3000)
                page.wait_for_timeout(800)

                icon_found = False
                try:
                    parent = label.locator("xpath=..").first
                    svg_icons = parent.locator("svg.MuiSvgIcon-root").all()
                    debug.append(f"    Found {len(svg_icons)} SVG icons in parent")
                    for svg_idx, svg_icon in enumerate(svg_icons):
                        try:
                            if svg_icon.is_visible(timeout=500):
                                debug.append(f"    Attempting to click SVG icon {svg_idx}...")
                                svg_icon.click(timeout=2000, force=True)
                                page.wait_for_timeout(1000)
                                icon_found = True
                                debug.append(f"    ✓ Clicked SVG icon {svg_idx}")
                                break
                        except Exception as e:
                            debug.append(f"    SVG {svg_idx} click failed: {str(e)[:100]}")
                            continue
                except Exception as e:
                    debug.append(f"    Parent SVG search failed: {str(e)[:100]}")

                if not icon_found:
                    try:
                        debug.append(f"    Trying data-testid selectors...")
                        info_selectors = [
                            '[data-testid*="info"]',
                            '[data-testid*="Info"]',
                            'svg[data-testid]',
                        ]
                        for selector in info_selectors:
                            nearby_icons = page.locator(selector).all()
                            if len(nearby_icons) > 0:
                                debug.append(f"    Found {len(nearby_icons)} with {selector}")
                                for icon in nearby_icons:
                                    try:
                                        if icon.is_visible(timeout=500):
                                            icon.click(timeout=2000, force=True)
                                            page.wait_for_timeout(1000)
                                            icon_found = True
                                            debug.append(f"    ✓ Clicked icon via {selector}")
                                            break
                                    except:
                                        continue
                            if icon_found:
                                break
                    except Exception as e:
                        debug.append(f"    data-testid search failed: {str(e)[:100]}")

                if not icon_found:
                    debug.append("    No icon found, hovering label as fallback...")
                    try:
                        label.hover(timeout=2000, force=True)
                        page.wait_for_timeout(1200)
                        debug.append("    ✓ Hovered label")
                    except Exception as e:
                        debug.append(f"    Hover failed: {str(e)[:100]}")

                page.wait_for_timeout(1500)

                tooltip_selectors = [
                    "[role='tooltip']",
                    ".MuiTooltip-popper",
                    ".MuiTooltip-tooltip",
                    ".MuiPopper-root",
                ]
                tooltip_found = False
                for selector in tooltip_selectors:
                    try:
                        tooltips = page.locator(selector).all()
                        for tooltip in tooltips:
                            if tooltip.is_visible(timeout=1000):
                                debug.append(f"    ✓ Tooltip visible with: {selector}")
                                page.wait_for_timeout(1000)
                                tooltip_found = True
                                success = True
                                break
                        if tooltip_found:
                            break
                    except:
                        continue

                if success:
                    debug.append(f"  ✓ Successfully triggered tooltip from instance {idx}")
                    break
                else:
                    debug.append(f"    ⚠ No tooltip appeared for instance {idx}")

            except Exception as e:
                debug.append(f"  Instance {idx} failed: {str(e)[:150]}")
                continue

        if not success:
            debug.append("⚠ All standard methods failed, trying JavaScript injection...")
            try:
                result = page.evaluate(f"""
                    () => {{
                        const labels = Array.from(document.querySelectorAll('*'))
                            .filter(el => el.textContent.trim() === '{label_text}');
                        for (const label of labels) {{
                            const parent = label.parentElement;
                            if (!parent) continue;
                            const svg = parent.querySelector('svg');
                            if (svg) {{
                                svg.scrollIntoView({{behavior: 'smooth', block: 'center'}});
                                setTimeout(() => {{
                                    ['mouseenter', 'mouseover', 'mousemove', 'click'].forEach(eventType => {{
                                        svg.dispatchEvent(new MouseEvent(eventType, {{ bubbles: true, cancelable: true, view: window }}));
                                    }});
                                }}, 500);
                                return true;
                            }}
                        }}
                        return false;
                    }}
                """)
                if result:
                    page.wait_for_timeout(2000)
                    debug.append("✓ JavaScript fallback executed - events dispatched")
                    for selector in tooltip_selectors:
                        try:
                            if page.locator(selector).first.is_visible(timeout=2000):
                                debug.append(f"✓ Tooltip appeared after JS fallback: {selector}")
                                success = True
                                break
                        except:
                            continue
                else:
                    debug.append("⚠ JavaScript fallback: no SVG elements found")
            except Exception as e:
                debug.append(f"JavaScript fallback error: {str(e)[:150]}")

        return success, "\n".join(debug)

    except Exception as e:
        debug.append(f"❌ Critical Error: {str(e)}")
        traceback.print_exc()
        return False, "\n".join(debug)

# -------------------- Capture Core --------------------

def do_capture(stock, zip_code, location_name, latitude, longitude):
    requested_url = f"https://rv.campingworld.com/rv/{stock}"

    if STORAGE_MODE == "persistent" and PERSISTENT_STORAGE_PATH:
        os.makedirs(PERSISTENT_STORAGE_PATH, exist_ok=True)
        tmpdir = os.path.join(PERSISTENT_STORAGE_PATH, f"cw-{stock}-{int(time.time())}")
        os.makedirs(tmpdir, exist_ok=True)
    else:
        tmpdir = tempfile.mkdtemp(prefix=f"cw-{stock}-")

    price_png = os.path.join(tmpdir, f"cw_{stock}_price.png")
    pay_png   = os.path.join(tmpdir, f"cw_{stock}_payment.png")

    all_debug = []
    all_debug.append(f"Starting capture for stock: {stock}")
    all_debug.append(f"Requested URL: {requested_url}")
    all_debug.append(f"Location: {location_name} (ZIP: {zip_code})")
    all_debug.append(f"Coordinates: {latitude}, {longitude}")

    # Identify if unit is used (contains letters)
    is_used = bool(re.search(r"[a-zA-Z]", stock))
    all_debug.append(f"Unit type: {'Used' if is_used else 'New'}")

    print(f"🚀 Starting capture: {requested_url} (ZIP: {zip_code}, Location: {location_name})")
    final_url = requested_url

    price_png_path = None
    pay_png_path = None

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
                geolocation={"latitude": latitude, "longitude": longitude},
                permissions=["geolocation"]
            )
            all_debug.append(f"✓ Browser context created with geolocation: {latitude}, {longitude}")

            page = context.new_page()
            all_debug.append("Navigating to page...")
            page.goto(requested_url, wait_until="domcontentloaded", timeout=60_000)

            try:
                page.wait_for_load_state("networkidle", timeout=30_000)
                all_debug.append("✓ Network idle reached")
            except:
                all_debug.append("⚠ Network idle timeout (continuing anyway)")

            try:
                page.evaluate(f"""
                    localStorage.setItem('cw_zip', '{zip_code}');
                    document.cookie = 'cw_zip={zip_code};path=/;SameSite=Lax';
                """)
                all_debug.append(f"✓ Injected ZIP: {zip_code}")
                page.reload(wait_until="load", timeout=30_000)
                page.wait_for_timeout(2000)
                all_debug.append("✓ Reloaded page with ZIP")
            except Exception as e:
                all_debug.append(f"⚠ ZIP injection issue: {e}")

            # Capture final URL after navigation/reloads
            final_url = page.url
            all_debug.append(f"✓ Final URL captured: {final_url}")

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
            page.wait_for_timeout(2000)

            # Scroll to pricing area
            try:
                page.evaluate("""
                    window.scrollTo({
                        top: document.body.scrollHeight * 0.3,
                        behavior: 'smooth'
                    });
                """)
                page.wait_for_timeout(1000)
                all_debug.append("✓ Scrolled to pricing section")
            except Exception as e:
                all_debug.append(f"⚠ Scroll failed: {e}")

            # NEW units: capture "Total Price" tooltip
            if not is_used:
                all_debug.append("\n--- Capturing Price Tooltip (New Unit) ---")
                success, debug_info = find_and_trigger_tooltip(page, "Total Price", "price")
                all_debug.append(debug_info)
                if success:
                    try:
                        page.screenshot(path=price_png, full_page=True)
                        size = os.path.getsize(price_png)
                        all_debug.append(f"✓ Price screenshot saved: {size} bytes")
                        print(f"✓ Price screenshot: {size} bytes")
                        price_png_path = price_png
                    except Exception as e:
                        all_debug.append(f"❌ Price screenshot failed: {e}")
            else:
                all_debug.append("\n--- Skipping Price Tooltip (Used Unit) ---")

            page.wait_for_timeout(1000)

            # Capture "Est. Payment" tooltip for both new/used
            all_debug.append("\n--- Capturing Payment Tooltip ---")
            success, debug_info = find_and_trigger_tooltip(page, "Est. Payment", "payment")
            all_debug.append(debug_info)
            if success:
                try:
                    page.screenshot(path=pay_png, full_page=True)
                    size = os.path.getsize(pay_png)
                    all_debug.append(f"✓ Payment screenshot saved: {size} bytes")
                    print(f"✓ Payment screenshot: {size} bytes")
                    pay_png_path = pay_png
                except Exception as e:
                    all_debug.append(f"❌ Payment screenshot failed: {e}")

            # Not found / fallback
            not_found = False
            if (is_used and pay_png_path is None) or (not is_used and price_png_path is None and pay_png_path is None):
                all_debug.append("\n--- Checking for 'No Matches Found' (Capture Failed) ---")
                try:
                    not_found_locator = page.locator(
                        "h1:text-matches('Page Not Found', 'i'), "
                        "h2:text-matches('No Matches Found', 'i'), "
                        ":text-matches('Listings Not Found', 'i'), "
                        ":text-matches('This listing is currently unavailable', 'i')"
                    ).first
                    if not_found_locator.is_visible(timeout=3000):
                        not_found_text = not_found_locator.text_content()
                        all_debug.append(f"✓ Found 'Not Found' text: {not_found_text.strip()}")
                        not_found_png = os.path.join(tmpdir, f"cw_{stock}_not_found.png")
                        page.screenshot(path=not_found_png, full_page=True)
                        size = os.path.getsize(not_found_png)
                        all_debug.append(f"✓ 'Not Found' screenshot saved: {size} bytes")
                        price_png_path = not_found_png  # use this as a single screenshot
                        not_found = True
                    else:
                        all_debug.append("⚠ No 'Not Found' text detected.")
                except Exception as e:
                    all_debug.append(f"⚠ Error checking for 'Not Found' text: {e}")

            if not_found:
                all_debug.append("NOT_FOUND_CAPTURE=TRUE")

            browser.close()
            all_debug.append("\n✓ Browser closed")

    except Exception as e:
        all_debug.append(f"\n❌ CRITICAL ERROR: {str(e)}")
        all_debug.append(traceback.format_exc())
        print(f"❌ Critical error in do_capture: {e}")
        traceback.print_exc()

    debug_output = "\n".join(all_debug)
    return price_png_path, pay_png_path, final_url, debug_output

# -------------------- Routes --------------------

@app.get("/")
def root():
    return send_from_directory(".", "index.html")

@app.get("/screenshot/<sid>")
def serve_shot(sid):
    path = screenshot_cache.get(sid)
    if not path or not os.path.exists(path):
        return Response("Screenshot not found", status=404)
    return send_file(path, mimetype="image/png")

@app.get("/admin/cleanup")
@require_admin_auth
def admin_cleanup():
    days = request.args.get("days", AUTO_CLEANUP_DAYS, type=int)
    result = cleanup_old_files(days)
    return jsonify({"cleaned": result["cleaned"], "size_mb": result["size_mb"], "days_old": days})

@app.get("/admin/storage")
@require_admin_auth
def admin_storage():
    try:
        with get_db() as conn:
            total_captures = conn.execute("SELECT COUNT(*) as count FROM captures").fetchone()['count']
            existing_pdfs = conn.execute("SELECT COUNT(*) as count FROM captures WHERE pdf_path IS NOT NULL").fetchone()['count']

            files_exist = 0
            files_missing = 0
            for row in conn.execute("SELECT pdf_path FROM captures WHERE pdf_path IS NOT NULL"):
                if row['pdf_path'] and os.path.exists(row['pdf_path']):
                    files_exist += 1
                else:
                    files_missing += 1

        # Temp storage stats
        temp_size = 0
        temp_dirs = 0
        temp_base = tempfile.gettempdir()
        for item in os.listdir(temp_base):
            if item.startswith("cw-"):
                item_path = os.path.join(temp_base, item)
                if os.path.isdir(item_path):
                    temp_dirs += 1
                    for root, dirs, files in os.walk(item_path):
                        for f in files:
                            fp = os.path.join(root, f)
                            if os.path.exists(fp):
                                temp_size += os.path.getsize(fp)

        # Persistent storage stats
        persistent_size = 0
        persistent_dirs = 0
        if STORAGE_MODE == "persistent" and os.path.exists(PERSISTENT_STORAGE_PATH):
            for item in os.listdir(PERSISTENT_STORAGE_PATH):
                if item.startswith("cw-"):
                    item_path = os.path.join(PERSISTENT_STORAGE_PATH, item)
                    if os.path.isdir(item_path):
                        persistent_dirs += 1
                        for root, dirs, files in os.walk(item_path):
                            for f in files:
                                fp = os.path.join(root, f)
                                if os.path.exists(fp):
                                    persistent_size += os.path.getsize(fp)

        temp_size_mb = temp_size / (1024 * 1024)
        persistent_size_mb = persistent_size / (1024 * 1024)
        total_size_mb = temp_size_mb + persistent_size_mb

        # Estimates
        estimated_max_captures = 90 * 3
        estimated_max_size_mb = estimated_max_captures * 5

        return jsonify({
            "storage_mode": STORAGE_MODE,
            "auto_cleanup_days": AUTO_CLEANUP_DAYS,
            "database": {
                "total_captures": total_captures,
                "pdfs_in_db": existing_pdfs,
                "files_exist": files_exist,
                "files_missing": files_missing
            },
            "temp_storage": {
                "directories": temp_dirs,
                "size_mb": round(temp_size_mb, 2)
            },
            "persistent_storage": {
                "directories": persistent_dirs,
                "size_mb": round(persistent_size_mb, 2)
            },
            "total_storage": {
                "size_mb": round(total_size_mb, 2),
                "size_gb": round(total_size_mb / 1024, 2)
            },
            "estimates": {
                "max_captures_90_days": estimated_max_captures,
                "estimated_max_size_mb": estimated_max_size_mb,
                "estimated_max_size_gb": round(estimated_max_size_mb / 1024, 2),
                "plan_limit_gb": 100,
                "estimated_usage_percent": round((estimated_max_size_mb / 1024 / 100) * 100, 2)
            }
        })
    except Exception as e:
        return jsonify({"error": str(e)}), 500

@app.get("/admin")
@require_admin_auth
def admin_dashboard():
    try:
        with get_db() as conn:
            total_captures = conn.execute("SELECT COUNT(*) as count FROM captures").fetchone()['count']
            location_stats = conn.execute("""
                SELECT location, COUNT(*) as count 
                FROM captures 
                GROUP BY location 
                ORDER BY count DESC
            """).fetchall()
            recent_captures = conn.execute("""
                SELECT id, stock, location, capture_utc, price_sha256, payment_sha256
                FROM captures
                ORDER BY created_at DESC
                LIMIT 20
            """).fetchall()
            daily_stats = conn.execute("""
                SELECT DATE(created_at) as date, COUNT(*) as count
                FROM captures
                WHERE created_at >= DATE('now', '-30 days')
                GROUP BY DATE(created_at)
                ORDER BY date DESC
            """).fetchall()

        temp_size = 0
        persistent_size = 0
        if STORAGE_MODE == "persistent" and os.path.exists(PERSISTENT_STORAGE_PATH):
            for item in os.listdir(PERSISTENT_STORAGE_PATH):
                if item.startswith("cw-"):
                    item_path = os.path.join(PERSISTENT_STORAGE_PATH, item)
                    if os.path.isdir(item_path):
                        for root, dirs, files in os.walk(item_path):
                            for f in files:
                                fp = os.path.join(root, f)
                                if os.path.exists(fp):
                                    persistent_size += os.path.getsize(fp)

        total_size_mb = (temp_size + persistent_size) / (1024 * 1024)
        estimated_max_mb = 270 * 5
        usage_percent = (total_size_mb / estimated_max_mb * 100) if estimated_max_mb > 0 else 0

        html = render_template_string("""
<!doctype html>
<html>
<head>
  <meta charset="utf-8"/>
  <title>Admin Dashboard - CW Compliance</title>
  <style>
    * { margin: 0; padding: 0; box-sizing: border-box; }
    body { font-family: 'Inter', -apple-system, sans-serif; background: #f5f7fb; color: #1d1d1f; padding: 20px; }
    .container { max-width: 1400px; margin: 0 auto; }
    header { background: linear-gradient(135deg, #003087 0%, #0055a4 100%); color: white; padding: 24px; border-radius: 12px; margin-bottom: 24px; }
    header h1 { font-size: 28px; margin-bottom: 8px; }
    header p { opacity: 0.9; font-size: 14px; }
    .stats-grid { display: grid; grid-template-columns: repeat(auto-fit, minmax(250px, 1fr)); gap: 16px; margin-bottom: 24px; }
    .stat-card { background: white; padding: 20px; border-radius: 12px; box-shadow: 0 2px 8px rgba(0,0,0,0.08); }
    .stat-card h3 { font-size: 14px; color: #6b7280; margin-bottom: 8px; font-weight: 500; }
    .stat-card .value { font-size: 32px; font-weight: 700; color: #1d1d1f; }
    .stat-card .subvalue { font-size: 13px; color: #9ca3af; margin-top: 4px; }
    .section { background: white; padding: 24px; border-radius: 12px; box-shadow: 0 2px 8px rgba(0,0,0,0.08); margin-bottom: 24px; }
    .section h2 { font-size: 20px; margin-bottom: 16px; color: #1d1d1f; }
    table { width: 100%; border-collapse: collapse; }
    th { text-align: left; padding: 12px; background: #f9fafb; font-weight: 600; font-size: 13px; color: #374151; }
    td { padding: 12px; border-bottom: 1px solid #e5e7eb; font-size: 14px; }
    tr:last-child td { border-bottom: none; }
    tr:hover { background: #f9fafb; }
    .location-badge { display: inline-block; padding: 4px 10px; background: #e0e7ff; color: #3730a3; border-radius: 6px; font-size: 12px; font-weight: 600; }
    .progress-bar { width: 100%; height: 8px; background: #e5e7eb; border-radius: 4px; overflow: hidden; margin-top: 8px; }
    .progress-fill { height: 100%; background: linear-gradient(90deg, #10b981 0%, #059669 100%); transition: width 0.3s; }
    .btn { display: inline-block; padding: 10px 20px; background: #2563eb; color: white; text-decoration: none; border-radius: 8px; font-weight: 600; font-size: 14px; border: none; cursor: pointer; }
    .btn:hover { background: #1d4ed8; }
    .btn-secondary { background: #6b7280; }
    .btn-secondary:hover { background: #4b5563; }
    .btn-danger { background: #dc2626; }
    .btn-danger:hover { background: #b91c1c; }
    .actions { display: flex; gap: 12px; margin-top: 16px; }
    .back-link { color: #2563eb; text-decoration: none; font-weight: 600; font-size: 14px; }
    .back-link:hover { text-decoration: underline; }
    code { font-family: 'Monaco', monospace; font-size: 11px; background: #f3f4f6; padding: 2px 6px; border-radius: 4px; }
  </style>
</head>
<body>
  <div class="container">
    <header>
      <h1>🔐 Admin Dashboard</h1>
      <p>CW Compliance Capture Tool - System Overview</p>
    </header>

    <div style="margin-bottom: 16px;">
      <a href="/" class="back-link">← Back to Main Site</a>
    </div>

    <div class="stats-grid">
      <div class="stat-card">
        <h3>Total Captures</h3>
        <div class="value">{{total_captures}}</div>
        <div class="subvalue">All time</div>
      </div>
      
      <div class="stat-card">
        <h3>Storage Used</h3>
        <div class="value">{{total_size_mb|round(2)}} MB</div>
        <div class="subvalue">{{usage_percent|round(1)}}% of estimated max</div>
        <div class="progress-bar">
          <div class="progress-fill" style="width: {{usage_percent if usage_percent < 100 else 100}}%"></div>
        </div>
      </div>
      
      <div class="stat-card">
        <h3>Storage Mode</h3>
        <div class="value" style="font-size: 20px;">{{storage_mode.upper()}}</div>
        <div class="subvalue">{{cleanup_days}} day retention</div>
      </div>
      
      <div class="stat-card">
        <h3>Estimated Max</h3>
        <div class="value">{{estimated_max_mb|round(0)|int}} MB</div>
        <div class="subvalue">270 captures @ 5MB each</div>
      </div>
    </div>

    <div class="section">
      <h2>📊 Captures by Location</h2>
      {% if location_stats %}
        <table>
          <thead>
            <tr>
              <th>Location</th>
              <th>Capture Count</th>
              <th>Percentage</th>
            </tr>
          </thead>
          <tbody>
            {% for loc in location_stats %}
            <tr>
              <td><span class="location-badge">{{loc.location}}</span></td>
              <td>{{loc.count}}</td>
              <td>{{(loc.count / total_captures * 100)|round(1)}}%</td>
            </tr>
            {% endfor %}
          </tbody>
        </table>
      {% else %}
        <p style="color: #6b7280;">No captures yet</p>
      {% endif %}
    </div>

    <div class="section">
      <h2>📅 Daily Activity (Last 30 Days)</h2>
      {% if daily_stats %}
        <table>
          <thead>
            <tr>
              <th>Date</th>
              <th>Captures</th>
            </tr>
          </thead>
          <tbody>
            {% for day in daily_stats[:10] %}
            <tr>
              <td>{{day.date}}</td>
              <td>{{day.count}}</td>
            </tr>
            {% endfor %}
          </tbody>
        </table>
      {% else %}
        <p style="color: #6b7280;">No recent activity</p>
      {% endif %}
    </div>

    <div class="section">
      <h2>🕐 Recent Captures</h2>
      {% if recent_captures %}
        <table>
          <thead>
            <tr>
              <th>ID</th>
              <th>Stock</th>
              <th>Location</th>
              <th>Capture Time</th>
              <th>Action</th>
            </tr>
          </thead>
          <tbody>
            {% for capture in recent_captures[:10] %}
            <tr>
              <td>{{capture.id}}</td>
              <td><strong>{{capture.stock}}</strong></td>
              <td><span class="location-badge">{{capture.location}}</span></td>
              <td>{{capture.capture_utc}}</td>
              <td>
                <a href="/view/{{capture.id}}" class="btn" style="padding: 6px 12px; font-size: 12px;">View PDF</a>
              </td>
            </tr>
            {% endfor %}
          </tbody>
        </table>
      {% else %}
        <p style="color: #6b7280;">No captures yet</p>
      {% endif %}
    </div>

    <div class="section">
      <h2>🧹 Maintenance Actions</h2>
      <p style="color: #6b7280; margin-bottom: 16px;">Manage storage and cleanup operations</p>
      <div class="actions">
        <a href="/admin/cleanup?days={{cleanup_days}}" class="btn btn-secondary">Run Cleanup Now</a>
        <a href="/admin/storage" class="btn btn-secondary" target="_blank">View Storage API</a>
        <a href="/admin/tsa-diagnostics" class="btn btn-secondary" target="_blank">TSA Diagnostics</a>
        <a href="/history" class="btn">View All Captures</a>
      </div>
    </div>

    <div class="section">
      <h2>⚙️ System Configuration</h2>
      <table>
        <tr><td><strong>Storage Mode</strong></td><td>{{storage_mode}}</td></tr>
        <tr><td><strong>Auto Cleanup Days</strong></td><td>{{cleanup_days}} days</td></tr>
        <tr><td><strong>Database Path</strong></td><td><code>{{db_path}}</code></td></tr>
        <tr><td><strong>Persistent Storage Path</strong></td><td><code>{{storage_path}}</code></td></tr>
      </table>
    </div>
  </div>
</body>
</html>
        """,
        total_captures=total_captures,
        location_stats=location_stats,
        recent_captures=recent_captures,
        daily_stats=daily_stats,
        total_size_mb=total_size_mb,
        estimated_max_mb=estimated_max_mb,
        usage_percent=usage_percent,
        storage_mode=STORAGE_MODE,
        cleanup_days=AUTO_CLEANUP_DAYS,
        db_path=DB_PATH,
        storage_path=PERSISTENT_STORAGE_PATH
        )
        return Response(html, mimetype="text/html")
    except Exception as e:
        return Response(f"Error loading dashboard: {e}", status=500)

@app.get("/admin/tsa-diagnostics")
@require_admin_auth
def tsa_diagnostics():
    results = []
    try:
        from rfc3161ng import RemoteTimestamper
        for url in TSA_URLS:
            row = {"tsa": url, "ok": False, "notes": ""}
            try:
                rt = RemoteTimestamper(url, hashname="sha256")
                tsr = None
                try:
                    tsr = rt.timestamp(data=b"diag", certreq=True)  # newer rfc3161ng
                except TypeError:
                    tsr = rt.timestamp(data=b"diag")                # older rfc3161ng
                row["ok"] = bool(tsr)
                row["notes"] = "token received" if tsr else "no token"
            except Exception as e:
                row["ok"] = False
                row["notes"] = str(e)[:200]
            results.append(row)
    except Exception as e:
        return jsonify({"error": str(e)}), 500
    return jsonify({"results": results})

@app.get("/admin/backfill-tsa/<int:capture_id>")
@require_admin_auth
def admin_backfill_tsa(capture_id):
    try:
        with get_db() as conn:
            row = conn.execute("SELECT * FROM captures WHERE id = ?", (capture_id,)).fetchone()
            if not row:
                return jsonify({"error": "capture not found"}), 404

            price_path = row['price_screenshot_path']
            pay_path   = row['payment_screenshot_path']
            price_path = price_path if (price_path and os.path.exists(price_path)) else None
            pay_path   = pay_path   if (pay_path and os.path.exists(pay_path))   else None

            if not price_path and not pay_path:
                return jsonify({"error": "no screenshots available to timestamp"}), 400

            price_tsa = row['price_tsa']
            pay_tsa   = row['payment_tsa']
            updates = {}

            # Timestamp price if needed
            rfc_price = None
            if price_path and not price_tsa:
                try:
                    rfc_price = get_rfc3161_timestamp(price_path)
                    if rfc_price:
                        updates['price_tsa'] = rfc_price['tsa']
                        updates['price_timestamp'] = rfc_price['timestamp']
                except Exception as e:
                    print(f"⚠ backfill price TSA failed: {e}")

            # Timestamp payment if needed
            rfc_pay = None
            if pay_path and not pay_tsa:
                try:
                    rfc_pay = get_rfc3161_timestamp(pay_path)
                    if rfc_pay:
                        updates['payment_tsa'] = rfc_pay['tsa']
                        updates['payment_timestamp'] = rfc_pay['timestamp']
                except Exception as e:
                    print(f"⚠ backfill payment TSA failed: {e}")

            # If nothing changed, still reconstruct RFC dicts from DB for regeneration
            if not rfc_price and row['price_tsa']:
                rfc_price = {
                    "tsa": row['price_tsa'],
                    "timestamp": row['price_timestamp'],
                    "cert_info": None
                }
            if not rfc_pay and row['payment_tsa']:
                rfc_pay = {
                    "tsa": row['payment_tsa'],
                    "timestamp": row['payment_timestamp'],
                    "cert_info": None
                }

            # Write DB updates if any
            if updates:
                set_clause = ", ".join([f"{k} = ?" for k in updates.keys()])
                vals = list(updates.values()) + [capture_id]
                conn.execute(f"UPDATE captures SET {set_clause} WHERE id = ?", vals)

            # Regenerate PDF with the (possibly) new timestamps
            pdf_path = generate_pdf(
                stock=row['stock'],
                location=row['location'],
                zip_code=row['zip_code'],
                url=row['url'],
                utc_time=row['capture_utc'],
                https_date_value=row['https_date'],
                price_path=price_path,
                pay_path=pay_path,
                sha_price=row['price_sha256'] or "N/A",
                sha_pay=row['payment_sha256'] or "N/A",
                rfc_price=rfc_price,
                rfc_pay=rfc_pay,
                debug_info=row['debug_info']
            )

            if pdf_path and os.path.exists(pdf_path):
                conn.execute("UPDATE captures SET pdf_path = ? WHERE id = ?", (pdf_path, capture_id))
                return send_file(
                    pdf_path,
                    mimetype="application/pdf",
                    as_attachment=True,
                    download_name=f"CW_Capture_{row['stock']}_{capture_id}.pdf"
                )

            return jsonify({"ok": True, "message": "timestamps updated, but PDF could not be regenerated"}), 200

    except Exception as e:
        print(f"❌ backfill error: {e}")
        traceback.print_exc()
        return jsonify({"error": str(e)}), 500

@app.get("/history")
def history():
    # Filters
    location_filter = request.args.get("location", "").strip()
    stock_filter = request.args.get("stock", "").strip()
    sort_by = request.args.get("sort", "date_desc")

    with get_db() as conn:
        query = "SELECT id, stock, location, capture_utc, price_sha256, payment_sha256 FROM captures WHERE 1=1"
        params = []

        if location_filter and location_filter.lower() != "all":
            location_name = location_filter.capitalize()
            query += " AND location = ?"
            params.append(location_name)

        if stock_filter:
            query += " AND stock LIKE ?"
            params.append(f"%{stock_filter}%")

        if sort_by == "date_asc":
            query += " ORDER BY created_at ASC"
        elif sort_by == "stock_asc":
            query += " ORDER BY stock ASC"
        elif sort_by == "stock_desc":
            query += " ORDER BY stock DESC"
        elif sort_by == "location":
            query += " ORDER BY location ASC, created_at DESC"
        else:
            query += " ORDER BY created_at DESC"

        query += " LIMIT 100"
        captures = conn.execute(query, params).fetchall()

    html = render_template_string("""
<!doctype html>
<html>
<head>
  <meta charset="utf-8"/>
  <title>Capture History</title>
  <style>
    body{font-family:Inter,Arial,sans-serif;background:#f3f4f6;margin:0;padding:24px;color:#111}
    h1{margin:0 0 16px}
    .back{display:inline-block;margin-bottom:16px;color:#2563eb;text-decoration:none;font-weight:600}
    .back:hover{text-decoration:underline}
    .filters{background:#fff;border:1px solid #e5e7eb;border-radius:8px;padding:16px;margin-bottom:20px;display:grid;grid-template-columns:repeat(auto-fit,minmax(200px,1fr));gap:12px}
    .filter-group{display:flex;flex-direction:column;gap:6px}
    .filter-group label{font-size:13px;font-weight:600;color:#374151}
    .filter-group select,.filter-group input{padding:8px 12px;border:1px solid #d1d5db;border-radius:6px;font-size:14px}
    .filter-group button{background:#2563eb;color:#fff;padding:8px 16px;border:none;border-radius:6px;cursor:pointer;font-weight:600}
    .filter-group button:hover{background:#1d4ed8}
    .stats{background:#fff;border:1px solid #e5e7eb;border-radius:8px;padding:12px 16px;margin-bottom:16px;font-size:14px;color:#6b7280}
    table{width:100%;background:#fff;border-collapse:collapse;border-radius:8px;overflow:hidden;box-shadow:0 2px 8px rgba(0,0,0,0.1)}
    th{background:#2563eb;color:#fff;padding:12px;text-align:left;font-weight:600;font-size:13px}
    td{padding:12px;border-bottom:1px solid #e5e7eb;font-size:14px}
    tr:last-child td{border-bottom:none}
    tr:hover{background:#f9fafb}
    .view-btn{background:#2563eb;color:#fff;padding:6px 12px;border-radius:4px;text-decoration:none;font-size:13px;display:inline-block}
    .view-btn:hover{background:#1d4ed8}
    .empty{text-align:center;padding:40px;color:#666;background:#fff;border-radius:8px}
    .location-badge{display:inline-block;padding:4px 8px;background:#e0e7ff;color:#3730a3;border-radius:4px;font-size:12px;font-weight:600}
    @media(max-width:768px){
      .filters{grid-template-columns:1fr}
      table{font-size:12px}
      th,td{padding:8px}
    }
  </style>
</head>
<body>
  <a href="/" class="back">← Back to Capture Tool</a>
  <h1>Capture History</h1>
  
  <form method="GET" action="/history" class="filters">
    <div class="filter-group">
      <label for="stock">Search by Stock Number</label>
      <input type="text" name="stock" id="stock" placeholder="Enter stock number..." value="{{stock_filter or ''}}">
    </div>
    
    <div class="filter-group">
      <label for="location">Filter by Location</label>
      <select name="location" id="location">
        <option value="all" {{'selected' if not location_filter or location_filter.lower() == 'all' else ''}}>All Locations</option>
        <option value="bend" {{'selected' if location_filter.lower() == 'bend' else ''}}>Bend</option>
        <option value="eugene" {{'selected' if location_filter.lower() == 'eugene' else ''}}>Eugene</option>
        <option value="hillsboro" {{'selected' if location_filter.lower() == 'hillsboro' else ''}}>Hillsboro</option>
        <option value="medford" {{'selected' if location_filter.lower() == 'medford' else ''}}>Medford</option>
        <option value="portland" {{'selected' if location_filter.lower() == 'portland' else ''}}>Portland</option>
      </select>
    </div>
    
    <div class="filter-group">
      <label for="sort">Sort By</label>
      <select name="sort" id="sort">
        <option value="date_desc" {{'selected' if sort_by == 'date_desc' else ''}}>Date (Newest First)</option>
        <option value="date_asc" {{'selected' if sort_by == 'date_asc' else ''}}>Date (Oldest First)</option>
        <option value="stock_asc" {{'selected' if sort_by == 'stock_asc' else ''}}>Stock (Low to High)</option>
        <option value="stock_desc" {{'selected' if sort_by == 'stock_desc' else ''}}>Stock (High to Low)</option>
        <option value="location" {{'selected' if sort_by == 'location' else ''}}>Location (A-Z)</option>
      </select>
    </div>
    
    <div class="filter-group" style="justify-content:flex-end;display:flex;gap:8px">
      <label>&nbsp;</label>
      <div style="display:flex;gap:8px">
        <button type="submit" style="background:#2563eb">Apply Filters</button>
        <a href="/history" style="background:#6b7280;color:#fff;padding:8px 16px;border-radius:6px;text-decoration:none;font-weight:600;display:inline-flex;align-items:center">Reset</a>
      </div>
    </div>
  </form>
  
  <div class="stats">
    Showing {{captures|length}} capture(s)
    {% if location_filter and location_filter.lower() != 'all' %} · Filtered by location: <strong>{{location_filter.capitalize()}}</strong>{% endif %}
    {% if stock_filter %} · Search: <strong>{{stock_filter}}</strong>{% endif %}
  </div>
  
  {% if captures %}
  <table>
    <thead>
      <tr>
        <th>ID</th>
        <th>Stock</th>
        <th>Location</th>
        <th>Capture Time (UTC)</th>
        <th>Price Hash</th>
        <th>Payment Hash</th>
        <th>Action</th>
      </tr>
    </thead>
    <tbody>
      {% for capture in captures %}
      <tr>
        <td>{{capture.id}}</td>
        <td><strong>{{capture.stock}}</strong></td>
        <td><span class="location-badge">{{capture.location}}</span></td>
        <td>{{capture.capture_utc}}</td>
        <td><code style="font-size:10px">{{(capture.price_sha256 or '')[:16]}}...</code></td>
        <td><code style="font-size:10px">{{(capture.payment_sha256 or '')[:16]}}...</code></td>
        <td><a href="/view/{{capture.id}}" class="view-btn">View PDF</a></td>
      </tr>
      {% endfor %}
    </tbody>
  </table>
  {% else %}
  <div class="empty">
    No captures found
    {% if location_filter or stock_filter %}
    <br><br><a href="/history" style="color:#2563eb">Clear filters</a>
    {% endif %}
  </div>
  {% endif %}
</body>
</html>
    """, captures=captures, location_filter=location_filter, stock_filter=stock_filter, sort_by=sort_by)
    return Response(html, mimetype="text/html")

@app.get("/view/<int:capture_id>")
def view_capture(capture_id):
    with get_db() as conn:
        capture = conn.execute("SELECT * FROM captures WHERE id = ?", (capture_id,)).fetchone()
    if not capture:
        return Response("Capture not found", status=404)

    # If we stored a PDF path and it exists, serve it
    if capture['pdf_path'] and os.path.exists(capture['pdf_path']):
        return send_file(
            capture['pdf_path'],
            mimetype="application/pdf",
            as_attachment=True,
            download_name=f"CW_Capture_{capture['stock']}_{capture_id}.pdf"
        )

    # Else, try to regenerate
    price_path = capture['price_screenshot_path']
    pay_path = capture['payment_screenshot_path']
    price_path = price_path if (price_path and os.path.exists(price_path)) else None
    pay_path   = pay_path   if (pay_path and os.path.exists(pay_path))   else None

    if (price_path or pay_path):
        print(f"📄 Regenerating PDF for capture {capture_id}")

        rfc_price = {'timestamp': capture['price_timestamp'], 'tsa': capture['price_tsa'], 'cert_info': None} if capture['price_tsa'] else None
        rfc_pay   = {'timestamp': capture['payment_timestamp'], 'tsa': capture['payment_tsa'], 'cert_info': None} if capture['payment_tsa'] else None

        try:
            pdf_path = generate_pdf(
                stock=capture['stock'],
                location=capture['location'],
                zip_code=capture['zip_code'],
                url=capture['url'],
                utc_time=capture['capture_utc'],
                https_date_value=capture['https_date'],
                price_path=price_path,
                pay_path=pay_path,
                sha_price=capture['price_sha256'] or "N/A",
                sha_pay=capture['payment_sha256'] or "N/A",
                rfc_price=rfc_price,
                rfc_pay=rfc_pay,
                debug_info=capture['debug_info']
            )
            if pdf_path and os.path.exists(pdf_path):
                return send_file(
                    pdf_path,
                    mimetype="application/pdf",
                    as_attachment=True,
                    download_name=f"CW_Capture_{capture['stock']}_{capture_id}.pdf"
                )
        except Exception as e:
            print(f"❌ PDF regeneration failed: {e}")
            traceback.print_exc()

    return Response("PDF and screenshots no longer available. Data has been cleaned up.", status=404)

@app.post("/capture")
def capture():
    try:
        stock = (request.form.get("stock") or "").strip()
        location = (request.form.get("location") or "portland").strip().lower()

        if not re.match(r"^[a-zA-Z0-9]+$", stock):
            return Response("Invalid stock number. Please use letters and numbers only.", status=400)
        if location not in CW_LOCATIONS:
            return Response("Invalid location", status=400)

        loc_info = CW_LOCATIONS[location]
        zip_code = loc_info["zip"]
        location_name = loc_info["name"]
        latitude = loc_info["lat"]
        longitude = loc_info["lon"]

        price_path, pay_path, final_url, debug_info = do_capture(stock, zip_code, location_name, latitude, longitude)

        utc_now = datetime.datetime.utcnow().strftime("%Y-%m-%d %H:%M:%S UTC")
        hdate = https_date()

        price_ok = bool(price_path and os.path.exists(price_path))
        pay_ok   = bool(pay_path   and os.path.exists(pay_path))

        sha_price = sha256_file(price_path) if price_ok else "N/A"
        sha_pay   = sha256_file(pay_path)   if pay_ok   else "N/A"

        rfc_price = None
        rfc_pay = None
        try:
            rfc_price = get_rfc3161_timestamp(price_path) if price_ok else None
        except Exception as e:
            print(f"⚠ RFC 3161 timestamp failed for price: {e}")
        try:
            rfc_pay = get_rfc3161_timestamp(pay_path) if pay_ok else None
        except Exception as e:
            print(f"⚠ RFC 3161 timestamp failed for payment: {e}")

        # Optional: add quick flags to debug_info (helps on error page)
        if rfc_price:
            debug_info += f"\nRFC3161_PRICE_OK={rfc_price.get('timestamp')}"
        else:
            debug_info += "\nRFC3161_PRICE_OK=FALSE"
        if rfc_pay:
            debug_info += f"\nRFC3161_PAY_OK={rfc_pay.get('timestamp')}"
        else:
            debug_info += "\nRFC3161_PAY_OK=FALSE"

        pdf_path = None
        if price_ok or pay_ok:
            try:
                pdf_path = generate_pdf(
                    stock=stock,
                    location=location_name,
                    zip_code=zip_code,
                    url=final_url,
                    utc_time=utc_now,
                    https_date_value=hdate,
                    price_path=price_path if price_ok else None,
                    pay_path=pay_path if pay_ok else None,
                    sha_price=sha_price,
                    sha_pay=sha_pay,
                    rfc_price=rfc_price,
                    rfc_pay=rfc_pay,
                    debug_info=debug_info
                )
            except Exception as e:
                print(f"❌ PDF generation error: {e}")
                traceback.print_exc()
                pdf_path = None

        capture_id = None
        try:
            with get_db() as conn:
                cursor = conn.execute("""
                    INSERT INTO captures (
                        stock, location, zip_code, url, capture_utc, https_date,
                        price_sha256, payment_sha256, price_screenshot_path, payment_screenshot_path,
                        price_tsa, price_timestamp, payment_tsa, payment_timestamp,
                        pdf_path, debug_info
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """, (
                    stock, location_name, zip_code, final_url, utc_now, hdate,
                    sha_price, sha_pay, price_path, pay_path,
                    rfc_price['tsa'] if rfc_price else None,
                    rfc_price['timestamp'] if rfc_price else None,
                    rfc_pay['tsa'] if rfc_pay else None,
                    rfc_pay['timestamp'] if rfc_pay else None,
                    pdf_path, debug_info
                ))
                capture_id = cursor.lastrowid
                print(f"✓ Saved to database with ID: {capture_id}")
        except Exception as e:
            print(f"⚠ Database save failed: {e}")
            traceback.print_exc()

        if pdf_path and os.path.exists(pdf_path):
            return send_file(
                pdf_path,
                mimetype="application/pdf",
                as_attachment=True,
                download_name=f"CW_Capture_{stock}_{capture_id or 'temp'}.pdf"
            )
        else:
            error_html = f"""
<!doctype html>
<html>
<head><title>PDF Generation Failed</title>
<style>body{{font-family:sans-serif;padding:40px;background:#f3f4f6}}
.error{{background:#fff;padding:20px;border-radius:8px;max-width:900px;margin:0 auto}}
h1{{color:#dc2626}}pre{{background:#f9fafb;padding:12px;border-radius:4px;overflow:auto}}
a{{color:#2563eb}}</style>
</head>
<body>
<div class="error">
<h1>PDF Generation Failed</h1>
<p>Screenshots may not have been captured, or PDF generation failed.</p>
<p><strong>Stock:</strong> {stock} | <strong>Location:</strong> {location_name}</p>
<p><strong>Price OK:</strong> {price_ok} | <strong>Payment OK:</strong> {pay_ok}</p>
<h3>Debug Information:</h3>
<pre>{debug_info}</pre>
<p><a href="/">← Back to Home</a></p>
</div>
</body>
</html>
            """
            return Response(error_html, mimetype="text/html", status=500)

    except Exception as e:
        print("❌ /capture failed:", e, file=sys.stderr)
        traceback.print_exc()
        return Response(f"Error: {e}", status=500)

# -------------------- Entrypoint --------------------

if __name__ == "__main__":
    app.run(host="0.0.0.0", port=PORT, debug=False)
