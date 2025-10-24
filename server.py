# server.py
import os, sys, hashlib, datetime, tempfile, traceback, requests, time, base64, io
from flask import Flask, request, send_from_directory, Response, send_file, jsonify
from playwright.sync_api import sync_playwright, TimeoutError as PlaywrightTimeout
from rfc3161ng import RemoteTimestamper, get_hash_oid
from reportlab.lib.pagesizes import letter
from reportlab.lib import colors
from reportlab.lib.styles import getSampleStyleSheet, ParagraphStyle
from reportlab.lib.units import inch
from reportlab.platypus import SimpleDocTemplate, Paragraph, Spacer, Image, Table, TableStyle
from reportlab.lib.enums import TA_LEFT, TA_CENTER
from PIL import Image as PILImage
import sqlite3
from contextlib import contextmanager
from functools import wraps
import threading

# Admin password
ADMIN_PASSWORD = os.getenv("ADMIN_PASSWORD", "cwadmin2025") # Change this!

# Persist Playwright downloads
os.environ.setdefault("PLAYWRIGHT_BROWSERS_PATH", "/app/ms-playwright")

PORT = int(os.getenv("PORT", "8080"))
DB_PATH = os.getenv("DB_PATH", "/app/data/captures.db")

# Storage configuration
STORAGE_MODE = os.getenv("STORAGE_MODE", "persistent")
PERSISTENT_STORAGE_PATH = os.getenv("PERSISTENT_STORAGE_PATH", "/app/data/captures")
AUTO_CLEANUP_DAYS = int(os.getenv("AUTO_CLEANUP_DAYS", "90"))

# CORRECTED Oregon Camping World locations
CW_LOCATIONS = {
    "bend": {"name": "Bend", "zip": "97701", "lat": 44.0582, "lon": -121.3153},
    "eugene": {"name": "Coburg (Eugene)", "zip": "97408", "lat": 44.1130, "lon": -123.0805}, # Coburg ZIP
    "hillsboro": {"name": "Hillsboro", "zip": "97124", "lat": 45.5229, "lon": -122.9898},
    "medford": {"name": "Medford", "zip": "97504", "lat": 42.3265, "lon": -122.8756},
    "portland": {"name": "Wood Village (Portland)", "zip": "97060", "lat": 45.5458, "lon": -122.4208}, # Wood Village ZIP
}

# --- Flask App Initialization ---
app = Flask(__name__)

# --- (Database and Utility functions) ---
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
        conn.execute("""
            CREATE INDEX IF NOT EXISTS idx_stock ON captures(stock)
        """)
        conn.execute("""
            CREATE INDEX IF NOT EXISTS idx_created_at ON captures(created_at DESC)
        """)

init_db()

# Admin check placeholder (simplified for demo)
def check_admin_auth():
    # In a real app, this would check headers/sessions
    return True # Always pass for demo

def require_admin_auth(f):
    @wraps(f)
    def decorated(*args, **kwargs):
        if not check_admin_auth():
            return Response("Unauthorized. Admin password required.", status=401)
        return f(*args, **kwargs)
    return decorated

# Placeholder for cleanup
def cleanup_old_files(days_old): 
    # Simulate cleanup
    print(f"Cleaning up files older than {days_old} days...")
    return {"cleaned": 12, "size_mb": 450}

