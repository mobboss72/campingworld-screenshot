# server.py
import os, sys, hashlib, datetime, tempfile, traceback, requests, time, re
from flask import Flask, request, send_from_directory, Response, render_template_string, send_file, jsonify
from playwright.sync_api import sync_playwright
from reportlab.lib.units import inch
from PIL import Image as PILImage
import sqlite3
from contextlib import contextmanager
from functools import wraps
import threading

# -------------------- Railway-friendly defaults & config --------------------

ADMIN_PASSWORD = os.getenv("ADMIN_PASSWORD", "cwadmin2025")  # Change this!

# Detect Railway and pick writable defaults
IS_RAILWAY = bool(os.getenv("RAILWAY_ENVIRONMENT") or os.getenv("RAILWAY_PROJECT_ID") or os.getenv("RAILWAY_STATIC_URL"))
DEFAULT_DATA_DIR = os.getenv("DATA_DIR", "/data" if IS_RAILWAY else tempfile.gettempdir())

# Persist Playwright downloads and app data under a writable dir
os.environ.setdefault("PLAYWRIGHT_BROWSERS_PATH", os.path.join(DEFAULT_DATA_DIR, "ms-playwright"))

PORT = int(os.getenv("PORT", "8080"))
DB_PATH = os.getenv("DB_PATH", os.path.join(DEFAULT_DATA_DIR, "captures.db"))

STORAGE_MODE = os.getenv("STORAGE_MODE", "persistent")  # persistent or temp
PERSISTENT_STORAGE_PATH = os.getenv("PERSISTENT_STORAGE_PATH", os.path.join(DEFAULT_DATA_DIR, "captures"))
AUTO_CLEANUP_DAYS = int(os.getenv("AUTO_CLEANUP_DAYS", "90"))

# Ensure dirs exist
os.makedirs(os.path.dirname(DB_PATH), exist_ok=True)
os.makedirs(PERSISTENT_STORAGE_PATH, exist_ok=True)
os.makedirs(os.environ["PLAYWRIGHT_BROWSERS_PATH"], exist_ok=True)

# Oregon Camping World locations (alphabetical)
CW_LOCATIONS = {
    "bend": {"name": "Bend", "zip": "97701", "lat": 44.0582, "lon": -121.3153},
    "eugene": {"name": "Eugene", "zip": "97402", "lat": 44.0521, "lon": -123.0868},
    "hillsboro": {"name": "Hillsboro", "zip": "97124", "lat": 45.5229, "lon": -122.9898},
    "medford": {"name": "Medford", "zip": "97504", "lat": 42.3265, "lon": -122.8756},
    "portland": {"name": "Portland", "zip": "97201", "lat": 45.5152, "lon": -122.6784},
}

# TSA list
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

# -------------------- Cleanup Scheduler --------------------

def cleanup_old_files(days_old=90):
    try:
        cutoff_time = time.time() - (days_old * 24 * 60 * 60)
        cleaned_count, cleaned_size = 0, 0

        # temp dirs
        temp_base = tempfile.gettempdir()
        for item in os.listdir(temp_base):
            if item.startswith("cw-"):
                p = os.path.join(temp_base, item)
                try:
                    if os.path.isdir(p) and os.path.getmtime(p) < cutoff_time:
                        for root, _, files in os.walk(p):
                            for f in files:
                                fp = os.path.join(root, f)
                                if os.path.exists(fp):
                                    cleaned_size += os.path.getsize(fp)
                        import shutil; shutil.rmtree(p)
                        cleaned_count += 1
                        print(f"🧹 Cleaned old temp dir: {item}")
                except Exception as e:
                    print(f"⚠ Could not clean {p}: {e}")

        # persistent dirs
        if STORAGE_MODE == "persistent" and os.path.exists(PERSISTENT_STORAGE_PATH):
            for item in os.listdir(PERSISTENT_STORAGE_PATH):
                if item.startswith("cw-"):
                    p = os.path.join(PERSISTENT_STORAGE_PATH, item)
                    try:
                        if os.path.isdir(p) and os.path.getmtime(p) < cutoff_time:
                            for root, _, files in os.walk(p):
                                for f in files:
                                    fp = os.path.join(root, f)
                                    if os.path.exists(fp):
                                        cleaned_size += os.path.getsize(fp)
                            import shutil; shutil.rmtree(p)
                            cleaned_count += 1
                            print(f"🧹 Cleaned old persistent dir: {item}")
                    except Exception as e:
                        print(f"⚠ Could not clean {p}: {e}")

        cleaned_size_mb = cleaned_size / (1024 * 1024)
        print(f"✓ Cleanup complete: removed {cleaned_count} dirs ({cleaned_size_mb:.2f} MB)")
        return {"cleaned": cleaned_count, "size_mb": round(cleaned_size_mb, 2)}
    except Exception as e:
        print(f"❌ Cleanup failed: {e}")
        return {"cleaned": 0, "size_mb": 0}

