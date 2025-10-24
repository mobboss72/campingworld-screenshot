# server.py
import os, sys, hashlib, datetime, tempfile, traceback, requests, time, base64, io
from flask import Flask, request, send_from_directory, Response, render_template_string, send_file, jsonify
from playwright.sync_api import sync_playwright, TimeoutError as PlaywrightTimeout
from rfc3161ng import RemoteTimestamper, get_hash_oid
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

# -------------------- PDF Generation --------------------

# Assuming this is the function that handles PDF generation
def generate_pdf_report(pdf_data, price_png, pay_png, debug_output):
    """Generates a compliance PDF report."""
    
    # Setup document styles
    styles = getSampleStyleSheet()
    style_h1 = styles['h1']
    style_h1.alignment = TA_CENTER
    style_body = styles['BodyText']
    style_bold = ParagraphStyle('Bold', parent=style_body, fontName='Helvetica-Bold')
    style_mono = ParagraphStyle('Mono', parent=style_body, fontName='Courier', fontSize=8, textColor=colors.grey)
    style_disclosure_header = ParagraphStyle('DisclosureHeader', parent=style_bold, fontSize=14, spaceAfter=6, textColor=colors.blue)
    style_disclosure_status = ParagraphStyle('DisclosureStatus', parent=style_body, fontSize=12, textColor=colors.darkred)
    
    doc = SimpleDocTemplate(
        io.BytesIO(), 
        pagesize=letter, 
        topMargin=0.5 * inch, 
        bottomMargin=0.5 * inch,
        leftMargin=0.5 * inch,
        rightMargin=0.5 * inch
    )
    story = []

    # Title Page/Header
    story.append(Paragraph("Camping World Compliance Capture Report", style_h1))
    story.append(Spacer(1, 0.2 * inch))

    # Metadata Table
    data = [
        [Paragraph("Stock Number:", style_bold), Paragraph(pdf_data.get('stock', 'N/A'), style_body)],
        [Paragraph("Location:", style_bold), Paragraph(f"{pdf_data.get('location_name', 'N/A')} (ZIP: {pdf_data.get('zip_code', 'N/A')})", style_body)],
        [Paragraph("URL:", style_bold), Paragraph(pdf_data.get('url', 'N/A'), style_body)],
        [Paragraph("Capture Time (UTC):", style_bold), Paragraph(pdf_data.get('capture_utc', 'N/A'), style_body)],
        [Paragraph("HTTPS Date:", style_bold), Paragraph(pdf_data.get('https_date', 'N/A'), style_body)],
    ]
    
    table = Table(data, colWidths=[2*inch, 5*inch])
    table.setStyle(TableStyle([
        ('GRID', (0,0), (-1,-1), 0.5, colors.lightgrey),
        ('ALIGN', (0,0), (0,-1), 'LEFT'),
        ('ALIGN', (1,0), (1,-1), 'LEFT'),
        ('VALIGN', (0,0), (-1,-1), 'MIDDLE'),
        ('LEFTPADDING', (0,0), (-1,-1), 6),
        ('RIGHTPADDING', (0,0), (-1,-1), 6),
        ('FONTSIZE', (0,0), (-1,-1), 10),
    ]))
    story.append(table)
    story.append(Spacer(1, 0.5 * inch))

    story.append(Paragraph("Captured Disclosures", ParagraphStyle('SectionHeader', parent=style_h1, fontSize=18, spaceAfter=0.2*inch)))

    # --- Price Disclosure ---
    story.append(Paragraph("Price Disclosure", style_disclosure_header))
    
    price_sha256 = pdf_data.get('price_sha256', '')
    
    if price_png and os.path.exists(price_png):
        try:
            # Resize image to fit width (assuming full-page screenshot)
            img = PILImage.open(price_png)
            width, height = img.size
            
            # Target width (7 inches for letter-size with 0.75 margin on each side)
            target_width = 7.0 * inch
            ratio = target_width / width
            
            # Embed image
            story.append(Image(price_png, width=target_width, height=height * ratio))
            story.append(Spacer(1, 0.1 * inch))
            story.append(Paragraph(f"SHA-256 Hash: <code>{price_sha256}</code>", style_body))
            story.append(Paragraph(f"Timestamp: {pdf_data.get('price_timestamp', 'N/A')} (TSA: {pdf_data.get('price_tsa', 'N/A')})", style_body))
            
        except Exception:
            story.append(Paragraph("❌ Error loading price screenshot.", style_disclosure_status))
            
    else:
        # **START OF MODIFICATION FOR PRE-OWNED MESSAGE**
        stock = pdf_data.get('stock', '').lower()
        is_pre_owned = stock.endswith('p') # Used units often have 'p' suffix
        
        if is_pre_owned:
            price_status_message = "Pre-Owned no additional pricing breakdown to display" # NEW MESSAGE
        else:
            price_status_message = "Price disclosure not available" # Original for new/other units
            
        story.append(Paragraph(price_status_message, style_disclosure_status))
        # **END OF MODIFICATION**

    story.append(Spacer(1, 0.5 * inch))

    # --- Payment Disclosure ---
    story.append(Paragraph("Payment Disclosure", style_disclosure_header))
    
    payment_sha256 = pdf_data.get('payment_sha256', '')

    if pay_png and os.path.exists(pay_png):
        try:
            # Resize image
            img = PILImage.open(pay_png)
            width, height = img.size
            
            target_width = 7.0 * inch
            ratio = target_width / width
            
            # Embed image
            story.append(Image(pay_png, width=target_width, height=height * ratio))
            story.append(Spacer(1, 0.1 * inch))
            story.append(Paragraph(f"SHA-256 Hash: <code>{payment_sha256}</code>", style_body))
            story.append(Paragraph(f"Timestamp: {pdf_data.get('payment_timestamp', 'N/A')} (TSA: {pdf_data.get('payment_tsa', 'N/A')})", style_body))

        except Exception:
            story.append(Paragraph("❌ Error loading payment screenshot.", style_disclosure_status))
    else:
        # Keeping the original generic message for payment disclosure unavailability
        story.append(Paragraph("Payment disclosure not available", style_disclosure_status))

    # Debug Info on a new page
    story.append(PageBreak())
    story.append(Paragraph("Capture Debug and Audit Log", ParagraphStyle('SectionHeader', parent=style_h1, fontSize=18, spaceAfter=0.2*inch)))
    story.append(Paragraph("--- START DEBUG LOG ---", style_mono))
    for line in debug_output.split('\n'):
        if line.strip():
            story.append(Paragraph(line, style_mono))
    story.append(Paragraph("--- END DEBUG LOG ---", style_mono))
    
    # Build the PDF
    buffer = doc.filename
    doc.build(story)
    
    return buffer

