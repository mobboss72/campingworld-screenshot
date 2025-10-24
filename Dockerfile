FROM python:3.11-slim

# Install system dependencies for Playwright
RUN apt-get update && apt-get install -y \
    wget \
    gnupg \
    ca-certificates \
    fonts-liberation \
    libasound2 \
    libatk-bridge2.0-0 \
    libatk1.0-0 \
    libatspi2.0-0 \
    libcups2 \
    libdbus-1-3 \
    libdrm2 \
    libgbm1 \
    libgtk-3-0 \
    libnspr4 \
    libnss3 \
    libwayland-client0 \
    libxcomposite1 \
    libxdamage1 \
    libxfixes3 \
    libxkbcommon0 \
    libxrandr2 \
    xdg-utils \
    libu2f-udev \
    libvulkan1 \
    xvfb \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app

# Set Playwright browser path to persist in the container
ENV PLAYWRIGHT_BROWSERS_PATH=/app/ms-playwright

# Copy requirements and install Python packages
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# Install Playwright browsers (Chromium only to save space)
RUN playwright install --with-deps chromium

# Verify installation
RUN playwright install-deps chromium

# Create data directory for database and captures
RUN mkdir -p /app/data

# Copy application files
COPY . .

# Make start script executable
RUN chmod +x start.sh

# Expose port
EXPOSE 8080

# Use start script as entrypoint
ENTRYPOINT ["./start.sh"]

# Run the application with increased timeout for captures
CMD ["gunicorn", "--bind", "0.0.0.0:8080", "--timeout", "180", "--workers", "1", "--worker-class", "sync", "server:app"]
