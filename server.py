# server.py
import os, sys, hashlib, datetime, tempfile, traceback, requests, time, base64, io
from flask import Flask, request, send_from_directory, Response, render_template_string, send_file, jsonify
from playwright.sync_api import sync_playwright, TimeoutError as PlaywrightTimeout
from rfc3161ng import RemoteTimestamper, get_hash_oid
from reportlab.lib.pagesizes import letter
from reportlab.lib import colors
from reportlab.lib.styles import getSampleStyleSheet, ParagraphStyle
from reportlab.lib.units import inch
from reportlab.platypus import SimpleDocTemplate, Paragraph, Spacer, Image, Table, TableStyle, PageBreak, KeepTogether
from reportlab.lib.enums import TA_LEFT, TA_CENTER
from PIL import Image as PILImage
import sqlite3
from contextlib import contextmanager
from functools import wraps
import threading

# Admin password
ADMIN_PASSWORD = os.getenv("ADMIN_PASSWORD", "cwadmin2025")  # Change this!

# Persist Playwright downloads
os.environ.setdefault("PLAYWRIGHT_BROWSERS_PATH", "/app/ms-playwright")

PORT = int(os.getenv("PORT", "8080"))
DB_PATH = os.getenv("DB_PATH", "/app/data/captures.db")

# Storage configuration
STORAGE_MODE = os.getenv("STORAGE_MODE", "persistent")  # Changed to persistent
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

