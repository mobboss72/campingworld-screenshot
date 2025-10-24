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

# Persist Playwright downloads
os.environ.setdefault("PLAYWRIGHT_BROWSERS_PATH", "/app/ms-playwright")

PORT = int(os.getenv("PORT", "8080"))
DB_PATH = os.getenv("DB_PATH", "/app/data/captures.db")

# Storage configuration
STORAGE_MODE = os.getenv("STORAGE_MODE", "ephemeral")  # "ephemeral" or "persistent"
PERSISTENT_STORAGE_PATH = os.getenv("PERSISTENT_STORAGE_PATH", "/app/data/captures")
AUTO_CLEANUP_DAYS = int(os.getenv("AUTO_CLEANUP_DAYS", "7"))

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

screenshot_cache: dict[str, str] = {}

app = Flask(__name__, static_folder=None)

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

# -------------------- Cleanup Utilities --------------------

def cleanup_old_files(days_old=7):
    """Clean up screenshot files older than specified days"""
    try:
        cutoff_time = time.time() - (days_old * 24 * 60 * 60)
        cleaned_count = 0
        
        # Clean up temp directories
        temp_base = tempfile.gettempdir()
        for item in os.listdir(temp_base):
            if item.startswith("cw-"):
                item_path = os.path.join(temp_base, item)
                try:
                    if os.path.isdir(item_path):
                        # Check if directory is old enough
                        dir_mtime = os.path.getmtime(item_path)
                        if dir_mtime < cutoff_time:
                            import shutil
                            shutil.rmtree(item_path)
                            cleaned_count += 1
                            print(f"🧹 Cleaned up old directory: {item}")
                except Exception as e:
                    print(f"⚠ Could not clean {item_path}: {e}")
        
        print(f"✓ Cleanup complete: removed {cleaned_count} old directories")
        return cleaned_count
    except Exception as e:
        print(f"❌ Cleanup failed: {e}")
        return 0

@app.get("/admin/cleanup")
def admin_cleanup():
    """Manual cleanup endpoint (protect this in production!)"""
    # TODO: Add authentication here
    days = request.args.get("days", 7, type=int)
    count = cleanup_old_files(days)
    return jsonify({"cleaned": count, "days_old": days})

@app.get("/admin/storage")
def admin_storage():
    """View storage status"""
    try:
        # Count captures in database
        with get_db() as conn:
            total_captures = conn.execute("SELECT COUNT(*) as count FROM captures").fetchone()['count']
            
            # Count existing files
            existing_pdfs = conn.execute(
                "SELECT COUNT(*) as count FROM captures WHERE pdf_path IS NOT NULL"
            ).fetchone()['count']
            
            # Check which files still exist
            files_exist = 0
            files_missing = 0
            for row in conn.execute("SELECT pdf_path FROM captures WHERE pdf_path IS NOT NULL"):
                if row['pdf_path'] and os.path.exists(row['pdf_path']):
                    files_exist += 1
                else:
                    files_missing += 1
        
        # Calculate temp directory size
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
        
        temp_size_mb = temp_size / (1024 * 1024)
        
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
            }
        })
    except Exception as e:
        return jsonify({"error": str(e)}), 500

# -------------------- Routes --------------------

@app.get("/")
def root():
    return send_from_directory(".", "index.html")

@app.get("/screenshot/<sid>")
def serve_shot(sid: str):
    path = screenshot_cache.get(sid)
    if not path or not os.path.exists(path):
        return Response("Screenshot not found", status=404)
    return send_file(path, mimetype="image/png")

