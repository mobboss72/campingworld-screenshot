from flask import Flask, request, render_template_string, send_file, jsonify, redirect, url_for
from playwright.async_api import async_playwright
import asyncio
import sqlite3
import hashlib
from datetime import datetime, timezone
import os
from pathlib import Path
from reportlab.lib.pagesizes import letter
from reportlab.lib.units import inch
from reportlab.pdfgen import canvas as pdf_canvas
from reportlab.lib.utils import ImageReader

app = Flask(__name__)

# Database setup
DB_PATH = "captures.db"
SCREENSHOTS_DIR = Path("screenshots")
SCREENSHOTS_DIR.mkdir(exist_ok=True)

def init_db():
    conn = sqlite3.connect(DB_PATH)
    c = conn.cursor()
    c.execute("""
        CREATE TABLE IF NOT EXISTS captures (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            stock TEXT NOT NULL,
            location TEXT NOT NULL,
            capture_utc TEXT NOT NULL,
            price_sha256 TEXT NOT NULL,
            payment_sha256 TEXT NOT NULL,
            pdf_path TEXT NOT NULL,
            screenshot1_path TEXT NOT NULL,
            screenshot2_path TEXT NOT NULL,
            is_used BOOLEAN DEFAULT 0
        )
    """)
    conn.commit()
    conn.close()

init_db()

LOCATIONS = {
    "Salem OR": {"zip": "97301", "lat": 44.9429, "lon": -123.0351},
    "Bend OR": {"zip": "97701", "lat": 44.0582, "lon": -121.3153},
    "Eugene OR": {"zip": "97402", "lat": 44.0521, "lon": -123.0868},
    "Grants Pass OR": {"zip": "97526", "lat": 42.4390, "lon": -123.3284},
    "Albany OR": {"zip": "97321", "lat": 44.6365, "lon": -123.1059}
}

async def take_screenshot(stock: str, location: str):
    """Capture screenshots with geolocation spoofing"""
    
    # Check if this is a used RV
    stock_upper = stock.upper()
    is_used = stock_upper.startswith('U') or 'USED' in stock_upper
    
    if is_used:
        return {
            'success': False,
            'is_used': True,
            'message': 'Used RV Selected - No Pricing Breakdown Needed',
            'stock': stock
        }
    
    loc_data = LOCATIONS.get(location)
    if not loc_data:
        return {'success': False, 'error': 'Invalid location'}
    
    timestamp = datetime.now(timezone.utc).isoformat()
    
    async with async_playwright() as p:
        browser = await p.chromium.launch(headless=True)
        context = await browser.new_context(
            geolocation={"latitude": loc_data["lat"], "longitude": loc_data["lon"]},
            permissions=["geolocation"],
            viewport={"width": 1920, "height": 1080}
        )
        
        page = await context.new_page()
        
        # Inject ZIP code
        await page.add_init_script(f"""
            Object.defineProperty(navigator, 'geolocation', {{
                get: () => ({{
                    getCurrentPosition: (success) => success({{
                        coords: {{
                            latitude: {loc_data["lat"]},
                            longitude: {loc_data["lon"]},
                            accuracy: 10
                        }}
                    }}),
                    watchPosition: (success) => success({{
                        coords: {{
                            latitude: {loc_data["lat"]},
                            longitude: {loc_data["lon"]},
                            accuracy: 10
                        }}
                    }})
                }})
            }});
            
            localStorage.setItem('userZip', '{loc_data["zip"]}');
            localStorage.setItem('spoofedZip', '{loc_data["zip"]}');
        """)
        
        # Navigate to RV page
        url = f"https://www.campingworld.com/rvsearch/details/{stock}"
        await page.goto(url, wait_until="networkidle", timeout=60000)
        await page.wait_for_timeout(3000)
        
        # Screenshot 1: Main pricing section
        screenshot1_path = SCREENSHOTS_DIR / f"{stock}_{timestamp.replace(':', '-')}_price.png"
        pricing_section = await page.query_selector('[data-testid="pricing-section"]')
        if pricing_section:
            await pricing_section.screenshot(path=str(screenshot1_path))
        else:
            await page.screenshot(path=str(screenshot1_path), full_page=False)
        
        # Screenshot 2: Payment breakdown with tooltip
        screenshot2_path = SCREENSHOTS_DIR / f"{stock}_{timestamp.replace(':', '-')}_payment.png"
        tooltip_button = await page.query_selector('[data-testid="payment-tooltip"]')
        if tooltip_button:
            await tooltip_button.click()
            await page.wait_for_timeout(1000)
            tooltip = await page.query_selector('[data-testid="payment-breakdown"]')
            if tooltip:
                await tooltip.screenshot(path=str(screenshot2_path))
            else:
                await page.screenshot(path=str(screenshot2_path), full_page=False)
        else:
            await page.screenshot(path=str(screenshot2_path), full_page=False)
        
        await browser.close()
    
    # Generate hashes
    with open(screenshot1_path, 'rb') as f:
        price_hash = hashlib.sha256(f.read()).hexdigest()
    with open(screenshot2_path, 'rb') as f:
        payment_hash = hashlib.sha256(f.read()).hexdigest()
    
    # Generate PDF (single page)
    pdf_path = SCREENSHOTS_DIR / f"{stock}_{timestamp.replace(':', '-')}_report.pdf"
    generate_single_page_pdf(str(pdf_path), str(screenshot1_path), str(screenshot2_path), 
                            stock, location, timestamp, price_hash, payment_hash)
    
    # Store in database
    conn = sqlite3.connect(DB_PATH)
    c = conn.cursor()
    c.execute("""
        INSERT INTO captures (stock, location, capture_utc, price_sha256, payment_sha256, 
                             pdf_path, screenshot1_path, screenshot2_path, is_used)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
    """, (stock, location, timestamp, price_hash, payment_hash, 
          str(pdf_path), str(screenshot1_path), str(screenshot2_path), 0))
    conn.commit()
    capture_id = c.lastrowid
    conn.close()
    
    return {
        'success': True,
        'capture_id': capture_id,
        'stock': stock,
        'timestamp': timestamp,
        'price_hash': price_hash[:16],
        'payment_hash': payment_hash[:16]
    }