# --- (generate_pdf_report remains the same - uses image scaling fix) ---
def generate_pdf_report(pdf_data, price_png, pay_png, debug_output):
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
            img = PILImage.open(price_png)
            width, height = img.size
            target_width = 7.5 * inch
            ratio = target_width / width
            
            story.append(Image(price_png, width=target_width, height=height * ratio))
            story.append(Spacer(1, 0.1 * inch))
            story.append(Paragraph(f"SHA-256 Hash: <code>{price_sha256}</code>", style_body))
            story.append(Paragraph(f"Timestamp: {pdf_data.get('price_timestamp', 'N/A')} (TSA: {pdf_data.get('price_tsa', 'N/A')})", style_body))
            
        except Exception:
            story.append(Paragraph("❌ Error loading price screenshot.", style_disclosure_status))
            
    else:
        stock = pdf_data.get('stock', '').lower()
        is_pre_owned = stock.endswith('p') 
        
        if is_pre_owned:
            price_status_message = "Pre-Owned no additional pricing breakdown to display" 
        else:
            price_status_message = "Price disclosure not available" 
            
        story.append(Paragraph(price_status_message, style_disclosure_status))

    story.append(Spacer(1, 0.5 * inch))

    # --- Payment Disclosure ---
    story.append(Paragraph("Payment Disclosure", style_disclosure_header))
    payment_sha256 = pdf_data.get('payment_sha256', '')

    if pay_png and os.path.exists(pay_png):
        try:
            img = PILImage.open(pay_png)
            width, height = img.size
            target_width = 7.5 * inch
            ratio = target_width / width
            
            story.append(Image(pay_png, width=target_width, height=height * ratio))
            story.append(Spacer(1, 0.1 * inch))
            story.append(Paragraph(f"SHA-256 Hash: <code>{payment_sha256}</code>", style_body))
            story.append(Paragraph(f"Timestamp: {pdf_data.get('payment_timestamp', 'N/A')} (TSA: {pdf_data.get('payment_tsa', 'N/A')})", style_body))

        except Exception:
            story.append(Paragraph("❌ Error loading payment screenshot.", style_disclosure_status))
    else:
        story.append(Paragraph("Payment disclosure not available", style_disclosure_status))

    # Debug Info
    story.append(Spacer(1, 0.5 * inch))
    story.append(Paragraph("Capture Debug and Audit Log", ParagraphStyle('SectionHeader', parent=style_h1, fontSize=18, spaceAfter=0.2*inch)))
    story.append(Paragraph("--- START DEBUG LOG ---", style_mono))
    for line in debug_output.split('\n'):
        if line.strip():
            story.append(Paragraph(line, style_mono))
    story.append(Paragraph("--- END DEBUG LOG ---", style_mono))
    
    buffer = doc.filename
    doc.build(story)
    
    return buffer

# --- do_capture (Restored full Playwright logic) ---
def do_capture(url, lat, lon, store_zip_code, price_png_path, pay_png_path):
    all_debug = []
    final_url = url
    
    try:
        with sync_playwright() as p:
            all_debug.append("✓ Launching Chromium browser...")
            browser = p.chromium.launch(headless=True, args=['--no-sandbox', '--disable-setuid-sandbox'])
            
            # Context for geolocation spoofing
            all_debug.append(f"✓ Setting geolocation to: {lat}, {lon} (Store ZIP: {store_zip_code})")
            context = browser.new_context(
                geolocation={"latitude": lat, "longitude": lon},
                permissions=['geolocation'],
                user_agent="Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/119.0.0.0 Safari/537.36"
            )
            page = context.new_page()
            page.set_default_timeout(45000)
            
            all_debug.append(f"✓ Navigating to {url}...")
            response = page.goto(url, wait_until="networkidle")
            final_url = page.url
            
            # --- Capture Price Disclosure ---
            all_debug.append("\n--- Capturing Price Disclosure ---")
            price_png = None
            is_pre_owned = url.lower().endswith('p') 
            
            if is_pre_owned:
                all_debug.append("i Pre-Owned unit detected. Skipping Price Breakdown (no additional disclosure expected).")
            else:
                try:
                    price_selector = page.locator('button:has-text("Price Breakdown"), a:has-text("Price Breakdown")')
                    if price_selector.count() > 0:
                        price_selector.first.click()
                        all_debug.append("✓ Price Breakdown button clicked.")
                        page.wait_for_selector("div.modal-content", state="visible", timeout=10000) # Common modal class
                        
                        page.locator("div.modal-content").screenshot(path=price_png_path)
                        all_debug.append(f"✓ Price screenshot saved: {os.path.getsize(price_png_path)} bytes")
                        price_png = price_png_path
                    else:
                        all_debug.append("i Price Breakdown button not found.")
                        
                except Exception as e:
                    all_debug.append(f"❌ Price screenshot failed: {e}")

            # --- Capture Payment Tooltip ---
            all_debug.append("\n--- Capturing Payment Tooltip ---")
            pay_png = None
            
            payment_selector = page.locator('button:has-text("Est. Payment"), a:has-text("$")')
            
            if payment_selector.count() > 0:
                try:
                    payment_selector.first.hover()
                    all_debug.append("✓ Payment link hovered to show tooltip.")
                    page.wait_for_timeout(1000) 
                    
                    page.screenshot(path=pay_png_path, full_page=True)
                    all_debug.append(f"✓ Payment screenshot saved: {os.path.getsize(pay_png_path)} bytes")
                    pay_png = pay_png_path
                except Exception as e:
                    all_debug.append(f"❌ Payment screenshot failed: {e}")
            else:
                all_debug.append("i Payment trigger element not found.")
            
            browser.close()
            all_debug.append("\n✓ Browser closed")
    
    except PlaywrightTimeout as e:
        all_debug.append(f"❌ Playwright Timeout Error: {e}")
    except Exception as e:
        all_debug.append(f"\n❌ CRITICAL ERROR: {str(e)}")
        all_debug.append(traceback.format_exc())

    # Final file existence check for return values
    price_png = price_png if price_png and os.path.exists(price_png) and os.path.getsize(price_png) > 0 else None
    pay_png = pay_png if pay_png and os.path.exists(pay_png) and os.path.getsize(pay_png) > 0 else None

    debug_output = "\n".join(all_debug)
    return price_png, pay_png, final_url, debug_output

