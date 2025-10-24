#!/usr/bin/env python3
import os
import time
import sqlite3
import hashlib
import tempfile
import traceback
import requests
from datetime import datetime, timezone
from functools import wraps
from contextlib import contextmanager

from flask import Flask, render_template_string, request, send_file, Response, jsonify, redirect, url_for
from playwright.sync_api import sync_playwright
from PIL import Image
from reportlab.pdfgen import canvas
from reportlab.lib.pagesizes import letter
from reportlab.lib.utils import ImageReader
from rfc3161ng import RemoteTimestamper, get_timestamp, get_hash_oid

app = Flask(__name__)

# Configuration
DB_PATH = os.getenv("DB_PATH", "/app/data/captures.db")
STORAGE_MODE = os.getenv("STORAGE_MODE", "ephemeral")
PERSISTENT_STORAGE_PATH = "/app/data"
AUTO_CLEANUP_DAYS = int(os.getenv("AUTO_CLEANUP_DAYS", "7"))
ADMIN_TOKEN = os.getenv("ADMIN_TOKEN", "")

# Oregon locations with ZIP codes and GPS coordinates
OREGON_LOCATIONS = {
    "bend": {
        "name": "Bend",
        "zip": "97701",
        "latitude": 44.0582,
        "longitude": -121.3153
    },
    "eugene": {
        "name": "Eugene",
        "zip": "97402",
        "latitude": 44.0521,
        "longitude": -123.0868
    },
    "hillsboro": {
        "name": "Hillsboro",
        "zip": "97124",
        "latitude": 45.5229,
        "longitude": -122.9900
    },
    "medford": {
        "name": "Medford",
        "zip": "97504",
        "latitude": 42.3265,
        "longitude": -122.8756
    },
    "portland": {
        "name": "Portland",
        "zip": "97201",
        "latitude": 45.5152,
        "longitude": -122.6784
    }
}

def init_db():
    """Initialize the database"""
    os.makedirs(os.path.dirname(DB_PATH), exist_ok=True)
    conn = sqlite3.connect(DB_PATH, timeout=30)
    conn.execute("PRAGMA journal_mode=WAL")
    
    conn.execute("""
        CREATE TABLE IF NOT EXISTS captures (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            stock TEXT NOT NULL,
            location TEXT NOT NULL,
            zip_code TEXT NOT NULL,
            url TEXT NOT NULL,
            capture_utc TEXT NOT NULL,
            https_date TEXT,
            price_screenshot_path TEXT,
            price_sha256 TEXT,
            price_tsa TEXT,
            price_timestamp TEXT,
            payment_screenshot_path TEXT,
            payment_sha256 TEXT,
            payment_tsa TEXT,
            payment_timestamp TEXT,
            pdf_path TEXT,
            debug_info TEXT,
            created_at DATETIME DEFAULT CURRENT_TIMESTAMP
        )
    """)
    conn.commit()
    conn.close()

@contextmanager
def get_db():
    """Database connection context manager"""
    conn = sqlite3.connect(DB_PATH, timeout=30)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    try:
        yield conn
        conn.commit()
    except Exception as e:
        conn.rollback()
        raise e
    finally:
        conn.close()

def require_admin_auth(f):
    """Decorator for admin authentication"""
    @wraps(f)
    def decorated_function(*args, **kwargs):
        if not ADMIN_TOKEN:
            return Response("Admin endpoints disabled", status=403)
        
        auth_header = request.headers.get("Authorization", "")
        if not auth_header.startswith("Bearer ") or auth_header[7:] != ADMIN_TOKEN:
            return Response("Unauthorized", status=401)
        
        return f(*args, **kwargs)
    return decorated_function

def cleanup_old_files(days=7):
    """Clean up old files"""
    if STORAGE_MODE != "persistent":
        return {"cleaned": 0, "size_mb": 0}
    
    cleaned = 0
    size_cleaned = 0
    cutoff = time.time() - (days * 24 * 60 * 60)
    
    try:
        if os.path.exists(PERSISTENT_STORAGE_PATH):
            for item in os.listdir(PERSISTENT_STORAGE_PATH):
                if item.startswith("cw-") and "-" in item:
                    try:
                        parts = item.split("-")
                        if len(parts) >= 3:
                            timestamp = int(parts[-1])
                            if timestamp < cutoff:
                                item_path = os.path.join(PERSISTENT_STORAGE_PATH, item)
                                if os.path.isdir(item_path):
                                    for root, dirs, files in os.walk(item_path):
                                        for f in files:
                                            fp = os.path.join(root, f)
                                            size_cleaned += os.path.getsize(fp)
                                            os.remove(fp)
                                    os.rmdir(item_path)
                                    cleaned += 1
                    except:
                        pass
    except Exception as e:
        print(f"Cleanup error: {e}")
    
    return {
        "cleaned": cleaned,
        "size_mb": round(size_cleaned / (1024 * 1024), 2)
    }