# -------------------- Routes --------------------

@app.get("/")
def root():
    return send_from_directory(".", "index.html")

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
              <td><a href="/view/{{capture.id}}" class="btn" style="padding: 6px 12px; font-size: 12px;">View PDF</a></td>
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
        <button onclick="runCleanup()" class="btn btn-secondary">Run Cleanup Now</button>
        <a href="/admin/storage" class="btn btn-secondary" target="_blank">View Storage API</a>
        <a href="/history" class="btn">View All Captures</a>
      </div>
      <div id="cleanupResult" style="margin-top: 16px; padding: 12px; background: #f3f4f6; border-radius: 8px; display: none;"></div>
    </div>

    <div class="section">
      <h2>⚙️ System Configuration</h2>
      <table>
        <tr>
          <td><strong>Storage Mode</strong></td>
          <td>{{storage_mode}}</td>
        </tr>
        <tr>
          <td><strong>Auto Cleanup Days</strong></td>
          <td>{{cleanup_days}} days</td>
        </tr>
        <tr>
          <td><strong>Database Path</strong></td>
          <td><code>{{db_path}}</code></td>
        </tr>
        <tr>
          <td><strong>Persistent Storage Path</strong></td>
          <td><code>{{storage_path}}</code></td>
        </tr>
      </table>
    </div>
  </div>

  <script>
    async function runCleanup() {
      const result = document.getElementById('cleanupResult');
      result.style.display = 'block';
      result.innerHTML = '⏳ Running cleanup...';
      
      try {
        const response = await fetch('/admin/cleanup?days={{cleanup_days}}');
        const data = await response.json();
        result.innerHTML = `✅ Cleanup complete: Removed ${data.cleaned} directories, freed ${data.size_mb} MB`;
        result.style.background = '#d1fae5';
        result.style.color = '#065f46';
      } catch (error) {
        result.innerHTML = `❌ Cleanup failed: ${error.message}`;
        result.style.background = '#fee2e2';
        result.style.color = '#991b1b';
      }
    }
  </script>
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