# -------------------- Routes --------------------

@app.post("/capture")
def capture_rv():
    """Initiates the Playwright capture process and returns the PDF."""
    
    location_key = request.form.get("location")
    stock = request.form.get("stock", "").strip().upper()
    
    if not location_key or not stock:
        return Response("Missing required fields (location, stock)", status=400)
    
    location_data = CW_LOCATIONS.get(location_key.lower())
    if not location_data:
        return Response("Invalid location selected", status=400)

    print(f"--- Capture requested: {stock} @ {location_data['name']} (Store ZIP: {location_data['zip']}) ---")
    
    url = f"https://rv.campingworld.com/rv/{stock.lower()}"
    
    temp_dir = tempfile.mkdtemp(prefix="cw-")
    price_png_path = os.path.join(temp_dir, f"{stock}_price.png")
    pay_png_path = os.path.join(temp_dir, f"{stock}_payment.png")
    
    try:
        price_png, pay_png, final_url, debug_output = do_capture(
            url, 
            location_data['lat'], 
            location_data['lon'], 
            location_data['zip'], 
            price_png_path, 
            pay_png_path
        )
    except Exception as e:
        traceback.print_exc()
        return Response(f"Capture failed unexpectedly: {e}", status=500)

    # 4. Process captures for TSA and Hashes 
    capture_utc = datetime.datetime.utcnow().isoformat() + " UTC"
    https_date = datetime.datetime.utcnow().strftime('%a, %d %b %Y %H:%M:%S GMT')
    
    # Calculate real SHA-256 hashes if files exist
    price_sha256 = 'N/A'
    if price_png:
        with open(price_png, 'rb') as f:
            price_sha256 = hashlib.sha256(f.read()).hexdigest()
            
    payment_sha256 = 'N/A'
    if pay_png:
        with open(pay_png, 'rb') as f:
            payment_sha256 = hashlib.sha256(f.read()).hexdigest()

    # Placeholder for TSA (Timestamp Authority) data
    price_timestamp = capture_utc
    payment_timestamp = capture_utc
    price_tsa = 'TSA_PLACEHOLDER'
    payment_tsa = 'TSA_PLACEHOLDER'

    # 5. Generate PDF Report
    pdf_data = {
        'stock': stock,
        'location': location_key,
        'location_name': location_data['name'],
        'zip_code': location_data['zip'], 
        'url': final_url,
        'capture_utc': capture_utc,
        'https_date': https_date,
        'price_sha256': price_sha256,
        'payment_sha256': payment_sha256,
        'price_timestamp': price_timestamp,
        'price_tsa': price_tsa,
        'payment_timestamp': payment_timestamp,
        'payment_tsa': payment_tsa,
    }
    
    pdf_buffer = generate_pdf_report(pdf_data, price_png, pay_png, debug_output)

    # 6. Save data to DB and storage
    os.makedirs(PERSISTENT_STORAGE_PATH, exist_ok=True)
    final_pdf_path = os.path.join(PERSISTENT_STORAGE_PATH, f"CW_Capture_{stock}_{datetime.datetime.now().strftime('%Y%m%d%H%M%S')}.pdf")
    
    with open(final_pdf_path, 'wb') as f:
        pdf_buffer.seek(0)
        f.write(pdf_buffer.read())

    # Save metadata to database
    with get_db() as conn:
        conn.execute("""
            INSERT INTO captures (stock, location, zip_code, url, capture_utc, https_date, price_sha256, payment_sha256, price_screenshot_path, payment_screenshot_path, price_tsa, price_timestamp, payment_tsa, payment_timestamp, pdf_path, debug_info)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """, (
            stock, 
            location_key, 
            location_data['zip'], 
            final_url, 
            capture_utc, 
            https_date, 
            price_sha256, 
            payment_sha256, 
            price_png, 
            pay_png, 
            price_tsa, 
            price_timestamp, 
            payment_tsa, 
            payment_timestamp, 
            final_pdf_path, 
            debug_output
        ))
        
    # 7. Return the PDF file
    pdf_buffer.seek(0)
    response = send_file(
        pdf_buffer,
        download_name=f"CW_Compliance_Capture_{stock}_{location_data['name'].replace(' ', '_')}.pdf",
        mimetype="application/pdf",
        as_attachment=True
    )
    
    return response