def compute_sha256(filepath):
    """Compute SHA-256 hash of a file"""
    sha256_hash = hashlib.sha256()
    with open(filepath, "rb") as f:
        for byte_block in iter(lambda: f.read(4096), b""):
            sha256_hash.update(byte_block)
    return sha256_hash.hexdigest()

def get_https_date():
    """Get current date from HTTPS response header"""
    try:
        resp = requests.head("https://www.cloudflare.com", timeout=5)
        return resp.headers.get("date", "")
    except:
        return ""

def timestamp_rfc3161(filepath):
    """Get RFC 3161 timestamp for file"""
    TSAs = [
        "http://timestamp.digicert.com",
        "http://time.certum.pl",
        "http://tsa.starfieldtech.com",
        "http://timestamp.globalsign.com/tsa/r6advanced1"
    ]
    
    with open(filepath, "rb") as f:
        file_data = f.read()
    
    file_hash = hashlib.sha256(file_data).digest()
    
    for tsa_url in TSAs:
        try:
            rt = RemoteTimestamper(tsa_url, hashname="sha256")
            timestamp = rt.timestamp(data=file_hash, return_timestamp=True)
            
            if timestamp:
                # Extract timestamp info
                tst_info = timestamp.tst_info
                gen_time = tst_info.gen_time.replace(tzinfo=timezone.utc) if tst_info.gen_time else None
                
                return {
                    "timestamp": gen_time.isoformat() if gen_time else "N/A",
                    "tsa": tsa_url,
                    "cert_info": f"Policy: {tst_info.policy}"
                }
        except Exception as e:
            continue
    
    return None

def generate_pdf(stock, location, zip_code, url, utc_time, https_date, price_path, pay_path, sha_price, sha_pay, rfc_price, rfc_pay, debug_info):
    """Generate PDF report with screenshots"""
    pdf_dir = os.path.dirname(price_path) if price_path else tempfile.gettempdir()
    pdf_path = os.path.join(pdf_dir, f"CW_Capture_{stock}_{int(time.time())}.pdf")
    
    c = canvas.Canvas(pdf_path, pagesize=letter)
    width, height = letter
    
    # Header
    c.setFont("Helvetica-Bold", 16)
    c.drawString(50, height - 50, "CW RV Compliance Screenshot Capture")
    
    # Metadata
    y = height - 100
    c.setFont("Helvetica", 10)
    
    metadata = [
        f"Stock Number: {stock}",
        f"Location: {location} (ZIP: {zip_code})",
        f"URL: {url}",
        f"Capture Time (UTC): {utc_time}",
        f"HTTPS Date: {https_date or 'N/A'}"
    ]
    
    for line in metadata:
        c.drawString(50, y, line)
        y -= 15
    
    y -= 10
    
    # Screenshots side by side
    if price_path and os.path.exists(price_path):
        try:
            img = Image.open(price_path)
            img_width, img_height = img.size
            
            # Calculate dimensions for side-by-side display
            max_width = (width - 100) / 2 - 10
            max_height = 300
            
            aspect = img_width / img_height
            if img_width > max_width:
                new_width = max_width
                new_height = new_width / aspect
            else:
                new_width = img_width
                new_height = img_height
            
            if new_height > max_height:
                new_height = max_height
                new_width = new_height * aspect
            
            # Draw price screenshot
            c.drawImage(price_path, 50, y - new_height, width=new_width, height=new_height)
            
            # Draw payment screenshot if exists
            if pay_path and os.path.exists(pay_path):
                c.drawImage(pay_path, 50 + new_width + 20, y - new_height, width=new_width, height=new_height)
            
            y -= (new_height + 20)
        except Exception as e:
            print(f"Error adding images to PDF: {e}")
    
    # Verification info
    c.setFont("Helvetica-Bold", 12)
    c.drawString(50, y, "Verification Information")
    y -= 20
    
    c.setFont("Helvetica", 9)
    
    # Price screenshot hash
    if sha_price:
        c.drawString(50, y, f"Price Screenshot SHA-256: {sha_price}")
        y -= 15
        
        if rfc_price:
            c.drawString(50, y, f"  RFC 3161 Timestamp: {rfc_price.get('timestamp', 'N/A')}")
            y -= 15
            c.drawString(50, y, f"  TSA: {rfc_price.get('tsa', 'N/A')}")
            y -= 15
    
    # Payment screenshot hash
    if sha_pay:
        c.drawString(50, y, f"Payment Screenshot SHA-256: {sha_pay}")
        y -= 15
        
        if rfc_pay:
            c.drawString(50, y, f"  RFC 3161 Timestamp: {rfc_pay.get('timestamp', 'N/A')}")
            y -= 15
            c.drawString(50, y, f"  TSA: {rfc_pay.get('tsa', 'N/A')}")
            y -= 15
    
    # Save PDF
    c.save()
    
    return pdf_path