# RFC 3161 Timestamp Authority URLs
TSA_URLS = [
    "http://timestamp.digicert.com",
    "http://timestamp.apple.com/ts01",
    "http://tsa.starfieldtech.com",
    "http://rfc3161timestamp.globalsign.com/advanced",
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
        conn.execute("""
            CREATE INDEX IF NOT EXISTS idx_stock ON captures(stock)
        """)
        conn.execute("""
            CREATE INDEX IF NOT EXISTS idx_created_at ON captures(created_at DESC)
        """)

init_db()

# -------------------- Automatic Cleanup Scheduler --------------------

def schedule_cleanup():
    """Run cleanup every 24 hours"""
    def cleanup_task():
        while True:
            time.sleep(24 * 60 * 60)  # 24 hours
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
    # Check session first
    from flask import session
    if session.get('admin_authenticated'):
        return True
    
    # Check basic auth
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
        return Response(
            'Authentication required',
            401,
            {'WWW-Authenticate': 'Basic realm="Admin Panel"'}
        )
    return decorated

# -------------------- Cleanup Utilities --------------------

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
                            # Calculate size before deleting
                            for root, dirs, files in os.walk(item_path):
                                for f in files:
                                    fp = os.path.join(root, f)
                                    if os.path.exists(fp):
                                        cleaned_size += os.path.getsize(fp)
                            
                            import shutil
                            shutil.rmtree(item_path)
                            cleaned_count += 1
                            print(f"🧹 Cleaned up old directory: {item}")
                except Exception as e:
                    print(f"⚠ Could not clean {item_path}: {e}")
        
        # Clean up persistent storage if enabled
        if STORAGE_MODE == "persistent" and os.path.exists(PERSISTENT_STORAGE_PATH):
            for item in os.listdir(PERSISTENT_STORAGE_PATH):
                if item.startswith("cw-"):
                    item_path = os.path.join(PERSISTENT_STORAGE_PATH, item)
                    try:
                        if os.path.isdir(item_path):
                            dir_mtime = os.path.getmtime(item_path)
                            if dir_mtime < cutoff_time:
                                # Calculate size before deleting
                                for root, dirs, files in os.walk(item_path):
                                    for f in files:
                                        fp = os.path.join(root, f)
                                        if os.path.exists(fp):
                                            cleaned_size += os.path.getsize(fp)
                                
                                import shutil
                                shutil.rmtree(item_path)
                                cleaned_count += 1
                                print(f"🧹 Cleaned up old persistent directory: {item}")
                    except Exception as e:
                        print(f"⚠ Could not clean {item_path}: {e}")
        
        cleaned_size_mb = cleaned_size / (1024 * 1024)
        print(f"✓ Cleanup complete: removed {cleaned_count} directories ({cleaned_size_mb:.2f} MB)")
        return {"cleaned": cleaned_count, "size_mb": round(cleaned_size_mb, 2)}
    except Exception as e:
        print(f"❌ Cleanup failed: {e}")
        return {"cleaned": 0, "size_mb": 0}

@app.get("/admin/cleanup")
@require_admin_auth
def admin_cleanup():
    """Manual cleanup endpoint"""
    days = request.args.get("days", AUTO_CLEANUP_DAYS, type=int)
    result = cleanup_old_files(days)
    return jsonify({"cleaned": result["cleaned"], "size_mb": result["size_mb"], "days_old": days})

@app.get("/admin/storage")
@require_admin_auth
def admin_storage():
    """View storage status"""
    try:
        with get_db() as conn:
            total_captures = conn.execute("SELECT COUNT(*) as count FROM captures").fetchone()['count']
            
            existing_pdfs = conn.execute(
                "SELECT COUNT(*) as count FROM captures WHERE pdf_path IS NOT NULL"
            ).fetchone()['count']
            
            files_exist = 0
            files_missing = 0
            for row in conn.execute("SELECT pdf_path FROM captures WHERE pdf_path IS NOT NULL"):
                if row['pdf_path'] and os.path.exists(row['pdf_path']):
                    files_exist += 1
                else:
                    files_missing += 1
        
        # Calculate temp storage
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
        
        # Calculate persistent storage
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
        
        # Calculate estimated max storage (90 days, 3/day)
        estimated_max_captures = 90 * 3  # 270 captures
        estimated_max_size_mb = estimated_max_captures * 5  # ~5MB per capture
        
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
    """Admin dashboard"""
    try:
        # Get storage stats
        with get_db() as conn:
            total_captures = conn.execute("SELECT COUNT(*) as count FROM captures").fetchone()['count']
            
            # Captures by location
            location_stats = conn.execute("""
                SELECT location, COUNT(*) as count 
                FROM captures 
                GROUP BY location 
                ORDER BY count DESC
            """).fetchall()
            
            # Recent captures
            recent_captures = conn.execute("""
                SELECT id, stock, location, capture_utc, price_sha256, payment_sha256
                FROM captures
                ORDER BY created_at DESC
                LIMIT 20
            """).fetchall()
            
            # Captures per day (last 30 days)
            daily_stats = conn.execute("""
                SELECT DATE(created_at) as date, COUNT(*) as count
                FROM captures
                WHERE created_at >= DATE('now', '-30 days')
                GROUP BY DATE(created_at)
                ORDER BY date DESC
            """).fetchall()
        
        # Calculate storage
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
        estimated_max_mb = 270 * 5  # 270 captures * 5MB
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
  <!-- trimmed for brevity in this HTML block -->
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

@app.get("/history")
def history():
    # ... (unchanged for brevity; your original history route code remains intact)
    # keep your existing history HTML/template exactly as before
    # [SNIP — unchanged code from your message]
    # -------------------------------------------------------------------------
    # For space, not repeating here — keep your original /history function body
    # -------------------------------------------------------------------------
    pass  # <-- replace this 'pass' with your original /history function body

@app.get("/view/<int:capture_id>")
def view_capture(capture_id):
    # ... unchanged
    # [SNIP — keep original implementation]
    pass  # <-- replace with your original view_capture function body

@app.post("/capture")
def capture():
    # ... unchanged
    # [SNIP — keep original implementation]
    pass  # <-- replace with your original capture function body

# -------------------- Helpers --------------------

def generate_pdf(stock, location, zip_code, url, utc_time, https_date, 
                 price_path, pay_path, sha_price, sha_pay, 
                 rfc_price, rfc_pay, debug_info):
    """
    Generate a single-page PDF with top/bottom screenshots.
    Strategy:
      • Tighten top/bottom margins.
      • Minimize spacers/table font sizes.
      • Dynamically split remaining vertical space between images so both fit.
    """
    try:
        # Choose a working directory
        if price_path:
            tmpdir = os.path.dirname(price_path)
        elif pay_path:
            tmpdir = os.path.dirname(pay_path)
        else:
            tmpdir = tempfile.mkdtemp(prefix=f"cw-{stock}-")
        
        pdf_path = os.path.join(tmpdir, f"cw_{stock}_report.pdf")
        print(f"📄 Generating single-page PDF at: {pdf_path}")

        PAGE_W, PAGE_H = letter
        # Slightly smaller top/bottom margins as requested
        top_margin = 0.35 * inch
        bottom_margin = 0.35 * inch
        side_margin = 0.5 * inch

        doc = SimpleDocTemplate(
            pdf_path,
            pagesize=letter,
            leftMargin=side_margin, rightMargin=side_margin,
            topMargin=top_margin, bottomMargin=bottom_margin
        )

        story = []
        styles = getSampleStyleSheet()

        # Tighter title & styling to save space
        title_style = ParagraphStyle(
            'CustomTitle',
            parent=styles['Heading1'],
            fontSize=14,
            leading=16,
            textColor=colors.HexColor('#003087'),
            spaceAfter=4,
            alignment=TA_CENTER
        )
        story.append(Paragraph("Camping World Compliance Capture Report", title_style))
        story.append(Spacer(1, 0.08*inch))

        # Metadata: smaller fonts & compact grid
        meta_data = [
            ['Stock Number:', stock],
            ['Location:', f"{location} (ZIP: {zip_code})"],
            ['URL:', url],
            ['Capture Time (UTC):', utc_time],
            ['HTTPS Date:', https_date or 'N/A'],
        ]
        meta_table = Table(meta_data, colWidths=[1.7*inch, (PAGE_W - 2*side_margin) - 1.7*inch])
        meta_table.setStyle(TableStyle([
            ('BACKGROUND', (0, 0), (0, -1), colors.HexColor('#f3f4f6')),
            ('ALIGN', (0, 0), (0, -1), 'RIGHT'),
            ('FONTNAME', (0, 0), (0, -1), 'Helvetica-Bold'),
            ('FONTSIZE', (0, 0), (-1, -1), 8),
            ('LEADING', (0, 0), (-1, -1), 9),
            ('TEXTCOLOR', (0, 0), (-1, -1), colors.black),
            ('GRID', (0, 0), (-1, -1), 0.25, colors.lightgrey),
            ('VALIGN', (0, 0), (-1, -1), 'MIDDLE'),
            ('BOTTOMPADDING', (0, 0), (-1, -1), 2),
            ('TOPPADDING', (0, 0), (-1, -1), 2),
        ]))
        story.append(meta_table)
        story.append(Spacer(1, 0.06*inch))

        # Cryptographic timestamps (optional, compact)
        if rfc_price or rfc_pay:
            small_heading = ParagraphStyle('h2small', parent=styles['Heading2'], fontSize=11, leading=12, spaceAfter=2)
            story.append(Paragraph("Cryptographic Timestamps (RFC 3161)", small_heading))
            ts_data = []
            if rfc_price:
                ts_data.append(['Price Disclosure:', f"{rfc_price['timestamp']} | TSA: {rfc_price['tsa']}"])
            if rfc_pay:
                ts_data.append(['Payment Disclosure:', f"{rfc_pay['timestamp']} | TSA: {rfc_pay['tsa']}"])
            ts_table = Table(ts_data, colWidths=[1.7*inch, (PAGE_W - 2*side_margin) - 1.7*inch])
            ts_table.setStyle(TableStyle([
                ('BACKGROUND', (0, 0), (-1, -1), colors.HexColor('#ecfdf5')),
                ('FONTNAME', (0, 0), (0, -1), 'Helvetica-Bold'),
                ('FONTSIZE', (0, 0), (-1, -1), 7),
                ('LEADING', (0, 0), (-1, -1), 8),
                ('GRID', (0, 0), (-1, -1), 0.25, colors.HexColor('#10b981')),
                ('BOTTOMPADDING', (0, 0), (-1, -1), 2),
                ('TOPPADDING', (0, 0), (-1, -1), 2),
            ]))
            story.append(ts_table)
            story.append(Spacer(1, 0.06*inch))

        # Section label, very compact
        story.append(Paragraph("<b>Captured Disclosures</b>", ParagraphStyle('lbl', parent=styles['Normal'], fontSize=10, leading=11)))
        story.append(Spacer(1, 0.04*inch))

        # Helper to compute scaled sizes
        def scaled_img_dims(path, max_w, max_h):
            im = PILImage.open(path)
            w, h = im.size
            aspect = h / float(w)
            # initial dimensions based on max width
            tw = min(max_w, w)
            th = tw * aspect
            if th > max_h:
                th = max_h
                tw = th / aspect
            return tw, th

        # We will calculate how much vertical room is left for images and split it
        usable_h = PAGE_H - top_margin - bottom_margin

        # Estimate fixed block height that’s already in 'story' above.
        # These estimates are conservative to ensure we stay on one page.
        est_title = 0.30 * inch
        est_meta  = 0.90 * inch
        est_ts    = 0.55 * inch if (rfc_price or rfc_pay) else 0
        est_labels_and_spacers = 0.30 * inch
        est_hashes = 0.70 * inch   # at the end

        fixed_blocks = est_title + est_meta + est_ts + est_labels_and_spacers + est_hashes
        # Make sure we leave a little buffer
        buffer = 0.15 * inch

        img_budget = max(usable_h - fixed_blocks - buffer, 1.8 * inch)  # never allow less than 1.8" total

        # Decide how to split between price and payment
        have_price = bool(price_path and os.path.exists(price_path))
        have_pay   = bool(pay_path and os.path.exists(pay_path))

        # Default split: if two images, split ~50/50; if one, give it all.
        if have_price and have_pay:
            price_budget = img_budget * 0.5
            pay_budget   = img_budget * 0.5
        elif have_price:
            price_budget = img_budget
            pay_budget   = 0
        elif have_pay:
            price_budget = 0
            pay_budget   = img_budget
        else:
            price_budget = 0
            pay_budget   = 0

        max_img_width = (PAGE_W - 2*side_margin)

        # Price block
        if have_price:
            story.append(Paragraph("<b>Price Disclosure</b>", ParagraphStyle('lbl2', parent=styles['Normal'], fontSize=9, leading=10)))
            tw, th = scaled_img_dims(price_path, max_img_width, price_budget)
            img_obj = Image(price_path, width=tw, height=th)
            # Center via one-cell table, tight spacers
            t = Table([[img_obj]], colWidths=[max_img_width])
            t.setStyle(TableStyle([('ALIGN', (0,0), (-1,-1), 'CENTER')]))
            story.append(t)
            story.append(Spacer(1, 0.06*inch))
        else:
            story.append(Paragraph("<b>Price Disclosure</b> — not available", ParagraphStyle('lbl2', parent=styles['Normal'], fontSize=9, leading=10)))
            story.append(Spacer(1, 0.04*inch))

        # Payment block
        if have_pay:
            story.append(Paragraph("<b>Payment Disclosure</b>", ParagraphStyle('lbl2', parent=styles['Normal'], fontSize=9, leading=10)))
            tw, th = scaled_img_dims(pay_path, max_img_width, pay_budget)
            img_obj = Image(pay_path, width=tw, height=th)
            t = Table([[img_obj]], colWidths=[max_img_width])
            t.setStyle(TableStyle([('ALIGN', (0,0), (-1,-1), 'CENTER')]))
            story.append(t)
            story.append(Spacer(1, 0.06*inch))
        else:
            story.append(Paragraph("<b>Payment Disclosure</b> — not available", ParagraphStyle('lbl2', parent=styles['Normal'], fontSize=9, leading=10)))
            story.append(Spacer(1, 0.04*inch))

        # Hashes (very compact)
        story.append(Paragraph("SHA-256 Verification Hashes", ParagraphStyle('h2mini', parent=styles['Heading2'], fontSize=11, leading=12, spaceAfter=2)))
        hash_data = [
            ['Price Disclosure:', sha_price],
            ['Payment Disclosure:', sha_pay],
        ]
        hash_table = Table(hash_data, colWidths=[1.7*inch, (PAGE_W - 2*side_margin) - 1.7*inch])
        hash_table.setStyle(TableStyle([
            ('FONTNAME', (0, 0), (0, -1), 'Helvetica-Bold'),
            ('FONTNAME', (1, 0), (1, -1), 'Courier'),
            ('FONTSIZE', (0, 0), (-1, -1), 7),
            ('LEADING', (0, 0), (-1, -1), 8),
            ('GRID', (0, 0), (-1, -1), 0.25, colors.lightgrey),
            ('BOTTOMPADDING', (0, 0), (-1, -1), 2),
            ('TOPPADDING', (0, 0), (-1, -1), 2),
        ]))
        story.append(hash_table)

        # Build doc
        doc.build(story)
        print(f"✓ Single-page PDF generated: {pdf_path} ({os.path.getsize(pdf_path)} bytes)")
        return pdf_path

    except Exception as e:
        print(f"❌ PDF generation failed: {e}")
        traceback.print_exc()
        return None

def sha256_file(path):
    if not path or not os.path.exists(path): return "N/A"
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(1024*1024), b""): h.update(chunk)
    return h.hexdigest()