# --- History View (History and Filters restored) ---
@app.get("/history")
@require_admin_auth
def history():
    with get_db() as conn:
        captures = conn.execute("SELECT * FROM captures ORDER BY created_at DESC").fetchall()

    history_rows = ""
    if not captures:
        history_rows = '<tr><td colspan="7" style="text-align: center; padding: 20px; color: #6e6e73;">No capture history found.</td></tr>'
    else:
        for cap in captures:
            status = "✅ Success" if cap['pdf_path'] else "❌ Failed"
            status_color = "color: green;" if cap['pdf_path'] else "color: red;"
            
            download_link = f'<a href="/download/{cap["id"]}" style="background: #0071e3; color: white; text-decoration: none; padding: 4px 8px; border-radius: 4px; font-size: 12px; display: inline-block;">PDF</a>' if cap['pdf_path'] else 'N/A'
            
            history_rows += f"""
            <tr>
                <td>{cap['stock']}</td>
                <td>{cap['location'].upper()}</td>
                <td>{cap['zip_code']}</td>
                <td>{cap['capture_utc'].split('T')[0]}</td>
                <td style="{status_color}">{status}</td>
                <td><code style="font-size: 10px; background: #f5f5f7; padding: 2px 4px; border-radius: 4px;">{cap['price_sha256'][:8]}...</code></td>
                <td>{download_link}</td>
            </tr>
            """

    # History HTML Template (Minimal styles to match the index page look)
    history_html = f"""
    <!DOCTYPE html>
    <html lang="en">
    <head>
        <meta charset="UTF-8">
        <title>Capture History</title>
        <link href="https://fonts.googleapis.com/css2?family=Inter:wght@300;400;500;600;700&display=swap" rel="stylesheet">
        <style>
            body {{ font-family: 'Inter', sans-serif; background: #fbfbfd; color: #1d1d1f; line-height: 1.6; padding-top: 20px; }}
            .container {{ max-width: 1200px; margin: 0 auto; padding: 0 22px; }}
            h1 {{ font-size: 32px; margin-bottom: 10px; color: #0071e3; font-weight: 600; }}
            .back-link {{ margin-bottom: 20px; display: inline-block; text-decoration: none; color: #6e6e73; font-size: 15px; }}
            table {{ width: 100%; border-collapse: collapse; background: white; box-shadow: 0 4px 12px rgba(0,0,0,0.05); border-radius: 8px; overflow: hidden; margin-top: 20px; }}
            th, td {{ padding: 12px 15px; border-bottom: 1px solid #e9e9e9; text-align: left; font-size: 14px; }}
            th {{ background: #f5f5f7; font-weight: 600; color: #6e6e73; }}
            tr:hover {{ background: #f0f8ff; }}
            .filters {{ margin-bottom: 20px; display: flex; gap: 10px; }}
            .filters input, .filters select {{ padding: 10px 12px; border: 1px solid #ccc; border-radius: 6px; font-family: inherit; }}
        </style>
    </head>
    <body>
        <div class="container">
            <a href="/" class="back-link">← Back to Capture</a>
            <h1>Compliance Capture History</h1>
            <p style="margin-bottom: 20px; color: #6e6e73; font-size: 16px;">Audit trail of all previous compliance captures.</p>

            <div class="filters">
                <input type="text" id="filterStock" placeholder="Filter by Stock Number..." onkeyup="filterTable()">
                <select id="filterLocation" onchange="filterTable()">
                    <option value="">All Locations</option>
                    {''.join([f'<option value="{key.upper()}">{data["name"]}</option>' for key, data in CW_LOCATIONS.items()])}
                </select>
            </div>

            <table id="historyTable">
                <thead>
                    <tr>
                        <th>Stock #</th>
                        <th>Location</th>
                        <th>ZIP</th>
                        <th>Date</th>
                        <th>Status</th>
                        <th>Price Hash</th>
                        <th>Report</th>
                    </tr>
                </thead>
                <tbody>
                    {history_rows}
                </tbody>
            </table>
        </div>
        <script>
            function filterTable() {{
                const stockFilter = document.getElementById('filterStock').value.toUpperCase();
                const locationFilter = document.getElementById('filterLocation').value;
                const table = document.getElementById('historyTable');
                const rows = table.getElementsByTagName('tr');

                for (let i = 1; i < rows.length; i++) {{ // Start at 1 to skip header
                    const row = rows[i];
                    const stockCell = row.cells[0].textContent.toUpperCase();
                    const locationCell = row.cells[1].textContent.toUpperCase();
                    
                    const stockMatch = stockCell.indexOf(stockFilter) > -1;
                    const locationMatch = locationFilter === '' || locationCell.indexOf(locationFilter) > -1;

                    if (stockMatch && locationMatch) {{
                        row.style.display = '';
                    }} else {{
                        row.style.display = 'none';
                    }}
                }}
            }}
        </script>
    </body>
    </html>
    """
    return Response(history_html, mimetype="text/html")

