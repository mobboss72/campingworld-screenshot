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
        else:  # date_desc (default)
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
        <td><code style="font-size:10px">{{capture.price_sha256[:16]}}...</code></td>
        <td><code style="font-size:10px">{{capture.payment_sha256[:16]}}...</code></td>
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
    
    if capture['pdf_path'] and os.path.exists(capture['pdf_path']):
        return send_file(capture['pdf_path'], mimetype="application/pdf", as_attachment=True,
                        download_name=f"CW_Capture_{capture['stock']}_{capture_id}.pdf")
    
    price_path = capture['price_screenshot_path']
    pay_path = capture['payment_screenshot_path']
    
    if (price_path and os.path.exists(price_path)) or (pay_path and os.path.exists(pay_path)):
        print(f"📄 Regenerating PDF for capture {capture_id}")
        
        rfc_price = None
        rfc_pay = None
        if capture['price_tsa']:
            rfc_price = {'timestamp': capture['price_timestamp'], 'tsa': capture['price_tsa'], 'cert_info': None}
        if capture['payment_tsa']:
            rfc_pay = {'timestamp': capture['payment_timestamp'], 'tsa': capture['payment_tsa'], 'cert_info': None}
        
        try:
            pdf_path = generate_pdf(
                stock=capture['stock'],
                location=capture['location'],
                zip_code=capture['zip_code'],
                url=capture['url'],
                utc_time=capture['capture_utc'],
                https_date=capture['https_date'],
                price_path=price_path if os.path.exists(price_path or "") else None,
                pay_path=pay_path if os.path.exists(pay_path or "") else None,
                sha_price=capture['price_sha256'],
                sha_pay=capture['payment_sha256'],
                rfc_price=rfc_price,
                rfc_pay=rfc_pay,
                debug_info=capture['debug_info']
            )
            
            if pdf_path and os.path.exists(pdf_path):
                return send_file(pdf_path, mimetype="application/pdf", as_attachment=True,
                               download_name=f"CW_Capture_{capture['stock']}_{capture_id}.pdf")
        except Exception as e:
            print(f"❌ PDF regeneration failed: {e}")
            traceback.print_exc()
    
    return Response("PDF and screenshots no longer available. Data has been cleaned up.", status=404)