@app.get("/history")
def history():
    with get_db() as conn:
        captures = conn.execute("""
            SELECT id, stock, location, capture_utc, price_sha256, payment_sha256
            FROM captures
            ORDER BY created_at DESC
            LIMIT 100
        """).fetchall()
    
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
    table{width:100%;background:#fff;border-collapse:collapse;border-radius:8px;overflow:hidden;box-shadow:0 2px 8px rgba(0,0,0,0.1)}
    th{background:#2563eb;color:#fff;padding:12px;text-align:left;font-weight:600}
    td{padding:12px;border-bottom:1px solid #e5e7eb}
    tr:last-child td{border-bottom:none}
    tr:hover{background:#f9fafb}
    .view-btn{background:#2563eb;color:#fff;padding:6px 12px;border-radius:4px;text-decoration:none;font-size:13px}
    .view-btn:hover{background:#1d4ed8}
    .empty{text-align:center;padding:40px;color:#666}
  </style>
</head>
<body>
  <a href="/" class="back">← Back to Capture Tool</a>
  <h1>Capture History</h1>
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
        <td>{{capture.stock}}</td>
        <td>{{capture.location}}</td>
        <td>{{capture.capture_utc}}</td>
        <td><code style="font-size:10px">{{capture.price_sha256[:16]}}...</code></td>
        <td><code style="font-size:10px">{{capture.payment_sha256[:16]}}...</code></td>
        <td><a href="/view/{{capture.id}}" class="view-btn">View</a></td>
      </tr>
      {% endfor %}
    </tbody>
  </table>
  {% else %}
  <div class="empty">No captures yet</div>
  {% endif %}
</body>
</html>
    """, captures=captures)
    return Response(html, mimetype="text/html")

@app.get("/view/<int:capture_id>")
def view_capture(capture_id: int):
    with get_db() as conn:
        capture = conn.execute("SELECT * FROM captures WHERE id = ?", (capture_id,)).fetchone()
    
    if not capture:
        return Response("Capture not found", status=404)
    
    # Check if stored PDF exists
    if capture['pdf_path'] and os.path.exists(capture['pdf_path']):
        return send_file(capture['pdf_path'], mimetype="application/pdf", as_attachment=True,
                        download_name=f"CW_Capture_{capture['stock']}_{capture_id}.pdf")
    
    # If PDF doesn't exist, regenerate it from screenshots (if they still exist)
    price_path = capture['price_screenshot_path']
    pay_path = capture['payment_screenshot_path']
    
    if (price_path and os.path.exists(price_path)) or (pay_path and os.path.exists(pay_path)):
        print(f"📄 Regenerating PDF for capture {capture_id}")
        
        # Reconstruct RFC timestamp info if available
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

        # Get RFC 3161 timestamps (but don't fail if unavailable)
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

        # Generate PDF
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

        # Save to database (even if PDF failed)
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

        # Return PDF download if available
        if pdf_path and os.path.exists(pdf_path):
            return send_file(pdf_path, mimetype="application/pdf", as_attachment=True,
                           download_name=f"CW_Capture_{stock}_{capture_id or 'temp'}.pdf")
        else:
            # Return error page with debug info if PDF failed
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
        # Determine tmpdir from available path
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
        
        # Title
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
        
        # Metadata table
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
        
        # RFC 3161 Timestamps
        if rfc_price or rfc_pay:
            story.append(Paragraph("Cryptographic Timestamps (RFC 3161)", styles['Heading2']))
            ts_data = []
            if rfc_price:
                ts_data.append(['Price Screenshot:', f"{rfc_price['timestamp']} | TSA: {rfc_price['tsa']}"])
            if rfc_pay:
                ts_data.append(['Payment Screenshot:', f"{rfc_pay['timestamp']} | TSA: {rfc_pay['tsa']}"])
            
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
        
        # Screenshots side by side
        story.append(Paragraph("Captured Screenshots", styles['Heading2']))
        story.append(Spacer(1, 0.1*inch))
        
        images_row = []
        labels_row = []
        
        # Price screenshot
        if price_path and os.path.exists(price_path):
            try:
                # Open and resize image
                img = PILImage.open(price_path)
                img_width = 3.25*inch
                aspect = img.height / img.width
                target_height = img_width * aspect
                
                # Limit height
                if target_height > 6*inch:
                    target_height = 6*inch
                    img_width = target_height / aspect
                
                img_obj = Image(price_path, width=img_width, height=target_height)
                images_row.append(img_obj)
                labels_row.append(Paragraph("<b>Price Tooltip</b>", styles['Normal']))
            except Exception as e:
                print(f"⚠ Error processing price image: {e}")
                images_row.append(Paragraph("Price screenshot\navailable but\ncould not render", styles['Normal']))
                labels_row.append(Paragraph("<b>Price Tooltip</b>", styles['Normal']))
        else:
            images_row.append(Paragraph("Price screenshot\nnot available", styles['Normal']))
            labels_row.append(Paragraph("<b>Price Tooltip</b>", styles['Normal']))
        
        # Payment screenshot
        if pay_path and os.path.exists(pay_path):
            try:
                img = PILImage.open(pay_path)
                img_width = 3.25*inch
                aspect = img.height / img.width
                target_height = img_width * aspect
                
                if target_height > 6*inch:
                    target_height = 6*inch
                    img_width = target_height / aspect
                
                img_obj = Image(pay_path, width=img_width, height=target_height)
                images_row.append(img_obj)
                labels_row.append(Paragraph("<b>Payment Tooltip</b>", styles['Normal']))
            except Exception as e:
                print(f"⚠ Error processing payment image: {e}")
                images_row.append(Paragraph("Payment screenshot\navailable but\ncould not render", styles['Normal']))
                labels_row.append(Paragraph("<b>Payment Tooltip</b>", styles['Normal']))
        else:
            images_row.append(Paragraph("Payment screenshot\nnot available", styles['Normal']))
            labels_row.append(Paragraph("<b>Payment Tooltip</b>", styles['Normal']))
        
        # Add labels then images
        label_table = Table([labels_row], colWidths=[3.5*inch, 3.5*inch])
        label_table.setStyle(TableStyle([
            ('ALIGN', (0, 0), (-1, -1), 'CENTER'),
            ('FONTSIZE', (0, 0), (-1, -1), 11),
        ]))
        story.append(label_table)
        story.append(Spacer(1, 0.1*inch))
        
        img_table = Table([images_row], colWidths=[3.5*inch, 3.5*inch])
        img_table.setStyle(TableStyle([
            ('ALIGN', (0, 0), (-1, -1), 'CENTER'),
            ('VALIGN', (0, 0), (-1, -1), 'TOP'),
        ]))
        story.append(img_table)
        story.append(Spacer(1, 0.2*inch))
        
        # SHA-256 Hashes
        story.append(Paragraph("SHA-256 Verification Hashes", styles['Heading2']))
        hash_data = [
            ['Price Screenshot:', sha_price],
            ['Payment Screenshot:', sha_pay],
        ]
        hash_table = Table(hash_data, colWidths=[2*inch, 5*inch])
        hash_table.setStyle(TableStyle([
            ('FONTNAME', (0, 0), (0, -1), 'Helvetica-Bold'),
            ('FONTNAME', (1, 0), (1, -1), 'Courier'),
            ('FONTSIZE', (0, 0), (-1, -1), 7),
            ('GRID', (0, 0), (-1, -1), 0.5, colors.grey),
        ]))
        story.append(hash_table)
        
        # Build PDF
        doc.build(story)
        print(f"✓ PDF generated successfully: {pdf_path} ({os.path.getsize(pdf_path)} bytes)")
        return pdf_path
        
    except Exception as e:
        print(f"❌ PDF generation failed: {e}")
        traceback.print_exc()
        return None

def sha256_file(path: str) -> str:
    if not path or not os.path.exists(path): return "N/A"
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(1024*1024), b""): h.update(chunk)
    return h.hexdigest()

def https_date() -> str | None:
    try:
        r = requests.head("https://cloudflare.com", timeout=8)
        return r.headers.get("Date")
    except Exception:
        return None

def get_rfc3161_timestamp(file_path: str) -> dict | None:
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

def find_and_trigger_tooltip(page, label_text: str, tooltip_name: str):
    """Enhanced tooltip triggering with multiple fallback strategies."""
    debug = []
    debug.append(f"Attempting to trigger {tooltip_name} tooltip for label: '{label_text}'")
    
    try:
        # Wait a bit for page to stabilize
        page.wait_for_timeout(1500)
        
        # Try to find all instances of the label
        all_labels = page.locator(f"text={label_text}").all()
        debug.append(f"Found {len(all_labels)} instances of '{label_text}'")
        
        if len(all_labels) == 0:
            debug.append(f"❌ No instances found - element may not exist on page")
            return False, "\n".join(debug)
        
        # Try each visible instance
        success = False
        for idx, label in enumerate(all_labels):
            try:
                # Check if this instance is visible
                is_visible = label.is_visible(timeout=1000)
                if not is_visible:
                    debug.append(f"  Instance {idx}: not visible, skipping")
                    continue
                
                debug.append(f"  Instance {idx}: visible, attempting trigger")
                
                # Scroll to this label
                label.scroll_into_view_if_needed(timeout=3000)
                page.wait_for_timeout(800)
                
                # Try multiple strategies to find the info icon
                icon_found = False
                
                # Strategy 1: Parent element contains SVG
                try:
                    parent = label.locator("xpath=..").first
                    svg_icons = parent.locator("svg.MuiSvgIcon-root").all()
                    
                    debug.append(f"    Found {len(svg_icons)} SVG icons in parent")
                    
                    for svg_idx, svg_icon in enumerate(svg_icons):
                        try:
                            if svg_icon.is_visible(timeout=500):
                                debug.append(f"    Attempting to click SVG icon {svg_idx}...")
                                
                                # Try regular click first
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
                
                # Strategy 2: Look for info icon with specific attributes
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
                
                # Strategy 3: Hover the label itself as last resort
                if not icon_found:
                    debug.append(f"    No icon found, hovering label as fallback...")
                    try:
                        label.hover(timeout=2000, force=True)
                        page.wait_for_timeout(1200)
                        debug.append(f"    ✓ Hovered label")
                    except Exception as e:
                        debug.append(f"    Hover failed: {str(e)[:100]}")
                
                # Wait for tooltip to appear with multiple selectors
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
                                page.wait_for_timeout(1000)  # Extra wait for animation
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
        
        # Final JavaScript fallback if nothing worked
        if not success:
            debug.append("⚠ All standard methods failed, trying JavaScript injection...")
            try:
                result = page.evaluate(f"""
                    () => {{
                        // Find all elements containing the label text
                        const labels = Array.from(document.querySelectorAll('*'))
                            .filter(el => el.textContent.trim() === '{label_text}');
                        
                        console.log('JS: Found', labels.length, 'label elements');
                        
                        for (const label of labels) {{
                            // Look for SVG in parent or siblings
                            const parent = label.parentElement;
                            if (!parent) continue;
                            
                            const svg = parent.querySelector('svg');
                            if (svg) {{
                                console.log('JS: Found SVG, triggering events');
                                
                                // Scroll into view
                                svg.scrollIntoView({{behavior: 'smooth', block: 'center'}});
                                
                                // Wait a bit
                                setTimeout(() => {{
                                    // Trigger all possible mouse events
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
                    
                    # Check for tooltip again
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

def do_capture(stock: str, zip_code: str, location_name: str, latitude: float, longitude: float) -> tuple[str | None, str | None, str, str]:
    url = f"https://rv.campingworld.com/rv/{stock}"
    
    # Choose storage location based on mode
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