def schedule_cleanup():
    def task():
        while True:
            time.sleep(24 * 60 * 60)
            print("🕐 Running scheduled cleanup...")
            try:
                result = cleanup_old_files(AUTO_CLEANUP_DAYS)
                print(f"✓ Scheduled cleanup: {result['cleaned']} dirs, {result['size_mb']} MB")
            except Exception as e:
                print(f"❌ Scheduled cleanup failed: {e}")
    t = threading.Thread(target=task, daemon=True)
    t.start()
    print(f"✓ Automatic cleanup scheduled (every 24h, retention {AUTO_CLEANUP_DAYS}d)")

schedule_cleanup()

# -------------------- Auth --------------------

def check_admin_auth():
    from flask import session
    if session.get('admin_authenticated'):
        return True
    auth = request.authorization
    if auth and auth.password == ADMIN_PASSWORD:
        return True
    return False

def require_admin_auth(f):
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
    """Try multiple TSAs; return dict {timestamp, tsa, token_file, cert_info} or None."""
    if not file_path or not os.path.exists(file_path):
        return None

    print(f"🕐 Getting RFC 3161 timestamp for {os.path.basename(file_path)}...")

    with open(file_path, "rb") as f:
        file_bytes = f.read()
    digest = hashlib.sha256(file_bytes).digest()

    try:
        from rfc3161ng import RemoteTimestamper, decode_timestamp_response
    except ImportError as e:
        print(f"\n{'='*60}")
        print(f"❌ CRITICAL: rfc3161ng library not installed!")
        print(f"{'='*60}")
        print(f"Error: {e}")
        print(f"\nTo fix this, run:")
        print(f"  pip install rfc3161ng --break-system-packages")
        print(f"\nOr if using a virtual environment:")
        print(f"  pip install rfc3161ng")
        print(f"{'='*60}\n")
        return None

    for tsa_url in TSA_URLS:
        try:
            print(f"  Trying TSA: {tsa_url}")
            rt = RemoteTimestamper(tsa_url, hashname="sha256")

            tsr = None
            # Prefer sending file bytes
            try:
                try:
                    tsr = rt.timestamp(data=file_bytes, certreq=True)
                except TypeError:
                    tsr = rt.timestamp(data=file_bytes)
            except Exception as e1:
                print(f"    data= failed ({e1}); retrying with data_hash...")

            if not tsr:
                try:
                    try:
                        tsr = rt.timestamp(data_hash=digest, certreq=True)
                    except TypeError:
                        tsr = rt.timestamp(data_hash=digest)
                except Exception as e2:
                    print(f"    data_hash= failed ({e2})")

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

# -------------------- PDF (single page, dynamic footer) --------------------