@app.post("/capture")
def capture():
    try:
        stock = (request.form.get("stock") or "").strip()
        location = (request.form.get("location") or "portland").strip().lower()
        
        if not stock.isdigit():
            return Response("Invalid stock number", status=400)
        
        if location not in CW_LOCATIONS:
            return Response("Invalid location", status=400)
        
        loc_info = CW_LOCATIONS[location]
        zip_code = loc_info["zip"]
        location_name = loc_info["name"]
        latitude = loc_info["lat"]
        longitude = loc_info["lon"]

        price_path, pay_path, url, debug_info = do_capture(stock, zip_code, location_name, latitude, longitude)

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

        pdf_path = None
        if price_ok or pay_ok:
            try:
                pdf_path = generate_pdf(
                    stock=stock,
                    location=location_name,
                    zip_code=zip_code,
                    url=url,
                    utc_time=utc_now,
                    https_date=hdate,
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
                    stock, location_name, zip_code, url, utc_now, hdate,
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
            return send_file(pdf_path, mimetype="application/pdf", as_attachment=True,
                           download_name=f"CW_Capture_{stock}_{capture_id or 'temp'}.pdf")
        else:
            error_html = f"""
<!doctype html>
<html>
<head><title>PDF Generation Failed</title>
<style>body{{font-family:sans-serif;padding:40px;background:#f3f4f6}}
.error{{background:#fff;padding:20px;border-radius:8px;max-width:800px;margin:0 auto}}
h1{{color:#dc2626}}pre{{background:#f9fafb;padding:12px;border-radius:4px;overflow:auto}}
a{{color:#2563eb}}</style>
</head>
<body>
<div class="error">
<h1>PDF Generation Failed</h1>
<p>Screenshots were captured successfully but PDF generation failed.</p>
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

# -------------------- Helpers --------------------

def generate_pdf(stock, location, zip_code, url, utc_time, https_date, 
                 price_path, pay_path, sha_price, sha_pay, 
                 rfc_price, rfc_pay, debug_info):
    """Generate PDF report with screenshots side by side"""
    try:
        if price_path:
            tmpdir = os.path.dirname(price_path)
        elif pay_path:
            tmpdir = os.path.dirname(pay_path)
        else:
            tmpdir = tempfile.mkdtemp(prefix=f"cw-{stock}-")
        
        pdf_path = os.path.join(tmpdir, f"cw_{stock}_report.pdf")
        print(f"📄 Generating PDF at: {pdf_path}")
        
        doc = SimpleDocTemplate(pdf_path, pagesize=letter,
                               leftMargin=0.5*inch, rightMargin=0.5*inch,
                               topMargin=0.5*inch, bottomMargin=0.5*inch)
        
        story = []
        styles = getSampleStyleSheet()
        
        title_style = ParagraphStyle(
            'CustomTitle',
            parent=styles['Heading1'],
            fontSize=18,
            textColor=colors.HexColor('#003087'),
            spaceAfter=12,
            alignment=TA_CENTER
        )
        story.append(Paragraph("Camping World Compliance Capture Report", title_style))
        story.append(Spacer(1, 0.2*inch))
        
        meta_data = [
            ['Stock Number:', stock],
            ['Location:', f"{location} (ZIP: {zip_code})"],
            ['URL:', url],
            ['Capture Time (UTC):', utc_time],
            ['HTTPS Date:', https_date or 'N/A'],
        ]
        
        meta_table = Table(meta_data, colWidths=[2*inch, 5*inch])
        meta_table.setStyle(TableStyle([
            ('BACKGROUND', (0, 0), (0, -1), colors.HexColor('#f3f4f6')),
            ('TEXTCOLOR', (0, 0), (-1, -1), colors.black),
            ('ALIGN', (0, 0), (0, -1), 'RIGHT'),
            ('FONTNAME', (0, 0), (0, -1), 'Helvetica-Bold'),
            ('FONTSIZE', (0, 0), (-1, -1), 9),
            ('GRID', (0, 0), (-1, -1), 0.5, colors.grey),
            ('VALIGN', (0, 0), (-1, -1), 'MIDDLE'),
        ]))
        story.append(meta_table)
        story.append(Spacer(1, 0.2*inch))
        
        if rfc_price or rfc_pay:
            story.append(Paragraph("Cryptographic Timestamps (RFC 3161)", styles['Heading2']))
            ts_data = []
            if rfc_price:
                ts_data.append(['Price Disclosure:', f"{rfc_price['timestamp']} | TSA: {rfc_price['tsa']}"])
            if rfc_pay:
                ts_data.append(['Payment Disclosure:', f"{rfc_pay['timestamp']} | TSA: {rfc_pay['tsa']}"])
            
            ts_table = Table(ts_data, colWidths=[2*inch, 5*inch])
            ts_table.setStyle(TableStyle([
                ('BACKGROUND', (0, 0), (-1, -1), colors.HexColor('#ecfdf5')),
                ('TEXTCOLOR', (0, 0), (-1, -1), colors.black),
                ('FONTNAME', (0, 0), (0, -1), 'Helvetica-Bold'),
                ('FONTSIZE', (0, 0), (-1, -1), 8),
                ('GRID', (0, 0), (-1, -1), 0.5, colors.HexColor('#10b981')),
            ]))
            story.append(ts_table)
            story.append(Spacer(1, 0.2*inch))
        
        story.append(Paragraph("Captured Disclosures", styles['Heading2']))
        story.append(Spacer(1, 0.1*inch))
        
        # Price Disclosure (Top)
        if price_path and os.path.exists(price_path):
            try:
                story.append(Paragraph("<b>Price Disclosure</b>", styles['Normal']))
                story.append(Spacer(1, 0.05*inch))
                
                img = PILImage.open(price_path)
                # Full width for better readability
                img_width = 7*inch
                aspect = img.height / img.width
                target_height = img_width * aspect
                
                # Limit height to fit on page
                if target_height > 3.5*inch:
                    target_height = 3.5*inch
                    img_width = target_height / aspect
                
                img_obj = Image(price_path, width=img_width, height=target_height)
                
                # Center the image
                img_table = Table([[img_obj]], colWidths=[7*inch])
                img_table.setStyle(TableStyle([
                    ('ALIGN', (0, 0), (0, 0), 'CENTER'),
                ]))
                story.append(img_table)
                story.append(Spacer(1, 0.15*inch))
            except Exception as e:
                print(f"⚠ Error processing price image: {e}")
                story.append(Paragraph("Price disclosure available but could not render", styles['Normal']))
                story.append(Spacer(1, 0.15*inch))
        else:
            story.append(Paragraph("<b>Price Disclosure</b>", styles['Normal']))
            story.append(Spacer(1, 0.05*inch))
            story.append(Paragraph("Price disclosure not available", styles['Normal']))
            story.append(Spacer(1, 0.15*inch))
        
        # Payment Disclosure (Bottom)
        if pay_path and os.path.exists(pay_path):
            try:
                story.append(Paragraph("<b>Payment Disclosure</b>", styles['Normal']))
                story.append(Spacer(1, 0.05*inch))
                
                img = PILImage.open(pay_path)
                img_width = 7*inch
                aspect = img.height / img.width
                target_height = img_width * aspect
                
                if target_height > 3.5*inch:
                    target_height = 3.5*inch
                    img_width = target_height / aspect
                
                img_obj = Image(pay_path, width=img_width, height=target_height)
                
                img_table = Table([[img_obj]], colWidths=[7*inch])
                img_table.setStyle(TableStyle([
                    ('ALIGN', (0, 0), (0, 0), 'CENTER'),
                ]))
                story.append(img_table)
                story.append(Spacer(1, 0.15*inch))
            except Exception as e:
                print(f"⚠ Error processing payment image: {e}")
                story.append(Paragraph("Payment disclosure available but could not render", styles['Normal']))
                story.append(Spacer(1, 0.15*inch))
        else:
            story.append(Paragraph("<b>Payment Disclosure</b>", styles['Normal']))
            story.append(Spacer(1, 0.05*inch))
            story.append(Paragraph("Payment disclosure not available", styles['Normal']))
            story.append(Spacer(1, 0.15*inch))
        
        story.append(Paragraph("SHA-256 Verification Hashes", styles['Heading2']))
        hash_data = [
            ['Price Disclosure:', sha_price],
            ['Payment Disclosure:', sha_pay],
        ]
        hash_table = Table(hash_data, colWidths=[2*inch, 5*inch])
        hash_table.setStyle(TableStyle([
            ('FONTNAME', (0, 0), (0, -1), 'Helvetica-Bold'),
            ('FONTNAME', (1, 0), (1, -1), 'Courier'),
            ('FONTSIZE', (0, 0), (-1, -1), 7),
            ('GRID', (0, 0), (-1, -1), 0.5, colors.grey),
        ]))
        story.append(hash_table)
        
        doc.build(story)
        print(f"✓ PDF generated successfully: {pdf_path} ({os.path.getsize(pdf_path)} bytes)")
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
    """Enhanced tooltip triggering with multiple fallback strategies."""
    debug = []
    debug.append(f"Attempting to trigger {tooltip_name} tooltip for label: '{label_text}'")
    
    try:
        page.wait_for_timeout(1500)
        
        all_labels = page.locator(f"text={label_text}").all()
        debug.append(f"Found {len(all_labels)} instances of '{label_text}'")
        
        if len(all_labels) == 0:
            debug.append(f"❌ No instances found - element may not exist on page")
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
                    debug.append(f"    No icon found, hovering label as fallback...")
                    try:
                        label.hover(timeout=2000, force=True)
                        page.wait_for_timeout(1200)
                        debug.append(f"    ✓ Hovered label")
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
                        
                        console.log('JS: Found', labels.length, 'label elements');
                        
                        for (const label of labels) {{
                            const parent = label.parentElement;
                            if (!parent) continue;
                            
                            const svg = parent.querySelector('svg');
                            if (svg) {{
                                console.log('JS: Found SVG, triggering events');
                                svg.scrollIntoView({{behavior: 'smooth', block: 'center'}});
                                
                                setTimeout(() => {{
                                    ['mouseenter', 'mouseover', 'mousemove', 'click'].forEach(eventType => {{
                                        svg.dispatchEvent(new MouseEvent(eventType, {{
                                            bubbles: true,
                                            cancelable: true,
                                            view: window
                                        }}));
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

def do_capture(stock, zip_code, location_name, latitude, longitude):
    url = f"https://rv.campingworld.com/rv/{stock}"
    
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
    all_debug.append(f"URL: {url}")
    all_debug.append(f"Location: {location_name} (ZIP: {zip_code})")
    all_debug.append(f"Coordinates: {latitude}, {longitude}")

    print(f"🚀 Starting capture: {url} (ZIP: {zip_code}, Location: {location_name})")
    
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
            page.goto(url, wait_until="domcontentloaded", timeout=60_000)
            
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
            
            all_debug.append("\n--- Capturing Price Tooltip ---")
            success, debug_info = find_and_trigger_tooltip(page, "Total Price", "price")
            all_debug.append(debug_info)
            
            if success:
                try:
                    page.screenshot(path=price_png, full_page=True)
                    size = os.path.getsize(price_png)
                    all_debug.append(f"✓ Price screenshot saved: {size} bytes")
                    print(f"✓ Price screenshot: {size} bytes")
                except Exception as e:
                    all_debug.append(f"❌ Price screenshot failed: {e}")
                    price_png = None
            else:
                price_png = None
            
            page.wait_for_timeout(1000)
            
            all_debug.append("\n--- Capturing Payment Tooltip ---")
            success, debug_info = find_and_trigger_tooltip(page, "Est. Payment", "payment")
            all_debug.append(debug_info)
            
            if success:
                try:
                    page.screenshot(path=pay_png, full_page=True)
                    size = os.path.getsize(pay_png)
                    all_debug.append(f"✓ Payment screenshot saved: {size} bytes")
                    print(f"✓ Payment screenshot: {size} bytes")
                except Exception as e:
                    all_debug.append(f"❌ Payment screenshot failed: {e}")
                    pay_png = None
            else:
                pay_png = None
            
            browser.close()
            all_debug.append("\n✓ Browser closed")
    
    except Exception as e:
        all_debug.append(f"\n❌ CRITICAL ERROR: {str(e)}")
        all_debug.append(traceback.format_exc())
        print(f"❌ Critical error in do_capture: {e}")
        traceback.print_exc()
    
    debug_output = "\n".join(all_debug)
    return price_png, pay_png, url, debug_output

# -------------------- Entrypoint --------------------

if __name__ == "__main__":
    app.run(host="0.0.0.0", port=PORT, debug=False)