def generate_single_page_pdf(pdf_path, img1_path, img2_path, stock, location, timestamp, hash1, hash2):
    """Generate a single-page PDF with both screenshots"""
    c = pdf_canvas.Canvas(pdf_path, pagesize=letter)
    width, height = letter
    
    # Title
    c.setFont("Helvetica-Bold", 16)
    c.drawString(1*inch, height - 0.5*inch, f"CW RV Pricing Capture - Stock #{stock}")
    
    # Metadata
    c.setFont("Helvetica", 10)
    c.drawString(1*inch, height - 0.8*inch, f"Location: {location}")
    c.drawString(1*inch, height - 1.0*inch, f"Timestamp: {timestamp}")
    c.drawString(1*inch, height - 1.2*inch, f"Price Hash: {hash1[:32]}")
    c.drawString(1*inch, height - 1.4*inch, f"Payment Hash: {hash2[:32]}")
    
    # Calculate image dimensions to fit both on one page
    max_width = width - 2*inch
    max_height_per_image = (height - 2.5*inch) / 2
    
    # Image 1
    try:
        img1 = ImageReader(img1_path)
        img1_width, img1_height = img1.getSize()
        scale1 = min(max_width/img1_width, max_height_per_image/img1_height)
        c.drawImage(img1_path, 1*inch, height - 1.8*inch - (img1_height*scale1), 
                   width=img1_width*scale1, height=img1_height*scale1)
        
        # Image 2
        img2 = ImageReader(img2_path)
        img2_width, img2_height = img2.getSize()
        scale2 = min(max_width/img2_width, max_height_per_image/img2_height)
        y_position = height - 2.0*inch - (img1_height*scale1) - (img2_height*scale2) - 0.2*inch
        c.drawImage(img2_path, 1*inch, y_position, 
                   width=img2_width*scale2, height=img2_height*scale2)
    except Exception as e:
        c.drawString(1*inch, height - 2*inch, f"Error loading images: {str(e)}")
    
    c.save()

@app.route("/", methods=["GET"])
def index():
    """Main page with admin panel and storage management"""
    conn = sqlite3.connect(DB_PATH)
    c = conn.cursor()
    c.execute("SELECT * FROM captures ORDER BY id DESC LIMIT 50")
    captures = c.fetchall()
    
    # Calculate storage
    total_size = sum(f.stat().st_size for f in SCREENSHOTS_DIR.glob("*") if f.is_file())
    total_size_mb = total_size / (1024 * 1024)
    
    conn.close()
    
    # Build captures table HTML
    captures_html = ""
    for cap in captures:
        status = "Used RV" if cap[9] else "Completed"
        captures_html += f"""
        <tr>
            <td>{cap[0]}</td>
            <td>{cap[1]}</td>
            <td>{cap[2]}</td>
            <td>{cap[3]}</td>
            <td><code>{cap[4][:16]}...</code></td>
            <td><code>{cap[5][:16]}...</code></td>
            <td>{status}</td>
            <td><a href="/view/{cap[0]}" class="view-btn">View PDF</a></td>
        </tr>
        """
    
    # Build location options
    location_options = "".join([f'<option value="{loc}">{loc}</option>' for loc in LOCATIONS.keys()])
    
    return render_template_string(HTML_TEMPLATE, 
                                 captures=captures_html, 
                                 storage_mb=f"{total_size_mb:.2f}",
                                 location_options=location_options)