def do_capture(stock, zip_code, location_name, latitude, longitude):
    """Perform the actual screenshot capture"""
    url = f"https://rv.campingworld.com/rv/{stock}"
    
    if STORAGE_MODE == "persistent" and PERSISTENT_STORAGE_PATH:
        os.makedirs(PERSISTENT_STORAGE_PATH, exist_ok=True)
        tmpdir = os.path.join(PERSISTENT_STORAGE_PATH, f"cw-{stock}-{int(time.time())}")
        os.makedirs(tmpdir, exist_ok=True)
    else:
        tmpdir = tempfile.mkdtemp(prefix=f"cw-{stock}-")
    
    price_png = os.path.join(tmpdir, f"cw_{stock}_price.png")
    pay_png = os.path.join(tmpdir, f"cw_{stock}_payment.png")
    
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
            
            # Inject ZIP code
            page.add_init_script(f"""
                Object.defineProperty(navigator, 'userAgent', {{
                    get: () => 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36'
                }});
                
                // Store the ZIP for later use
                window.__OREGON_ZIP = '{zip_code}';
                
                // Override fetch to inject ZIP
                const originalFetch = window.fetch;
                window.fetch = function(...args) {{
                    if (args[0] && args[0].includes && args[0].includes('api')) {{
                        if (args[1] && args[1].headers) {{
                            args[1].headers['X-User-Zip'] = window.__OREGON_ZIP;
                        }}
                    }}
                    return originalFetch.apply(this, args);
                }};
            """)
            
            all_debug.append("Navigating to page...")
            page.goto(url, wait_until="domcontentloaded", timeout=60_000)
            
            try:
                page.wait_for_load_state("networkidle", timeout=30_000)
                all_debug.append("✓ Network idle reached")
            except:
                all_debug.append("⚠ Network timeout, continuing anyway")
            
            # Handle cookie consent if present
            try:
                cookie_btn = page.locator("button:has-text('Accept'), button:has-text('OK')").first
                if cookie_btn.is_visible(timeout=2000):
                    cookie_btn.click()
                    all_debug.append("✓ Cookie consent accepted")
            except:
                pass
            
            # Wait for price to load
            page.wait_for_timeout(3000)
            
            # Scroll to price section
            price_selectors = [
                "span:has-text('Total Price')",
                "div[class*='price']:has-text('$')",
                "span[class*='price']:has-text('$')",
                "[data-testid*='price']"
            ]
            
            price_found = False
            for selector in price_selectors:
                try:
                    price_elem = page.locator(selector).first
                    if price_elem.is_visible(timeout=5000):
                        price_elem.scroll_into_view_if_needed()
                        page.wait_for_timeout(1000)
                        price_found = True
                        all_debug.append(f"✓ Price element found with selector: {selector}")
                        break
                except:
                    continue
            
            if not price_found:
                all_debug.append("⚠ No price element found, will capture anyway")
            
            # Click price tooltip
            tooltip_clicked = False
            tooltip_selectors = [
                "[aria-label*='price breakdown']",
                "[class*='tooltip']:has-text('View Price')",
                "button:has-text('View Price')",
                "[data-testid='price-tooltip-trigger']",
                "svg[class*='info'], svg[class*='help']"
            ]
            
            for selector in tooltip_selectors:
                try:
                    tooltip = page.locator(selector).first
                    if tooltip.is_visible(timeout=2000):
                        tooltip.hover()
                        page.wait_for_timeout(500)
                        tooltip.click()
                        page.wait_for_timeout(2000)
                        
                        # Check if tooltip appeared
                        tooltip_content = page.locator("[role='tooltip'], [class*='tooltip'][class*='content'], [class*='popover']").first
                        if tooltip_content.is_visible(timeout=2000):
                            tooltip_clicked = True
                            all_debug.append(f"✓ Price tooltip clicked and displayed")
                            break
                except:
                    continue
            
            if not tooltip_clicked:
                all_debug.append("⚠ Could not trigger price tooltip")
            
            # Capture price screenshot
            page.screenshot(path=price_png, full_page=False)
            all_debug.append(f"✓ Price screenshot saved: {price_png}")
            
            # Close tooltip if open
            try:
                close_btn = page.locator("button[aria-label='Close'], [class*='close']").first
                if close_btn.is_visible(timeout=1000):
                    close_btn.click()
                    page.wait_for_timeout(1000)
            except:
                # Click outside
                page.mouse.click(100, 100)
                page.wait_for_timeout(1000)
            
            # Find and click payment calculator
            payment_clicked = False
            payment_selectors = [
                "button:has-text('Payment Calculator')",
                "button:has-text('Calculate Payment')",
                "[aria-label*='payment calculator']",
                "[class*='calculator']:has-text('Payment')"
            ]
            
            for selector in payment_selectors:
                try:
                    payment_btn = page.locator(selector).first
                    if payment_btn.is_visible(timeout=2000):
                        payment_btn.scroll_into_view_if_needed()
                        page.wait_for_timeout(1000)
                        payment_btn.click()
                        page.wait_for_timeout(2000)
                        
                        # Check if calculator appeared
                        calc_content = page.locator("[class*='calculator'][class*='modal'], [class*='payment'][class*='calc']").first
                        if calc_content.is_visible(timeout=2000):
                            payment_clicked = True
                            all_debug.append(f"✓ Payment calculator opened")
                            break
                except:
                    continue
            
            if not payment_clicked:
                all_debug.append("⚠ Could not open payment calculator")
            
            # Capture payment screenshot
            page.screenshot(path=pay_png, full_page=False)
            all_debug.append(f"✓ Payment screenshot saved: {pay_png}")
            
            browser.close()
            
            # Compute hashes
            sha_price = compute_sha256(price_png) if os.path.exists(price_png) else None
            sha_pay = compute_sha256(pay_png) if os.path.exists(pay_png) else None
            
            all_debug.append(f"✓ SHA-256 hashes computed")
            
            # Get timestamps
            utc_now = datetime.now(timezone.utc).isoformat()
            https_date = get_https_date()
            
            # RFC 3161 timestamps
            rfc_price = timestamp_rfc3161(price_png) if os.path.exists(price_png) else None
            rfc_pay = timestamp_rfc3161(pay_png) if os.path.exists(pay_png) else None
            
            if rfc_price:
                all_debug.append(f"✓ RFC 3161 timestamp obtained for price screenshot")
            else:
                all_debug.append("⚠ RFC 3161 timestamp unavailable for price screenshot")
            
            if rfc_pay:
                all_debug.append(f"✓ RFC 3161 timestamp obtained for payment screenshot")
            else:
                all_debug.append("⚠ RFC 3161 timestamp unavailable for payment screenshot")
            
            # Generate PDF
            pdf_path = generate_pdf(
                stock=stock,
                location=location_name,
                zip_code=zip_code,
                url=url,
                utc_time=utc_now,
                https_date=https_date,
                price_path=price_png,
                pay_path=pay_png,
                sha_price=sha_price,
                sha_pay=sha_pay,
                rfc_price=rfc_price,
                rfc_pay=rfc_pay,
                debug_info="\n".join(all_debug)
            )
            
            all_debug.append(f"✓ PDF generated: {pdf_path}")
            
            # Save to database
            with get_db() as conn:
                conn.execute("""
                    INSERT INTO captures (
                        stock, location, zip_code, url, capture_utc, https_date,
                        price_screenshot_path, price_sha256, price_tsa, price_timestamp,
                        payment_screenshot_path, payment_sha256, payment_tsa, payment_timestamp,
                        pdf_path, debug_info
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """, (
                    stock, location_name, zip_code, url, utc_now, https_date,
                    price_png if STORAGE_MODE == "persistent" else None,
                    sha_price,
                    rfc_price["tsa"] if rfc_price else None,
                    rfc_price["timestamp"] if rfc_price else None,
                    pay_png if STORAGE_MODE == "persistent" else None,
                    sha_pay,
                    rfc_pay["tsa"] if rfc_pay else None,
                    rfc_pay["timestamp"] if rfc_pay else None,
                    pdf_path if STORAGE_MODE == "persistent" else None,
                    "\n".join(all_debug)
                ))
            
            all_debug.append("✓ Capture saved to database")
            print("✅ Capture completed successfully")
            
            return True, "\n".join(all_debug), pdf_path
            
    except Exception as e:
        all_debug.append(f"❌ Critical Error: {str(e)}")
        traceback.print_exc()
        return False, "\n".join(all_debug), None