def https_date():
    try:
        r = requests.head("https://cloudflare.com", timeout=8)
        return r.headers.get("Date")
    except Exception:
        return None

def get_rfc3161_timestamp(file_path):
    """Get RFC 3161 timestamp for a file from a public TSA."""
    if not file_path or not os.path.exists(file_path):
        return None
    
    print(f"🕐 Getting RFC 3161 timestamp for {os.path.basename(file_path)}...")
    
    file_hash = hashlib.sha256()
    with open(file_path, "rb") as f:
        for chunk in iter(lambda: f.read(1024*1024), b""):
            file_hash.update(chunk)
    digest = file_hash.digest()
    
    for tsa_url in TSA_URLS:
        try:
            print(f"  Trying TSA: {tsa_url}")
            rt = RemoteTimestamper(tsa_url, hashname='sha256')
            tsr = rt.timestamp(data=digest)
            
            if tsr:
                from rfc3161ng import decode_timestamp_response
                ts_info = decode_timestamp_response(tsr)
                timestamp_dt = ts_info.gen_time
                timestamp_str = timestamp_dt.strftime("%Y-%m-%d %H:%M:%S UTC")
                
                token_path = file_path + ".tsr"
                with open(token_path, "wb") as tf:
                    tf.write(tsr)
                
                print(f"  ✓ Timestamp obtained: {timestamp_str}")
                
                return {
                    "timestamp": timestamp_str,
                    "tsa": tsa_url,
                    "cert_info": f"Token saved: {os.path.basename(token_path)}",
                    "token_file": token_path
                }
        except Exception as e:
            print(f"  ✗ TSA {tsa_url} failed: {e}")
            continue
    
    print(f"  ✗ All TSAs failed for {os.path.basename(file_path)}")
    return None

def find_and_trigger_tooltip(page, label_text, tooltip_name):
    # ... unchanged
    pass  # <-- replace with your original function body

def do_capture(stock, zip_code, location_name, latitude, longitude):
    # ... unchanged
    pass  # <-- replace with your original function body

# -------------------- Entrypoint --------------------

if __name__ == "__main__":
    app.run(host="0.0.0.0", port=PORT, debug=False)