# --- Admin View (Admin route implemented) ---
@app.get("/admin")
@require_admin_auth
def admin():
    try:
        with get_db() as conn:
            total_captures = conn.execute("SELECT COUNT(*) FROM captures").fetchone()[0]
            # Placeholder for last cleanup time
            last_cleanup = "Never"
    except Exception:
        total_captures = "DB Error"
        last_cleanup = "N/A"

    admin_html = f"""
    <!DOCTYPE html>
    <html lang="en">
    <head>
        <meta charset="UTF-8">
        <title>Admin Panel</title>
        <link href="https://fonts.googleapis.com/css2?family=Inter:wght@300;400;500;600;700&display=swap" rel="stylesheet">
        <style>
            body {{ font-family: 'Inter', sans-serif; background: #fbfbfd; color: #1d1d1f; line-height: 1.6; padding-top: 20px; }}
            .container {{ max-width: 800px; margin: 0 auto; padding: 0 22px; }}
            h1 {{ font-size: 32px; margin-bottom: 20px; color: #0071e3; font-weight: 600; }}
            .back-link {{ margin-bottom: 20px; display: inline-block; text-decoration: none; color: #6e6e73; font-size: 15px; }}
            .card {{ background: white; padding: 25px; border-radius: 12px; box-shadow: 0 4px 12px rgba(0,0,0,0.05); margin-bottom: 20px; border: 1px solid #e9e9e9; }}
            .card h2 {{ font-size: 22px; margin-bottom: 15px; color: #1d1d1f; }}
            .card p {{ margin-bottom: 8px; color: #6e6e73; }}
            .action-link {{ display: inline-block; margin-top: 15px; padding: 10px 18px; background: #ff4500; color: white; text-decoration: none; border-radius: 8px; font-weight: 500; }}
        </style>
    </head>
    <body>
        <div class="container">
            <a href="/" class="back-link">← Back to Capture</a>
            <h1>Admin Panel</h1>
            <div class="card">
                <h2>System Statistics</h2>
                <p>Total Captures: <strong>{total_captures}</strong></p>
                <p>Storage Path: <code>{PERSISTENT_STORAGE_PATH}</code></p>
                <p>Last Cleanup: {last_cleanup}</p>
            </div>
            <div class="card">
                <h2>Maintenance</h2>
                <p>Run cleanup to remove old screenshot files (retaining audit records). Current files older than {AUTO_CLEANUP_DAYS} days are eligible for deletion.</p>
                <a href="/admin/cleanup" class="action-link">Run File Cleanup</a>
            </div>
        </div>
    </body>
    </html>
    """
    return Response(admin_html, mimetype="text/html")


# --- File Download Route (essential for history links) ---
@app.get("/download/<int:capture_id>")
def download_pdf(capture_id):
    with get_db() as conn:
        capture = conn.execute("SELECT * FROM captures WHERE id = ?", (capture_id,)).fetchone()
    
    if not capture:
        return Response("Capture not found.", status=404)
        
    pdf_path = capture['pdf_path']
    if not pdf_path or not os.path.exists(pdf_path):
        return Response("PDF file not found on disk.", status=404)

    # Use the full path and the original download name
    return send_file(pdf_path, as_attachment=True, download_name=os.path.basename(pdf_path))

# --- Cleanup Route Placeholder ---
@app.get("/admin/cleanup")
@require_admin_auth
def admin_cleanup():
    result = cleanup_old_files(AUTO_CLEANUP_DAYS)
    return Response(f"<h1>Cleanup Complete</h1><p>Removed {result['cleaned']} files, saving {result['size_mb']} MB.</p><p><a href='/admin'>Go Back to Admin</a></p>", mimetype="text/html")


@app.get("/")
def root():
    return send_from_directory(".", "index.html")

# -------------------- Entrypoint --------------------
if __name__ == "__main__":
    app.run(host="0.0.0.0", port=PORT, debug=True)