@app.post("/capture")
def capture():
    """Run screenshot capture"""
    stock = request.form.get("stock", "").strip()
    location = request.form.get("location", "Salem OR")
    
    if not stock:
        return jsonify({'error': 'Stock number required'}), 400
    
    result = asyncio.run(take_screenshot(stock, location))
    
    if result.get('is_used'):
        return render_template_string(USED_RV_TEMPLATE, stock=stock, message=result['message'])
    
    if result.get('success'):
        return render_template_string(SUCCESS_TEMPLATE, result=result)
    else:
        return jsonify(result), 400

@app.get("/view/<int:capture_id>")
def view_capture(capture_id):
    """View PDF for a capture"""
    conn = sqlite3.connect(DB_PATH)
    c = conn.cursor()
    c.execute("SELECT pdf_path FROM captures WHERE id = ?", (capture_id,))
    row = c.fetchone()
    conn.close()
    
    if row and Path(row[0]).exists():
        return send_file(row[0], mimetype='application/pdf')
    return "PDF not found", 404

@app.post("/admin/cleanup")
def cleanup_storage():
    """Delete old captures to free storage"""
    days = int(request.form.get("days", 30))
    cutoff = datetime.now(timezone.utc).timestamp() - (days * 86400)
    
    conn = sqlite3.connect(DB_PATH)
    c = conn.cursor()
    c.execute("SELECT id, pdf_path, screenshot1_path, screenshot2_path FROM captures")
    captures = c.fetchall()
    
    deleted = 0
    for cap in captures:
        pdf_path = Path(cap[1])
        if pdf_path.exists() and pdf_path.stat().st_mtime < cutoff:
            Path(cap[1]).unlink(missing_ok=True)
            Path(cap[2]).unlink(missing_ok=True)
            Path(cap[3]).unlink(missing_ok=True)
            c.execute("DELETE FROM captures WHERE id = ?", (cap[0],))
            deleted += 1
    
    conn.commit()
    conn.close()
    
    return jsonify({'deleted': deleted, 'message': f'Deleted {deleted} old captures'})