# Routes

@app.get("/")
def index():
    """Serve the index.html file"""
    with open("index.html", "r") as f:
        return Response(f.read(), mimetype="text/html")

@app.post("/capture")
def capture():
    """Handle capture request"""
    stock = request.form.get("stock", "").strip()
    location_key = request.form.get("location", "portland").lower()
    
    if not stock:
        return Response("Stock number required", status=400)
    
    if location_key not in OREGON_LOCATIONS:
        return Response("Invalid location", status=400)
    
    location_data = OREGON_LOCATIONS[location_key]
    
    success, debug_info, pdf_path = do_capture(
        stock=stock,
        zip_code=location_data["zip"],
        location_name=location_data["name"],
        latitude=location_data["latitude"],
        longitude=location_data["longitude"]
    )
    
    if success and pdf_path and os.path.exists(pdf_path):
        return send_file(
            pdf_path,
            mimetype="application/pdf",
            as_attachment=True,
            download_name=f"CW_Capture_{stock}_{int(time.time())}.pdf"
        )
    else:
        return Response(
            f"<h1>Capture Failed</h1><pre>{debug_info}</pre><br><a href='/'>Go Back</a>",
            status=500,
            mimetype="text/html"
        )

@app.get("/history")
def history():
    """View capture history"""
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
    """View or regenerate PDF for a capture"""
    with get_db() as conn:
        capture = conn.execute("SELECT * FROM captures WHERE id = ?", (capture_id,)).fetchone()
    
    if not capture:
        return Response("Capture not found", status=404)
    
    # If PDF exists, send it
    if capture['pdf_path'] and os.path.exists(capture['pdf_path']):
        return send_file(capture['pdf_path'], mimetype="application/pdf", as_attachment=True,
                        download_name=f"CW_Capture_{capture['stock']}_{capture_id}.pdf")
    
    # Try to regenerate PDF if screenshots exist
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
            print(f"Error regenerating PDF: {e}")
    
    return Response(
        f"<h1>PDF not available</h1><p>The PDF for this capture could not be found or regenerated.</p><a href='/history'>Back to History</a>",
        status=404,
        mimetype="text/html"
    )

