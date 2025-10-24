# CW Compliance Screenshot Tool V4.0

Automated screenshot capture tool for Camping World RV listings with legal timestamping and PDF generation.

## Features

- ✅ **Automated Screenshot Capture** - Captures price and payment tooltips
- 🔒 **Legal Timestamps** - RFC 3161 cryptographic timestamps + HTTPS date verification
- 📄 **Automatic PDF Generation** - Downloads PDF report immediately after capture
- 🗄️ **Database History** - SQLite database stores all capture history
- 📍 **Multi-Location Support** - 5 Oregon Camping World locations with proper ZIP codes
- 🔐 **SHA-256 Verification** - Hash verification for all screenshots
- 📊 **Side-by-Side Screenshots** - Both tooltips displayed together in PDF

## Oregon Locations Supported

- Bend, OR (97701)
- Eugene, OR (97402)
- Hillsboro, OR (97124)
- Medford, OR (97504)
- Portland, OR (97201)

## Deployment on Railway

### 1. Create Railway Project

```bash
# Install Railway CLI (if not installed)
npm i -g @railway/cli

# Login to Railway
railway login

# Link to project or create new one
railway link
```

### 2. Create Volume for Persistent Storage

In Railway Dashboard:
1. Go to your project
2. Click on "Variables" tab
3. Add environment variable:
   - `DB_PATH` = `/app/data/captures.db`

4. Go to "Settings" tab
5. Under "Volumes", click "Add Volume"
6. Mount path: `/app/data`
7. Save

### 3. Deploy

```bash
# Deploy to Railway
railway up
```

## Environment Variables

Set these in Railway dashboard:

- `PORT` - Port to run on (default: 8080)
- `DB_PATH` - Database path (default: /app/data/captures.db)
- `OREGON_ZIP` - Default ZIP if not specified (default: 97201)

## Local Development

```bash
# Install dependencies
pip install -r requirements.txt

# Install Playwright browsers
playwright install chromium

# Run server
python server.py
```

Access at `http://localhost:8080`

## API Endpoints

- `GET /` - Main capture form
- `POST /capture` - Run capture (returns PDF download)
  - Parameters: `stock` (required), `location` (required)
- `GET /history` - View all previous captures
- `GET /view/<id>` - Download PDF for specific capture

## Database Schema

The SQLite database stores:
- Stock number
- Location and ZIP code
- Capture timestamps (UTC, HTTPS, RFC 3161)
- SHA-256 hashes
- Screenshot file paths
- PDF file path
- Debug information

## Legal Compliance

Each capture includes:
1. **Multiple Timestamps**:
   - UTC timestamp
   - HTTPS Date header from Cloudflare
   - RFC 3161 cryptographic timestamp (when available)

2. **Verification Hashes**:
   - SHA-256 hash of each screenshot
   - Saved in database and PDF

3. **PDF Report**:
   - Both screenshots side-by-side
   - All metadata and timestamps
   - Suitable for legal proceedings

## Troubleshooting

### RFC 3161 Timestamps Unavailable

If RFC 3161 timestamps show as unavailable:
- This is usually due to firewall/network restrictions
- The capture still succeeds with HTTPS date + SHA-256 hashes
- TSAs tried: DigiCert, Apple, Starfield, GlobalSign

### Screenshots Not Capturing

Check debug output for:
- Whether elements are visible
- Whether tooltips appear after clicking
- Network timeouts

### Database Locked

If you see "database is locked" errors:
- Restart the Railway service
- Check that volume is properly mounted
- Ensure only one worker is writing at a time

## File Structure

```
/app
├── server.py           # Main Flask application
├── index.html          # Landing page
├── requirements.txt    # Python dependencies
├── Dockerfile          # Container configuration
├── railway.toml        # Railway deployment config
├── start.sh           # Startup script
└── data/              # Persistent volume (mounted)
    ├── captures.db    # SQLite database
    └── cw-*-*/        # Screenshot directories
```

## Version History

- **V4.0** - Added PDF generation, database history, multi-location support
- **V3.0** - Added RFC 3161 timestamping
- **V2.0** - Improved tooltip detection
- **V1.0** - Initial release

## License

Proprietary - Internal Use Only