def generate_pdf(
    stock, location, zip_code, url, utc_time, https_date_value,
    price_path, pay_path, sha_price, sha_pay,
    rfc_price, rfc_pay, debug_info
):
    """Single-page LETTER PDF with stacked screenshots and RFC-3161 footer that always fits."""
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

        # Allow runtime debug boxes via env or query param (set ?debug=1 before calling this)
        debug_boxes = os.getenv("PDF_DEBUG_BOXES", "0") == "1"

        price_ok = bool(price_path and os.path.exists(price_path))
        pay_ok   = bool(pay_path and os.path.exists(pay_path))
        is_used  = bool(debug_info and "Unit type: Used" in debug_info)

        imgs, dims = [], []
        if price_ok:
            im1 = PILImage.open(price_path)
            imgs.append(("Price Disclosure", price_path, im1))
            dims.append((im1.width, im1.height))
        if pay_ok:
            im2 = PILImage.open(pay_path)
            imgs.append(("Payment Disclosure", pay_path, im2))
            dims.append((im2.width, im2.height))

        # Output path
        if price_ok:
            tmpdir = os.path.dirname(price_path)
        elif pay_ok:
            tmpdir = os.path.dirname(pay_path)
        else:
            tmpdir = tempfile.mkdtemp(prefix=f"cw-{stock}-")

        pdf_path = os.path.join(tmpdir, f"cw_{stock}_report.pdf")
        c = pdfcanvas.Canvas(pdf_path, pagesize=letter)

        # Helpers
        def draw_wrapped_line(text, x, y, max_width, font="Helvetica", size=8, leading=11):
            c.setFont(font, size)
            words = (text or "").split()
            line, used_y = "", 0
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

        def measure_wrapped_height(text, max_width, font="Helvetica", size=8, leading=11):
            c.setFont(font, size)
            words = (text or "").split()
            line, lines = "", 0
            for w in words:
                test = w if not line else (line + " " + w)
                if c.stringWidth(test, font, size) <= max_width:
                    line = test
                else:
                    lines += 1
                    line = w
            if line:
                lines += 1
            return lines * leading

        def measure_hash_height():
            h = 0
            # Heading
            h += 0.16 * inch
            max_text_width = page_w - 2 * margin
            if sha_price and sha_price != "N/A":
                h += measure_wrapped_height(f"Price Disclosure: {sha_price}", max_text_width, font="Courier", size=7, leading=9)
            if sha_pay and sha_pay != "N/A":
                h += measure_wrapped_height(f"Payment Disclosure: {sha_pay}", max_text_width, font="Courier", size=7, leading=9)
            h += 0.04 * inch
            return h

        def measure_rfc_height():
            h = 0
            # Heading
            h += 0.16 * inch
            max_text_width = page_w - 2 * margin

            def block_height(label, data):
                if not data:
                    return 0.14 * inch
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
                total = 0
                for ln in lines:
                    total += measure_wrapped_height(ln, max_text_width, font="Helvetica", size=8, leading=11)
                return total + 2  # micro spacer

            h += block_height("Price", rfc_price)
            h += block_height("Payment", rfc_pay)
            return h

        # === Page header/meta ===
        y = page_h - margin
        c.setFont("Helvetica-Bold", 14)
        c.drawString(margin, y, "Camping World Compliance Capture Report")
        y -= title_h

        c.setFont("Helvetica", 8)
        max_text_width = page_w - 2 * margin
        meta_lines = [
            f"Stock: {stock}",
            f"Location: {location} (ZIP: {zip_code})",
            f"URL: {url or 'N/A'}",
            f"Capture Time (UTC): {utc_time}",
            f"HTTPS Date: {https_date_value or 'N/A'}",
        ]
        for line in meta_lines:
            if line.startswith("URL: "):
                used_h = draw_wrapped_line(line, margin, y, max_text_width, size=8, leading=11)
                y -= used_h
            else:
                c.drawString(margin, y, line)
                y -= meta_line_h

        y -= gap_small
        if is_used and not price_ok:
            c.setFont("Helvetica-Bold", 9)
            c.drawString(margin, y, "Used RV selected — no pricing breakdown needed.")
            y -= (gap_small + 0.05 * inch)

        # === Footer sizing ===
        hashes_h = measure_hash_height()
        rfc_h    = measure_rfc_height()
        footer_needed = hashes_h + rfc_h + 0.15 * inch  # safety buffer

        # Space left for images after guaranteeing the footer
        available_for_imgs = max(0, (y - margin) - footer_needed)

        if debug_boxes:
            # visual guides
            c.setStrokeGray(0.7); c.setLineWidth(0.5)
            c.rect(margin, margin, page_w - 2*margin, footer_needed, stroke=1, fill=0)  # footer area
            c.setStrokeGray(0.85)
            c.rect(margin, margin + footer_needed, page_w - 2*margin, available_for_imgs, stroke=1, fill=0)  # image area

        # === Images ===
        if imgs and available_for_imgs > 0:
            max_img_w = page_w - 2 * margin
            scaled = []
            for (label, path, im), (w, h) in zip(imgs, dims):
                if w == 0 or h == 0:
                    scaled.append((label, path, 0, 0)); continue
                s = max_img_w / float(w)
                scaled.append((label, path, w * s, h * s))

            total_h = sum(h for _, _, _, h in scaled) + (len(scaled) - 1) * gap_img + len(scaled) * 0.13 * inch
            if total_h > available_for_imgs and total_h > 0:
                shrink = available_for_imgs / total_h
                scaled = [(label, path, w * shrink, h * shrink) for (label, path, w, h) in scaled]

            y_top_images = margin + footer_needed + available_for_imgs
            y = y_top_images
            for idx, (label, path, dw, dh) in enumerate(scaled):
                if dw <= 0 or dh <= 0: continue
                c.setFont("Helvetica", 8)
                c.drawString(margin, y - 0.13 * inch, label)
                y -= 0.13 * inch
                c.drawImage(path, margin, y - dh, width=dw, height=dh, preserveAspectRatio=True, mask='auto')
                y -= dh
                if idx < len(scaled) - 1:
                    y -= gap_img
        else:
            # No screenshots; just leave the footer space
            pass

        # === Footer positioning: Build from BOTTOM UP ===
        # RFC section: starts at bottom margin, goes up
        rfc_start_y = margin + rfc_h  # Start at bottom + height = top of RFC section
        
        # SHA section: starts above RFC section
        sha_start_y = rfc_start_y + hashes_h  # Start at top of RFC + SHA height
        
        # Footer ends at top of SHA section
        footer_top = sha_start_y

        # === Draw SHA-256 section (from sha_start_y, drawing downward) ===
        y = sha_start_y
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

        # === Draw RFC-3161 section (from rfc_start_y, drawing downward) ===
        y = rfc_start_y
        c.setFont("Helvetica-Bold", 10)
        c.drawString(margin, y, "RFC-3161 Timestamps")
        y -= 0.16 * inch
        c.setFont("Helvetica", 8)

        def draw_rfc(label, data, y):
            if not data or not data.get('timestamp') or data.get('timestamp') == 'N/A':
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
        # We should now be above or right at the margin line.

        c.showPage()
        c.save()
        print(f"✓ PDF generated (one page, dynamic footer): {pdf_path} ({os.path.getsize(pdf_path)} bytes)")
        return pdf_path

    except Exception as e:
        print(f"❌ PDF generation failed: {e}")
        traceback.print_exc()
        return None