@app.get("/admin")
def admin():
    """Admin dashboard"""
    try:
        with get_db() as conn:
            # Get statistics
            total_captures = conn.execute("SELECT COUNT(*) as count FROM captures").fetchone()['count']
            
            location_stats = conn.execute("""
                SELECT location, COUNT(*) as count 
                FROM captures 
                GROUP BY location 
                ORDER BY count DESC
            """).fetchall()
            
            recent_captures = conn.execute("""
                SELECT stock, location, capture_utc, created_at 
                FROM captures 
                ORDER BY created_at DESC 
                LIMIT 10
            """).fetchall()
            
            # Daily capture stats
            daily_stats = conn.execute("""
                SELECT DATE(created_at) as date, COUNT(*) as count 
                FROM captures 
                WHERE created_at > datetime('now', '-30 days')
                GROUP BY DATE(created_at)
                ORDER BY date DESC
            """).fetchall()
        
        # Calculate storage usage
        total_size = 0
        if os.path.exists(PERSISTENT_STORAGE_PATH):
            for item in os.listdir(PERSISTENT_STORAGE_PATH):
                if item.startswith("cw-"):
                    item_path = os.path.join(PERSISTENT_STORAGE_PATH, item)
                    if os.path.isdir(item_path):
                        for root, dirs, files in os.walk(item_path):
                            for f in files:
                                fp = os.path.join(root, f)
                                if os.path.exists(fp):
                                    total_size += os.path.getsize(fp)
        
        total_size_mb = round(total_size / (1024 * 1024), 2)
        estimated_max_mb = 90 * 3 * 5  # 90 days * 3 captures/day * 5MB per capture
        usage_percent = round((total_size_mb / estimated_max_mb) * 100, 2)
        
        html = render_template_string("""
<!doctype html>
<html>
<head>
  <meta charset="utf-8"/>
  <title>Admin Dashboard</title>
  <style>
    body{font-family:Inter,Arial,sans-serif;background:#f3f4f6;margin:0;padding:24px;color:#111}
    h1{margin:0 0 24px}
    .dashboard{display:grid;grid-template-columns:repeat(auto-fit,minmax(300px,1fr));gap:20px;margin-bottom:40px}
    .card{background:#fff;border:1px solid #e5e7eb;border-radius:12px;padding:24px;box-shadow:0 1px 3px rgba(0,0,0,0.1)}
    .card h2{font-size:18px;margin:0 0 16px;color:#374151}
    .stat{font-size:36px;font-weight:700;color:#1e40af;margin:8px 0}
    .label{font-size:13px;color:#6b7280;text-transform:uppercase;letter-spacing:0.5px}
    table{width:100%;margin-top:12px}
    th{background:#f3f4f6;padding:8px 12px;text-align:left;font-size:13px;font-weight:600;color:#374151}
    td{padding:8px 12px;font-size:14px;border-top:1px solid #e5e7eb}
    .progress-bar{width:100%;height:12px;background:#e5e7eb;border-radius:6px;overflow:hidden;margin:8px 0}
    .progress-fill{height:100%;background:#10b981;transition:width 0.3s ease}
    .warning{color:#f59e0b}
    .danger{color:#ef4444}
    .success{color:#10b981}
    .actions{margin-top:32px;display:flex;gap:12px}
    .btn{padding:10px 20px;border:none;border-radius:8px;font-weight:600;cursor:pointer;text-decoration:none;display:inline-block}
    .btn-primary{background:#2563eb;color:#fff}
    .btn-primary:hover{background:#1d4ed8}
    .btn-danger{background:#ef4444;color:#fff}
    .btn-danger:hover{background:#dc2626}
    .btn-secondary{background:#6b7280;color:#fff}
    .btn-secondary:hover{background:#4b5563}
    pre{background:#1e293b;color:#e2e8f0;padding:12px;border-radius:8px;font-size:12px;overflow-x:auto}
    .cleanup-result{margin-top:12px;padding:12px;border-radius:8px;font-size:14px}
    .cleanup-result.success{background:#d1fae5;color:#065f46}
    .cleanup-result.error{background:#fee2e2;color:#991b1b}
  </style>
</head>
<body>
  <a href="/" style="color:#2563eb;text-decoration:none;font-weight:600;margin-bottom:16px;display:inline-block">← Back to Tool</a>
  <h1>Admin Dashboard</h1>
  
  <div class="dashboard">
    <div class="card">
      <h2>Total Captures</h2>
      <div class="stat">{{total_captures}}</div>
      <div class="label">All Time</div>
    </div>
    
    <div class="card">
      <h2>Storage Usage</h2>
      <div class="stat">{{total_size_mb}} MB</div>
      <div class="label">Current Usage</div>
      <div class="progress-bar">
        <div class="progress-fill" style="width:{{usage_percent}}%"></div>
      </div>
      <p style="font-size:13px;color:#6b7280;margin:4px 0">
        {{usage_percent}}% of estimated max ({{estimated_max_mb}} MB)
      </p>
    </div>
    
    <div class="card">
      <h2>Storage Mode</h2>
      <div class="stat" style="font-size:24px">{{storage_mode}}</div>
      <div class="label">Auto-cleanup: {{cleanup_days}} days</div>
    </div>
  </div>
  
  <div class="dashboard">
    <div class="card">
      <h2>Captures by Location</h2>
      <table>
        <thead>
          <tr>
            <th>Location</th>
            <th>Count</th>
          </tr>
        </thead>
        <tbody>
          {% for stat in location_stats %}
          <tr>
            <td>{{stat.location}}</td>
            <td>{{stat.count}}</td>
          </tr>
          {% endfor %}
        </tbody>
      </table>
    </div>
    
    <div class="card">
      <h2>Recent Captures</h2>
      <table>
        <thead>
          <tr>
            <th>Stock</th>
            <th>Location</th>
            <th>Time</th>
          </tr>
        </thead>
        <tbody>
          {% for capture in recent_captures %}
          <tr>
            <td>{{capture.stock}}</td>
            <td>{{capture.location}}</td>
            <td style="font-size:12px">{{capture.created_at}}</td>
          </tr>
          {% endfor %}
        </tbody>
      </table>
    </div>
  </div>
  
  <div class="card" style="margin-top:24px">
    <h2>Daily Capture Statistics (Last 30 Days)</h2>
    <table>
      <thead>
        <tr>
          <th>Date</th>
          <th>Captures</th>
        </tr>
      </thead>
      <tbody>
        {% for stat in daily_stats[:7] %}
        <tr>
          <td>{{stat.date}}</td>
          <td>{{stat.count}}</td>
        </tr>
        {% endfor %}
      </tbody>
    </table>
  </div>
  
  <div class="card" style="margin-top:24px">
    <h2>System Information</h2>
    <pre>
Database Path: {{db_path}}
Storage Path: {{storage_path}}
Storage Mode: {{storage_mode}}
Auto Cleanup: {{cleanup_days}} days
    </pre>
    
    <div class="actions">
      <button onclick="runCleanup(7)" class="btn btn-secondary">Cleanup 7+ Days</button>
      <button onclick="runCleanup(30)" class="btn btn-secondary">Cleanup 30+ Days</button>
      <button onclick="window.location.href='/admin/storage'" class="btn btn-primary">View Storage Details</button>
    </div>
    
    <div id="cleanupResult"></div>
  </div>
  
  <script>
    async function runCleanup(days) {
      const result = document.getElementById('cleanupResult');
      result.innerHTML = '<div class="cleanup-result">Running cleanup...</div>';
      
      try {
        const response = await fetch(`/admin/cleanup?days=${days}`);
        const data = await response.json();
        
        if (response.ok) {
          result.innerHTML = `<div class="cleanup-result success">✓ Cleanup completed: Removed ${data.cleaned} directories (${data.size_mb} MB)</div>`;
        } else {
          result.innerHTML = `<div class="cleanup-result error">❌ Cleanup failed: ${data.error || 'Unknown error'}</div>`;
        }
      } catch (error) {
        result.innerHTML = `<div class="cleanup-result error">❌ Cleanup failed: ${error.message}</div>`;
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
        traceback.print_exc()
        return Response(f"Error loading dashboard: {e}", status=500)

@app.get("/admin/cleanup")
def admin_cleanup():
    """Manual cleanup endpoint - no auth for simplicity in internal tool"""
    days = request.args.get("days", AUTO_CLEANUP_DAYS, type=int)
    result = cleanup_old_files(days)
    return jsonify({"cleaned": result["cleaned"], "size_mb": result["size_mb"], "days_old": days})

@app.get("/admin/storage")
def admin_storage():
    """View storage status - no auth for simplicity in internal tool"""
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
                "directories": 0,
                "size_mb": round(temp_size_mb, 2)
            },
            "persistent_storage": {
                "directories": len([i for i in os.listdir(PERSISTENT_STORAGE_PATH) if i.startswith("cw-")]) if os.path.exists(PERSISTENT_STORAGE_PATH) else 0,
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

if __name__ == "__main__":
    # Initialize database
    init_db()
    
    # Run cleanup on startup
    if AUTO_CLEANUP_DAYS > 0:
        result = cleanup_old_files(AUTO_CLEANUP_DAYS)
        print(f"🧹 Startup cleanup: removed {result['cleaned']} old directories ({result['size_mb']} MB)")
    
    # Run Flask app
    port = int(os.getenv("PORT", 8080))
    app.run(host="0.0.0.0", port=port, debug=False)
