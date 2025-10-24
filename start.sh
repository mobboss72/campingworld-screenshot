#!/bin/bash
set -e

echo "🚀 Starting CW Compliance Screenshot Tool..."

# Ensure Playwright browsers are installed
if [ ! -d "/app/ms-playwright/chromium"* ]; then
    echo "📦 Installing Playwright browsers..."
    playwright install chromium
fi

# Verify Chromium installation
echo "✓ Verifying Chromium installation..."
playwright install-deps chromium || true

# Create data directory if it doesn't exist
mkdir -p /app/data

# Check database
if [ -f "/app/data/captures.db" ]; then
    echo "✓ Database found"
else
    echo "📝 Database will be created on first capture"
fi

echo "✓ Initialization complete"

# Start the application
exec "$@"