# -------------------- Tooltip Helper --------------------

def find_and_trigger_tooltip(page, label_text, tooltip_name):
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
                if not label.is_visible(timeout=1000):
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
                                    ['mouseenter','mouseover','mousemove','click'].forEach(t => {{
                                        svg.dispatchEvent(new MouseEvent(t, {{bubbles:true,cancelable:true,view:window}}));
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
                    "--disable-blink-features=AutomationControlled",
                    "--disable-gpu",
                    "--no-zygote",
                    "--single-process",
                    "--js-flags=--max-old-space-size=128",
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

            final_url = page.url
            all_debug.append(f"✓ Final URL captured: {final_url}")

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
                success, dbg = find_and_trigger_tooltip(page, "Total Price", "price")
                all_debug.append(dbg)
                if success:
                    try:
                        page.screenshot(path=price_png, full_page=True)
                        size = os.path.getsize(price_png)
                        all_debug.append(f"✓ Price screenshot saved: {size} bytes")
                        price_png_path = price_png
                    except Exception as e:
                        all_debug.append(f"❌ Price screenshot failed: {e}")
            else:
                all_debug.append("\n--- Skipping Price Tooltip (Used Unit) ---")

            page.wait_for_timeout(1000)

            # Payment tooltip (both new/used)
            all_debug.append("\n--- Capturing Payment Tooltip ---")
            success, dbg = find_and_trigger_tooltip(page, "Est. Payment", "payment")
            all_debug.append(dbg)
            if success:
                try:
                    page.screenshot(path=pay_png, full_page=True)
                    size = os.path.getsize(pay_png)
                    all_debug.append(f"✓ Payment screenshot saved: {size} bytes")
                    pay_png_path = pay_png
                except Exception as e:
                    all_debug.append(f"❌ Payment screenshot failed: {e}")

            # Not found fallback
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
                        price_png_path = not_found_png
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

    return price_png_path, pay_png_path, final_url, "\n".join(all_debug)

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

# Health/diagnostics (Railway)
@app.get("/healthz")
def healthz():
    try:
        test_dir = os.path.join(PERSISTENT_STORAGE_PATH, "_health")
        os.makedirs(test_dir, exist_ok=True)
        with open(os.path.join(test_dir, "touch.txt"), "w") as f:
            f.write(str(time.time()))
        with get_db() as conn:
            conn.execute("CREATE TABLE IF NOT EXISTS _healthcheck (ts TEXT)")
            conn.execute("INSERT INTO _healthcheck (ts) VALUES (?)", (datetime.datetime.utcnow().isoformat()+"Z",))
        return jsonify({
            "ok": True,
            "db_path": DB_PATH,
            "storage_path": PERSISTENT_STORAGE_PATH,
            "browsers_path": os.environ.get("PLAYWRIGHT_BROWSERS_PATH"),
        }), 200
    except Exception as e:
        return jsonify({"ok": False, "error": str(e)}), 500

@app.get("/admin/diag")
@require_admin_auth
def admin_diag():
    out = {"env": {}, "fs": {}, "db_ok": False, "playwright_ok": False, "notes": []}
    try:
        out["env"] = {
            "PORT": os.getenv("PORT"),
            "DB_PATH": DB_PATH,
            "PERSISTENT_STORAGE_PATH": PERSISTENT_STORAGE_PATH,
            "PLAYWRIGHT_BROWSERS_PATH": os.environ.get("PLAYWRIGHT_BROWSERS_PATH"),
            "STORAGE_MODE": STORAGE_MODE,
            "IS_RAILWAY": IS_RAILWAY,
        }
        test_dir = os.path.join(PERSISTENT_STORAGE_PATH, "_diag")
        os.makedirs(test_dir, exist_ok=True)
        fp = os.path.join(test_dir, "write.txt")
        with open(fp, "w") as f: f.write("ok")
        out["fs"]["write_ok"] = True

        with get_db() as conn:
            conn.execute("CREATE TABLE IF NOT EXISTS _diag (k TEXT, v TEXT)")
            conn.execute("INSERT INTO _diag (k, v) VALUES (?, ?)", ("ts", datetime.datetime.utcnow().isoformat()+"Z"))
            out["db_ok"] = True

        try:
            with sync_playwright() as p:
                b = p.chromium.launch(headless=True, args=[
                    "--no-sandbox","--disable-setuid-sandbox","--disable-dev-shm-usage"
                ])
                b.close()
            out["playwright_ok"] = True
        except Exception as e:
            out["notes"].append(f"playwright: {e}")

        return jsonify(out), 200
    except Exception as e:
        out["error"] = str(e)
        return jsonify(out), 500

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

        temp_size = 0
        temp_dirs = 0
        temp_base = tempfile.gettempdir()
        for item in os.listdir(temp_base):
            if item.startswith("cw-"):
                ip = os.path.join(temp_base, item)
                if os.path.isdir(ip):
                    temp_dirs += 1
                    for root, _, files in os.walk(ip):
                        for f in files:
                            fp = os.path.join(root, f)
                            if os.path.exists(fp):
                                temp_size += os.path.getsize(fp)

        persistent_size = 0
        persistent_dirs = 0
        if STORAGE_MODE == "persistent" and os.path.exists(PERSISTENT_STORAGE_PATH):
            for item in os.listdir(PERSISTENT_STORAGE_PATH):
                if item.startswith("cw-"):
                    ip = os.path.join(PERSISTENT_STORAGE_PATH, item)
                    if os.path.isdir(ip):
                        persistent_dirs += 1
                        for root, _, files in os.walk(ip):
                            for f in files:
                                fp = os.path.join(root, f)
                                if os.path.exists(fp):
                                    persistent_size += os.path.getsize(fp)

        temp_size_mb = temp_size / (1024 * 1024)
        persistent_size_mb = persistent_size / (1024 * 1024)
        total_size_mb = temp_size_mb + persistent_size_mb

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
                    ip = os.path.join(PERSISTENT_STORAGE_PATH, item)
                    if os.path.isdir(ip):
                        for root, _, files in os.walk(ip):
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
          <thead><tr><th>Location</th><th>Capture Count</th><th>Percentage</th></tr></thead>
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
          <thead><tr><th>Date</th><th>Captures</th></tr></thead>
          <tbody>
            {% for day in daily_stats[:10] %}
            <tr><td>{{day.date}}</td><td>{{day.count}}</td></tr>
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
          <thead><tr><th>ID</th><th>Stock</th><th>Location</th><th>Capture Time</th><th>Action</th></tr></thead>
          <tbody>
            {% for capture in recent_captures[:10] %}
            <tr>
              <td>{{capture.id}}</td>
              <td><strong>{{capture.stock}}</strong></td>
              <td><span class="location-badge">{{capture.location}}</span></td>
              <td>{{capture.capture_utc}}</td>
              <td>
                <a href="/view/{{capture.id}}?force=1" class="btn" style="padding: 6px 12px; font-size: 12px;">Force PDF</a>
                <a href="/view/{{capture.id}}?force=1&restamp=1" class="btn btn-secondary" style="padding: 6px 12px; font-size: 12px; margin-left:8px;">Restamp + PDF</a>
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
                    tsr = rt.timestamp(data=b"diag", certreq=True)
                except TypeError:
                    tsr = rt.timestamp(data=b"diag")
                row["ok"] = bool(tsr)
                row["notes"] = "token received" if tsr else "no token"
            except Exception as e:
                row["ok"] = False
                row["notes"] = str(e)[:200]
            results.append(row)
    except Exception as e:
        return jsonify({"error": str(e)}), 500
    return jsonify({"results": results})

@app.get("/admin/capture/<int:capture_id>")
@require_admin_auth
def admin_capture_details(capture_id):
    with get_db() as conn:
        row = conn.execute("SELECT * FROM captures WHERE id = ?", (capture_id,)).fetchone()
        if not row:
            return jsonify({"error": "capture not found"}), 404
        details = dict(row)
        price_path = details.get("price_screenshot_path")
        pay_path   = details.get("payment_screenshot_path")
        details["price_file_exists"] = bool(price_path and os.path.exists(price_path))
        details["payment_file_exists"] = bool(pay_path and os.path.exists(pay_path))
        details["pdf_exists"] = bool(details.get("pdf_path") and os.path.exists(details["pdf_path"]))
        return jsonify(details)

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

            rfc_price = None
            if price_path and not price_tsa:
                try:
                    rfc_price = get_rfc3161_timestamp(price_path)
                    if rfc_price:
                        updates['price_tsa'] = rfc_price['tsa']
                        updates['price_timestamp'] = rfc_price['timestamp']
                except Exception as e:
                    print(f"⚠ backfill price TSA failed: {e}")

            rfc_pay = None
            if pay_path and not pay_tsa:
                try:
                    rfc_pay = get_rfc3161_timestamp(pay_path)
                    if rfc_pay:
                        updates['payment_tsa'] = rfc_pay['tsa']
                        updates['payment_timestamp'] = rfc_pay['timestamp']
                except Exception as e:
                    print(f"⚠ backfill payment TSA failed: {e}")

            if not rfc_price and row['price_tsa']:
                rfc_price = {"tsa": row['price_tsa'], "timestamp": row['price_timestamp'], "cert_info": None}
            if not rfc_pay and row['payment_tsa']:
                rfc_pay = {"tsa": row['payment_tsa'], "timestamp": row['payment_timestamp'], "cert_info": None}

            if updates:
                set_clause = ", ".join([f"{k} = ?" for k in updates.keys()])
                vals = list(updates.values()) + [capture_id]
                conn.execute(f"UPDATE captures SET {set_clause} WHERE id = ?", vals)

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
        <td>
          <a href="/view/{{capture.id}}?force=1" class="view-btn">Force PDF</a>
          &nbsp;|&nbsp;
          <a href="/view/{{capture.id}}?force=1&restamp=1" class="view-btn">Re-stamp + PDF</a>
        </td>
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
    force = request.args.get("force") == "1"
    restamp = request.args.get("restamp") == "1"

    with get_db() as conn:
        capture = conn.execute("SELECT * FROM captures WHERE id = ?", (capture_id,)).fetchone()
    if not capture:
        return Response("Capture not found", status=404)

    # Serve cached PDF if present and not forcing
    if capture['pdf_path'] and os.path.exists(capture['pdf_path']) and not force:
        return send_file(
            capture['pdf_path'],
            mimetype="application/pdf",
            as_attachment=True,
            download_name=f"CW_Capture_{capture['stock']}_{capture_id}.pdf"
        )

    # Rebuild (optionally re-stamp)
    price_path = capture['price_screenshot_path']
    pay_path   = capture['payment_screenshot_path']
    price_path = price_path if (price_path and os.path.exists(price_path)) else None
    pay_path   = pay_path   if (pay_path and os.path.exists(pay_path))   else None

    rfc_price = {'timestamp': capture['price_timestamp'], 'tsa': capture['price_tsa'], 'cert_info': None} if (capture['price_tsa'] and capture['price_timestamp']) else None
    rfc_pay   = {'timestamp': capture['payment_timestamp'], 'tsa': capture['payment_tsa'], 'cert_info': None} if (capture['payment_tsa'] and capture['payment_timestamp']) else None

    # --- AUTO-STAMP SAFETY NET: If missing OR explicitly requested via restamp, stamp now (when file exists) ---
    updated = {}
    try:
        if (restamp or not rfc_price) and price_path:
            new_price = get_rfc3161_timestamp(price_path)
            if new_price:
                rfc_price = new_price
                updated["price_tsa"] = new_price["tsa"]
                updated["price_timestamp"] = new_price["timestamp"]
    except Exception as e:
        print(f"⚠ auto-stamp price failed: {e}")

    try:
        if (restamp or not rfc_pay) and pay_path:
            new_pay = get_rfc3161_timestamp(pay_path)
            if new_pay:
                rfc_pay = new_pay
                updated["payment_tsa"] = new_pay["tsa"]
                updated["payment_timestamp"] = new_pay["timestamp"]
    except Exception as e:
        print(f"⚠ auto-stamp payment failed: {e}")

    if updated:
        with get_db() as conn:
            set_clause = ", ".join([f"{k} = ?" for k in updated.keys()])
            vals = list(updated.values()) + [capture_id]
            conn.execute(f"UPDATE captures SET {set_clause} WHERE id = ?", vals)
    # --- end auto-stamp safety net ---

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
            with get_db() as conn:
                conn.execute("UPDATE captures SET pdf_path = ? WHERE id = ?", (pdf_path, capture_id))
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

        loc = CW_LOCATIONS[location]
        zip_code = loc["zip"]
        location_name = loc["name"]
        latitude = loc["lat"]
        longitude = loc["lon"]

        price_path, pay_path, final_url, debug_info = do_capture(stock, zip_code, location_name, latitude, longitude)

        utc_now = datetime.datetime.utcnow().strftime("%Y-%m-%d %H:%M:%S UTC")
        hdate = https_date()

        price_ok = bool(price_path and os.path.exists(price_path))
        pay_ok   = bool(pay_path   and os.path.exists(pay_path))

        sha_price = sha256_file(price_path) if price_ok else "N/A"
        sha_pay   = sha256_file(pay_path)   if pay_ok   else "N/A"

        rfc_price = None
        rfc_pay = None
        
        print("\n" + "="*60)
        print("RFC-3161 TIMESTAMP GENERATION")
        print("="*60)
        
        try:
            if price_ok:
                print(f"🕐 Attempting RFC-3161 timestamp for PRICE screenshot...")
                print(f"   File: {price_path}")
                print(f"   Size: {os.path.getsize(price_path)} bytes")
                rfc_price = get_rfc3161_timestamp(price_path)
                if rfc_price:
                    print(f"✓ SUCCESS - Price timestamp: {rfc_price.get('timestamp')}")
                    print(f"   TSA: {rfc_price.get('tsa')}")
                else:
                    print(f"✗ FAILED - No timestamp obtained for price (all TSAs failed)")
        except Exception as e:
            print(f"❌ EXCEPTION - RFC 3161 timestamp failed for price: {e}")
            traceback.print_exc()
            
        try:
            if pay_ok:
                print(f"\n🕐 Attempting RFC-3161 timestamp for PAYMENT screenshot...")
                print(f"   File: {pay_path}")
                print(f"   Size: {os.path.getsize(pay_path)} bytes")
                rfc_pay = get_rfc3161_timestamp(pay_path)
                if rfc_pay:
                    print(f"✓ SUCCESS - Payment timestamp: {rfc_pay.get('timestamp')}")
                    print(f"   TSA: {rfc_pay.get('tsa')}")
                else:
                    print(f"✗ FAILED - No timestamp obtained for payment (all TSAs failed)")
        except Exception as e:
            print(f"❌ EXCEPTION - RFC 3161 timestamp failed for payment: {e}")
            traceback.print_exc()
            
        print("="*60 + "\n")

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
