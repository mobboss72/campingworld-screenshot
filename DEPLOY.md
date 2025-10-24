# Railway Deployment Checklist

## Pre-Deployment

### 1. Files to Deploy
Ensure these files are in your repository:
- ✅ `server.py` (main application)
- ✅ `index.html` (landing page)
- ✅ `requirements.txt` (Python dependencies)
- ✅ `Dockerfile` (container configuration)
- ✅ `start.sh` (startup script)
- ✅ `railway.toml` (Railway config)
- ✅ `.dockerignore` (build optimization)

### 2. Environment Variables (Optional)
Set these in Railway Dashboard if you want to customize:

```bash
# Port (Railway sets this automatically)
PORT=8080

# Database location (default is fine)
DB_PATH=/app/data/captures.db

# Storage mode (ephemeral recommended)
STORAGE_MODE=ephemeral

# Auto-cleanup (days)
AUTO_CLEANUP_DAYS=7

# Admin token (for /admin endpoints)
ADMIN_TOKEN=your-secret-token-here
```

### 3. Railway Volume Setup
1. Go to Railway Dashboard → Your Project
2. Click "Settings" → "Volumes"
3. Click "New Volume"
4. **Mount Path:** `/app/data`
5. **Size:** 1 GB (sufficient for ephemeral mode)
6. Save

---

## Deployment Steps

### Option 1: Railway CLI

```bash
# Install Railway CLI (if not installed)
npm i -g @railway/cli

# Login
railway login

# Link to project (or create new)
railway link

# Deploy
railway up

# View logs
railway logs
```

### Option 2: GitHub Integration

1. Push code to GitHub
2. Connect Railway to your GitHub repo
3. Railway auto-deploys on push
4. View deployment in Railway Dashboard

---

## Post-Deployment Verification

### 1. Check Deployment Status
In Railway Dashboard:
- ✅ Build completed successfully
- ✅ Service is running
- ✅ No crash loops

### 2. Check Logs
```bash
railway logs
```

Look for:
```
🚀 Starting CW Compliance Screenshot Tool...
✓ Verifying Chromium installation...
✓ Database will be created on first capture
✓ Initialization complete
[INFO] Listening at: http://0.0.0.0:8080
```

### 3. Test the Application

Visit your Railway URL (e.g., `https://your-app.railway.app`)

**Test Checklist:**
- [ ] Landing page loads
- [ ] Location dropdown has 5 Oregon cities
- [ ] Form accepts stock number
- [ ] Capture runs successfully
- [ ] PDF downloads automatically
- [ ] History page works (`/history`)
- [ ] Storage status works (`/admin/storage`)

### 4. Test Capture
Use test stock number: `2319928`
- Select location: Portland
- Submit form
- Wait 30-60 seconds
- PDF should download automatically

### 5. Verify Storage
```bash
curl https://your-app.railway.app/admin/storage
```

Expected response:
```json
{
  "storage_mode": "ephemeral",
  "auto_cleanup_days": 7,
  "database": {
    "total_captures": 1,
    "pdfs_in_db": 0,
    "files_exist": 1,
    "files_missing": 0
  },
  "temp_storage": {
    "directories": 1,
    "size_mb": 2.1
  }
}
```

---

## Troubleshooting

### Issue: Playwright Browser Not Found

**Symptoms:**
```
Executable doesn't exist at /app/ms-playwright/chromium...
```

**Solution:**
1. Check Dockerfile has `playwright install --with-deps chromium`
2. Verify `PLAYWRIGHT_BROWSERS_PATH=/app/ms-playwright` is set
3. Rebuild: `railway up --detach`

### Issue: Database Lock Errors

**Symptoms:**
```
database is locked
```

**Solution:**
1. Reduce workers to 1 in Dockerfile: `--workers 1`
2. Ensure volume is properly mounted at `/app/data`
3. Restart service

### Issue: Timeout During Capture

**Symptoms:**
```
TimeoutError: page.goto: Timeout 60000ms exceeded
```

**Solution:**
1. Increase gunicorn timeout in Dockerfile: `--timeout 180`
2. Check Railway region (closer = faster)
3. Network issues with target website

### Issue: Memory Errors

**Symptoms:**
```
OSError: [Errno 12] Cannot allocate memory
```

**Solution:**
1. Reduce to 1 worker: `--workers 1`
2. Add to Dockerfile before CMD:
   ```dockerfile
   ENV MALLOC_ARENA_MAX=2
   ```
3. Upgrade Railway plan for more RAM

### Issue: Screenshots Capture but PDF Fails

**Symptoms:**
```
PDF Generation Failed
```

**Solution:**
Check logs for specific error:
- Image processing error → Pillow/reportlab issue
- Path error → Check file permissions
- Memory error → Reduce image size in `generate_pdf()`

---

## Monitoring

### View Logs in Real-Time
```bash
railway logs --follow
```

### Check Storage Usage
```bash
curl https://your-app.railway.app/admin/storage | jq
```

### Manual Cleanup
```bash
curl "https://your-app.railway.app/admin/cleanup?days=7"
```

---

## Security Hardening

### 1. Protect Admin Endpoints

Add to `server.py`:
```python
ADMIN_TOKEN = os.getenv("ADMIN_TOKEN", "")

def require_admin(f):
    @wraps(f)
    def decorated(*args, **kwargs):
        if not ADMIN_TOKEN:
            return Response("Admin endpoints disabled", status=403)
        token = request.headers.get("Authorization", "")
        if token != f"Bearer {ADMIN_TOKEN}":
            return Response("Unauthorized", status=401)
        return f(*args, **kwargs)
    return decorated

@app.get("/admin/storage")
@require_admin
def admin_storage():
    # ...
```

Set in Railway:
```
ADMIN_TOKEN=your-secret-token-12345
```

Access with:
```bash
curl -H "Authorization: Bearer your-secret-token-12345" \
  https://your-app.railway.app/admin/storage
```

### 2. Rate Limiting (Optional)

Install Flask-Limiter:
```bash
pip install Flask-Limiter
```

Add to server.py:
```python
from flask_limiter import Limiter
from flask_limiter.util import get_remote_address

limiter = Limiter(
    app=app,
    key_func=get_remote_address,
    default_limits=["50 per hour"]
)

@app.post("/capture")
@limiter.limit("10 per hour")
def capture():
    # ...
```

---

## Maintenance

### Regular Tasks

**Weekly:**
- Check storage status
- Review capture logs
- Run cleanup if needed

**Monthly:**
- Review database size
- Check for errors in logs
- Update dependencies

**As Needed:**
- Update Playwright: `pip install --upgrade playwright`
- Update Python packages: `pip install --upgrade -r requirements.txt`

---

## Success Criteria

Your deployment is successful when:
- ✅ Application loads without errors
- ✅ Test capture completes in < 60 seconds
- ✅ PDF downloads automatically
- ✅ Database stores capture metadata
- ✅ No memory/storage warnings
- ✅ Logs show clean startup

---

## Getting Help

If issues persist:
1. Check Railway logs: `railway logs`
2. Check storage: `/admin/storage`
3. Test locally: `python server.py`
4. Review error traces in capture debug output

Common fixes:
- Rebuild: `railway up --detach`
- Restart: Railway Dashboard → Restart Service
- Check volumes: Ensure `/app/data` mounted correctly