# HTML Templates
HTML_TEMPLATE = """
<!DOCTYPE html>
<html lang="en">
<head>
  <meta charset="UTF-8">
  <title>CW Screenshot Tool</title>
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <style>
    :root {
      --primary: #003087;
      --accent: #0055a4;
      --bg: #f5f7fb;
      --text: #2d2d2d;
      --white: #ffffff;
      --shadow: rgba(0, 0, 0, 0.08);
    }

    body {
      font-family: "Inter", "Segoe UI", Roboto, Arial, sans-serif;
      background: var(--bg);
      margin: 0;
      color: var(--text);
      display: flex;
      flex-direction: column;
      align-items: center;
      min-height: 100vh;
    }

    header {
      width: 100%;
      background: linear-gradient(90deg, var(--primary), var(--accent));
      color: var(--white);
      text-align: center;
      padding: 1.5rem 0;
      box-shadow: 0 2px 5px var(--shadow);
    }

    header h1 {
      margin: 0;
      font-size: 1.8rem;
      letter-spacing: 0.5px;
    }

    .content {
      background: var(--white);
      max-width: 900px;
      width: 100%;
      margin: 2rem auto;
      padding: 2rem;
      border-radius: 8px;
      box-shadow: 0 1px 8px var(--shadow);
    }

    .form-group {
      margin-bottom: 1.5rem;
    }

    label {
      display: block;
      font-weight: 600;
      margin-bottom: 0.5rem;
    }

    input[type="text"],
    input[type="number"],
    select {
      width: 100%;
      padding: 0.75rem;
      border: 1px solid #ccc;
      border-radius: 4px;
      font-size: 1rem;
      box-sizing: border-box;
    }

    button {
      background: var(--primary);
      color: var(--white);
      border: none;
      padding: 0.75rem 2rem;
      font-size: 1rem;
      border-radius: 4px;
      cursor: pointer;
      transition: background 0.3s;
    }

    button:hover {
      background: var(--accent);
    }

    button:disabled {
      background: #ccc;
      cursor: not-allowed;
    }

    .view-btn {
      background: var(--accent);
      color: var(--white);
      padding: 0.5rem 1rem;
      text-decoration: none;
      border-radius: 4px;
      display: inline-block;
    }

    table {
      width: 100%;
      border-collapse: collapse;
      margin-top: 1rem;
    }

    th, td {
      padding: 0.75rem;
      text-align: left;
      border-bottom: 1px solid #ddd;
    }

    th {
      background: var(--bg);
      font-weight: 600;
    }

    code {
      background: #f0f0f0;
      padding: 2px 6px;
      border-radius: 3px;
      font-family: monospace;
      font-size: 0.9rem;
    }

    .admin-section {
      margin-top: 3rem;
      padding-top: 2rem;
      border-top: 2px solid var(--bg);
    }

    .storage-info {
      background: #fff3cd;
      padding: 1rem;
      border-radius: 4px;
      margin-bottom: 1rem;
    }

    footer {
      margin-top: auto;
      padding: 1rem;
      text-align: center;
      color: #666;
      font-size: 0.9rem;
    }

    ol {
      line-height: 1.8;
    }

    h2 {
      color: var(--primary);
      margin-top: 0;
    }

    h3 {
      color: var(--accent);
    }

    /* Loading overlay */
    .loading-overlay {
      display: none;
      position: fixed;
      top: 0;
      left: 0;
      width: 100%;
      height: 100%;
      background: rgba(0, 0, 0, 0.7);
      z-index: 9999;
      justify-content: center;
      align-items: center;
    }

    .loading-overlay.active {
      display: flex;
    }

    .loading-content {
      background: white;
      padding: 3rem;
      border-radius: 12px;
      text-align: center;
      box-shadow: 0 4px 20px rgba(0, 0, 0, 0.3);
    }

    .spinner {
      border: 4px solid #f3f3f3;
      border-top: 4px solid var(--primary);
      border-radius: 50%;
      width: 60px;
      height: 60px;
      animation: spin 1s linear infinite;
      margin: 0 auto 1.5rem;
    }

    @keyframes spin {
      0% { transform: rotate(0deg); }
      100% { transform: rotate(360deg); }
    }

    .loading-text {
      font-size: 1.2rem;
      color: var(--text);
      font-weight: 600;
      margin-bottom: 0.5rem;
    }

    .loading-subtext {
      color: #666;
      font-size: 0.9rem;
    }
  </style>
</head>
<body>
  <header>
    <h1>🚙 CW RV Screenshot Capture Tool</h1>
  </header>

  <div class="content">
    <h2>📸 Run Capture</h2>
    <p>Enter a CW RV stock number and select location to generate legally timestamped screenshots.</p>

    <form method="POST" action="/capture" id="captureForm">
      <div class="form-group">
        <label for="stock">Stock Number</label>
        <input type="text" id="stock" name="stock" placeholder="Enter stock number..." required>
      </div>

      <div class="form-group">
        <label for="location">Location</label>
        <select id="location" name="location">
          {{ location_options|safe }}
        </select>
      </div>

      <button type="submit" id="submitBtn">🚀 Run Capture</button>
    </form>

    <div class="admin-section">
      <h2>📊 Admin Panel - Recent Captures</h2>
      <table>
        <thead>
          <tr>
            <th>ID</th>
            <th>Stock</th>
            <th>Location</th>
            <th>Timestamp (UTC)</th>
            <th>Price Hash</th>
            <th>Payment Hash</th>
            <th>Status</th>
            <th>Action</th>
          </tr>
        </thead>
        <tbody>
          {{ captures|safe }}
        </tbody>
      </table>
    </div>

    <div class="admin-section">
      <h2>💾 Storage Management</h2>
      <div class="storage-info">
        <strong>Total Storage Used:</strong> {{ storage_mb }} MB
      </div>

      <h3>Cleanup Old Captures</h3>
      <form method="POST" action="/admin/cleanup" id="cleanupForm">
        <div class="form-group">
          <label for="days">Delete captures older than:</label>
          <input type="number" id="days" name="days" value="30" min="1"> days
        </div>
        <button type="submit">🗑️ Run Cleanup</button>
      </form>
    </div>
  </div>

  <!-- Loading Overlay -->
  <div class="loading-overlay" id="loadingOverlay">
    <div class="loading-content">
      <div class="spinner"></div>
      <div class="loading-text">Processing Screenshot Capture</div>
      <div class="loading-subtext">This may take up to a minute...</div>
    </div>
  </div>

  <footer>
    © 2025 CW Compliance Tool V3.0
  </footer>

  <script>
    // Handle form submission with loading animation
    document.getElementById('captureForm').addEventListener('submit', function(e) {
      // Show loading overlay
      document.getElementById('loadingOverlay').classList.add('active');
      document.getElementById('submitBtn').disabled = true;
    });

    // Clear loading state on page load (in case of back navigation)
    window.addEventListener('pageshow', function(event) {
      // Hide loading overlay
      document.getElementById('loadingOverlay').classList.remove('active');
      document.getElementById('submitBtn').disabled = false;
      
      // If navigating back from cache, force reload to ensure clean state
      if (event.persisted) {
        window.location.reload();
      }
    });

    // Also handle beforeunload to clean up
    window.addEventListener('beforeunload', function() {
      document.getElementById('loadingOverlay').classList.remove('active');
      document.getElementById('submitBtn').disabled = false;
    });
  </script>
</body>
</html>
"""