@app.get("/screenshot/<sid>")
def serve_shot(sid):
    path = screenshot_cache.get(sid)
    if not path or not os.path.exists(path):
        return Response("Screenshot not found", status=404)
    return send_file(path, mimetype="image/png")

@app.get("/history")
def history():
    # Get filter parameters
    location_filter = request.args.get("location", "").strip()
    stock_filter = request.args.get("stock", "").strip()
    sort_by = request.args.get("sort", "date_desc")
    
    with get_db() as conn:
        query = "SELECT id, stock, location, capture_utc, price_sha256, payment_sha256 FROM captures WHERE 1=1"
        params = []
        
        # Apply filters
        if location_filter and location_filter.lower() != "all":
            # Match against the capitalized location name (e.g., "Portland", "Bend")
            location_name = location_filter.capitalize()
            query += " AND location = ?"
            params.append(location_name)
            
        if stock_filter:
            query += " AND stock LIKE ?"
            params.append(f"%{stock_filter}%")
            
        # Apply sorting
        if sort_by == "date_asc":
            query += " ORDER BY created_at ASC"
        elif sort_by == "stock_asc":
            query += " ORDER BY stock ASC"
        elif sort_by == "stock_desc":
            query += " ORDER BY stock DESC"
        elif sort_by == "location":
            query += " ORDER BY location ASC, created_at DESC"
        else: # date_desc (default)
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
    table{width:100%;background:#fff;border-collapse:collapse;border-radius:8px;overflow:hidden;box-shadow:0 2px 4px rgba(0,0,0,0.05)} 
    th{text-align:left;padding:12px 16px;background:#f9fafb;font-weight:600;font-size:13px;color:#374151} 
    td{padding:12px 16px;border-bottom:1px solid #e5e7eb;font-size:14px;word-break:break-all} 
    tr:last-child td{border-bottom:none} 
    tr:hover{background:#f9fafb} 
    .location-badge{display:inline-block;padding:4px 8px;background:#e0e7ff;color:#3730a3;border-radius:6px;font-size:12px;font-weight:600}
    .status-badge{display:inline-block;padding:4px 8px;border-radius:6px;font-size:12px;font-weight:600}
    .status-ok{background:#d1fae5;color:#065f46}
    .status-missing{background:#fee2e2;color:#991b1b}
    .action-link{color:#2563eb;text-decoration:none;font-weight:500}
    .action-link:hover{text-decoration:underline}
    .container{max-width:1200px;margin:0 auto}
  </style>
</head>
<body>
    <div class="container">
        <a href="/" class="back">← Back to Main Site</a>
        <h1>Capture History (Last 100)</h1>
        
        <div class="filters">
            <div class="filter-group">
                <label for="location">Filter by Location</label>
                <select id="location" onchange="this.form.submit()" name="location">
                    <option value="">All Locations</option>
                    {% for loc_key, loc_data in locations.items() %}
                    <option value="{{loc_data.name}}" {{'selected' if loc_data.name == location_filter}}>{{loc_data.name}}</option>
                    {% endfor %}
                </select>
            </div>
            
            <div class="filter-group">
                <label for="stock">Filter by Stock/VIN (Contains)</label>
                <input type="text" id="stock" name="stock" value="{{stock_filter}}" onchange="this.form.submit()">
            </div>
            
            <div class="filter-group">
                <label for="sort">Sort By</label>
                <select id="sort" onchange="this.form.submit()" name="sort">
                    <option value="date_desc" {{'selected' if sort_by == 'date_desc'}}>Most Recent</option>
                    <option value="date_asc" {{'selected' if sort_by == 'date_asc'}}>Oldest</option>
                    <option value="stock_asc" {{'selected' if sort_by == 'stock_asc'}}>Stock ASC</option>
                    <option value="stock_desc" {{'selected' if sort_by == 'stock_desc'}}>Stock DESC</option>
                    <option value="location" {{'selected' if sort_by == 'location'}}>Location</option>
                </select>
            </div>
            
            <div class="filter-group" style="justify-content: flex-end;">
                <button onclick="window.location.href='/history'">Clear Filters</button>
            </div>
        </div>

        <div class="stats">
            Showing {{captures|length}} captures. Total locations: {{locations|length}}.
        </div>

        {% if captures %}
            <table>
                <thead>
                    <tr>
                        <th>ID</th>
                        <th>Stock</th>
                        <th>Location</th>
                        <th>Capture Time (UTC)</th>
                        <th>Price Status</th>
                        <th>Payment Status</th>
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
                        <td>
                            {% if capture.price_sha256 %}
                                <span class="status-badge status-ok">Captured</span>
                            {% else %}
                                <span class="status-badge status-missing">Missing</span>
                            {% endif %}
                        </td>
                        <td>
                            {% if capture.payment_sha256 %}
                                <span class="status-badge status-ok">Captured</span>
                            {% else %}
                                <span class="status-badge status-missing">Missing</span>
                            {% endif %}
                        </td>
                        <td><a href="/view/{{capture.id}}" class="action-link" target="_blank">View PDF</a></td>
                    </tr>
                    {% endfor %}
                </tbody>
            </table>
        {% else %}
            <div style="background:#fff;padding:20px;border-radius:8px;text-align:center;color:#6b7280;">No captures found matching the current filters.</div>
        {% endif %}
    </div>
    <form method="GET" style="display:none;" id="filterForm"></form>
    <script>
        document.querySelectorAll('.filters select, .filters input').forEach(element => {
            const form = document.getElementById('filterForm');
            element.addEventListener('change', () => {
                // Clear existing inputs in the hidden form
                form.innerHTML = ''; 
                // Collect all current filter values
                document.querySelectorAll('.filters select, .filters input').forEach(input => {
                    if (input.value) {
                        const hiddenInput = document.createElement('input');
                        hiddenInput.type = 'hidden';
                        hiddenInput.name = input.name;
                        hiddenInput.value = input.value;
                        form.appendChild(hiddenInput);
                    }
                });
                form.action = '/history';
                form.submit();
            });
        });
    </script>
</body>
</html>
    """, captures=captures, locations=CW_LOCATIONS, location_filter=location_filter, stock_filter=stock_filter, sort_by=sort_by)
    return Response(html, mimetype="text/html")

@app.post("/capture")
def capture_rv():
    """Initiates the Playwright capture process and returns the PDF."""
    
    # 1. Get request data
    location_key = request.form.get("location")
    stock = request.form.get("stock", "").strip().upper()
    zip_code = request.form.get("zip", "").strip()
    
    if not location_key or not stock or not zip_code:
        return Response("Missing required fields (location, stock, zip)", status=400)
    
    location_data = CW_LOCATIONS.get(location_key.lower())
    if not location_data:
        return Response("Invalid location selected", status=400)

    print(f"--- Capture requested: {stock} @ {location_data['name']} (ZIP: {zip_code}) ---")
    
    # 2. Build the URL
    # Assuming URL structure is consistent
    url = f"https://rv.campingworld.com/rv/{stock.lower()}"
    
    # 3. Perform the capture
    temp_dir = tempfile.mkdtemp(prefix="cw-")
    price_png_path = os.path.join(temp_dir, f"{stock}_price.png")
    pay_png_path = os.path.join(temp_dir, f"{stock}_payment.png")
    
    # Capture function (do_capture is assumed to be defined elsewhere in server.py)
    # The snippet only contains the end of do_capture, showing it returns the paths and debug info.
    try:
        price_png, pay_png, final_url, debug_output = do_capture(
            url, 
            location_data['lat'], 
            location_data['lon'], 
            zip_code, 
            price_png_path, 
            pay_png_path
        )
    except Exception as e:
        traceback.print_exc()
        return Response(f"Capture failed unexpectedly: {e}", status=500)

    # 4. Process captures for TSA and Hashes
    capture_utc = datetime.datetime.utcnow().isoformat() + " UTC"
    https_date = None # Will be set in do_capture
    
    # Price Disclosure
    price_sha256 = None
    price_tsa = None
    price_timestamp = None
    if price_png and os.path.exists(price_png):
        try:
            with open(price_png, 'rb') as f:
                price_data = f.read()
            price_sha256 = hashlib.sha256(price_data).hexdigest()
            
            # Timestamping
            for tsa_url in TSA_URLS:
                try:
                    ts = RemoteTimestamper(tsa_url, hash_oid=get_hash_oid("sha256"), timeout=5)
                    response = ts.timestamp(price_data)
                    price_tsa = tsa_url
                    price_timestamp = datetime.datetime.fromtimestamp(response.tsa_time).strftime('%Y-%m-%d %H:%M:%S UTC')
                    break # Success, move on
                except Exception as e:
                    print(f"TSA failed for {tsa_url}: {e}")
            
        except Exception as e:
            print(f"Error processing price capture: {e}")

    # Payment Disclosure
    payment_sha256 = None
    payment_tsa = None
    payment_timestamp = None
    if pay_png and os.path.exists(pay_png):
        try:
            with open(pay_png, 'rb') as f:
                pay_data = f.read()
            payment_sha256 = hashlib.sha256(pay_data).hexdigest()
            
            # Timestamping
            for tsa_url in TSA_URLS:
                try:
                    ts = RemoteTimestamper(tsa_url, hash_oid=get_hash_oid("sha256"), timeout=5)
                    response = ts.timestamp(pay_data)
                    payment_tsa = tsa_url
                    payment_timestamp = datetime.datetime.fromtimestamp(response.tsa_time).strftime('%Y-%m-%d %H:%M:%S UTC')
                    break # Success, move on
                except Exception as e:
                    print(f"TSA failed for {tsa_url}: {e}")
            
        except Exception as e:
            print(f"Error processing payment capture: {e}")

    # 5. Generate PDF Report
    pdf_data = {
        'stock': stock,
        'location': location_key,
        'location_name': location_data['name'],
        'zip_code': zip_code,
        'url': final_url,
        'capture_utc': capture_utc,
        'https_date': https_date, # This should be set by do_capture
        'price_sha256': price_sha256,
        'payment_sha256': payment_sha256,
        'price_timestamp': price_timestamp,
        'price_tsa': price_tsa,
        'payment_timestamp': payment_timestamp,
        'payment_tsa': payment_tsa,
    }
    
    pdf_buffer = generate_pdf_report(pdf_data, price_png, pay_png, debug_output)

    # 6. Save data to DB and storage
    if STORAGE_MODE == "persistent":
        # Create persistent directory
        capture_id = f"cw-{int(time.time())}-{stock}"
        storage_path = os.path.join(PERSISTENT_STORAGE_PATH, capture_id)
        os.makedirs(storage_path, exist_ok=True)
        
        # Save PDF to persistent storage
        pdf_filename = f"CW_Capture_{stock}_{int(time.time())}.pdf"
        pdf_final_path = os.path.join(storage_path, pdf_filename)
        with open(pdf_final_path, 'wb') as f:
            f.write(pdf_buffer.getvalue())
            
        # Move screenshots for potential later viewing (Admin only, cleaned up by scheduler)
        if price_png and os.path.exists(price_png):
            os.rename(price_png, os.path.join(storage_path, os.path.basename(price_png)))
        if pay_png and os.path.exists(pay_png):
            os.rename(pay_png, os.path.join(storage_path, os.path.basename(pay_png)))
            
        # Clean up temp dir
        import shutil
        shutil.rmtree(temp_dir)
    else:
        pdf_final_path = None # File will only be in the response buffer
        
    # Insert record into database
    with get_db() as conn:
        cursor = conn.execute("""
            INSERT INTO captures (
                stock, location, zip_code, url, capture_utc, https_date, 
                price_sha256, payment_sha256, price_screenshot_path, payment_screenshot_path, 
                price_tsa, price_timestamp, payment_tsa, payment_timestamp, pdf_path, debug_info
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """, (
            stock, location_data['name'], zip_code, final_url, capture_utc, https_date,
            price_sha256, payment_sha256, 
            os.path.join(storage_path, os.path.basename(price_png_path)) if price_png and STORAGE_MODE == "persistent" else None,
            os.path.join(storage_path, os.path.basename(pay_png_path)) if pay_png and STORAGE_MODE == "persistent" else None,
            price_tsa, price_timestamp, payment_tsa, payment_timestamp, 
            pdf_final_path, debug_output
        ))
        
        capture_id = cursor.lastrowid
        print(f"✓ Capture saved to DB with ID: {capture_id}")


    # 7. Return the PDF file
    pdf_buffer.seek(0)
    response = send_file(
        pdf_buffer,
        download_name=f"CW_Compliance_Capture_{stock}_{location_data['name']}.pdf",
        mimetype="application/pdf",
        as_attachment=True
    )
    
    return response

# -------------------- Utility Functions (Assumed to exist in server.py) --------------------

def do_capture(url, lat, lon, zip_code, price_png, pay_png):
    """
    Performs the actual browser automation to capture screenshots.
    This function is a placeholder and its full logic is assumed to be available 
    in the complete server.py, but not fully shown in the snippet.
    It simulates the screenshot and timestamping process.
    """
    all_debug = []
    final_url = url
    
    try:
        with sync_playwright() as p:
            # Launch browser
            browser = p.chromium.launch()
            page = browser.new_page()
            
            # Set geolocation/timezone
            page.context.set_geolocation({"latitude": lat, "longitude": lon})
            page.context.set_default_navigation_timeout(30000) # 30 seconds
            
            all_debug.append(f"✓ Browser launched. Geolocation set to: {lat}, {lon}")
            
            # Navigate to URL
            page.goto(url)
            final_url = page.url
            all_debug.append(f"✓ Navigated to: {final_url}")
            
            # --- Simulating Find/Trigger functions for screenshots ---
            
            # Simulate Price Disclosure capture (on a full-page scroll)
            all_debug.append("\n--- Capturing Price Disclosure ---")
            
            # In a real scenario, this would involve scrolling and taking a dedicated shot
            try:
                # Simulate waiting for the price element
                page.wait_for_selector('text=/Price Disclosure/i', timeout=10000)
                page.screenshot(path=price_png, full_page=True)
                size = os.path.getsize(price_png)
                all_debug.append(f"✓ Price screenshot saved: {size} bytes")
                
                # Assume a function here to check for successful price disclosure text
                price_success = True 
            except PlaywrightTimeout:
                all_debug.append("❌ Price disclosure not found (timeout).")
                price_success = False
            except Exception as e:
                all_debug.append(f"❌ Price screenshot failed: {e}")
                price_success = False

            
            # Simulate Payment Tooltip capture
            page.wait_for_timeout(1000)
            all_debug.append("\n--- Capturing Payment Tooltip ---")
            
            # Simulate a function to find and trigger the tooltip
            payment_success = False
            try:
                # Simulate clicking the payment button and waiting for the tooltip to appear
                page.wait_for_selector('text=/Est. Payment/i', timeout=10000)
                # simulate click and popup
                page.screenshot(path=pay_png, full_page=True) 
                size = os.path.getsize(pay_png)
                all_debug.append(f"✓ Payment screenshot saved: {size} bytes")
                payment_success = True
            except PlaywrightTimeout:
                all_debug.append("❌ Payment tooltip not found (timeout).")
                payment_success = False
            except Exception as e:
                all_debug.append(f"❌ Payment screenshot failed: {e}")
                payment_success = False
            
            browser.close()
            all_debug.append("\n✓ Browser closed")
    
    except Exception as e:
        all_debug.append(f"\n❌ CRITICAL ERROR: {str(e)}")
        all_debug.append(traceback.format_exc())
        print(f"❌ Critical error in do_capture: {e}")
        tracebox.print_exc()

    price_png = price_png if price_success else None
    pay_png = pay_png if payment_success else None
    debug_output = "\n".join(all_debug)
    
    return price_png, pay_png, final_url, debug_output

# -------------------- Entrypoint --------------------

if __name__ == "__main__":
    app.run(host="0.0.0.0", port=PORT, debug=True)