USED_RV_TEMPLATE = """
<!DOCTYPE html>
<html lang="en">
<head>
  <meta charset="UTF-8">
  <title>Used RV - No Capture Needed</title>
  <style>
    body {
      font-family: "Inter", sans-serif;
      background: #f5f7fb;
      display: flex;
      justify-content: center;
      align-items: center;
      min-height: 100vh;
      margin: 0;
    }
    .message-box {
      background: white;
      padding: 3rem;
      border-radius: 8px;
      box-shadow: 0 2px 10px rgba(0,0,0,0.1);
      text-align: center;
      max-width: 500px;
    }
    h1 {
      color: #003087;
      margin-top: 0;
    }
    .stock {
      font-size: 1.5rem;
      font-weight: bold;
      color: #0055a4;
      margin: 1rem 0;
    }
    a {
      display: inline-block;
      margin-top: 2rem;
      padding: 0.75rem 2rem;
      background: #003087;
      color: white;
      text-decoration: none;
      border-radius: 4px;
    }
    a:hover {
      background: #0055a4;
    }
  </style>
</head>
<body>
  <div class="message-box">
    <h1>ℹ️ {{ message }}</h1>
    <div class="stock">Stock #{{ stock }}</div>
    <p>Used RVs do not require pricing breakdown screenshots.</p>
    <a href="/">← Back to Home</a>
  </div>
</body>
</html>
"""

SUCCESS_TEMPLATE = """
<!DOCTYPE html>
<html lang="en">
<head>
  <meta charset="UTF-8">
  <title>Capture Successful</title>
  <style>
    body {
      font-family: "Inter", sans-serif;
      background: #f5f7fb;
      display: flex;
      justify-content: center;
      align-items: center;
      min-height: 100vh;
      margin: 0;
    }
    .success-box {
      background: white;
      padding: 3rem;
      border-radius: 8px;
      box-shadow: 0 2px 10px rgba(0,0,0,0.1);
      max-width: 600px;
    }
    h1 {
      color: #28a745;
      margin-top: 0;
    }
    .detail {
      margin: 1rem 0;
      padding: 0.5rem;
      background: #f8f9fa;
      border-radius: 4px;
    }
    code {
      background: #e9ecef;
      padding: 2px 6px;
      border-radius: 3px;
      font-family: monospace;
    }
    a {
      display: inline-block;
      margin-top: 2rem;
      margin-right: 1rem;
      padding: 0.75rem 2rem;
      background: #003087;
      color: white;
      text-decoration: none;
      border-radius: 4px;
    }
    a:hover {
      background: #0055a4;
    }
  </style>
</head>
<body>
  <div class="success-box">
    <h1>✅ Capture Successful!</h1>
    <div class="detail"><strong>Stock:</strong> {{ result.stock }}</div>
    <div class="detail"><strong>Timestamp:</strong> {{ result.timestamp }}</div>
    <div class="detail"><strong>Price Hash:</strong> <code>{{ result.price_hash }}...</code></div>
    <div class="detail"><strong>Payment Hash:</strong> <code>{{ result.payment_hash }}...</code></div>
    <a href="/view/{{ result.capture_id }}">📄 View PDF</a>
    <a href="/">← Back to Home</a>
  </div>
</body>
</html>
"""

if __name__ == "__main__":
    app.run(host="0.0.0.0", port=5000, debug=True)
