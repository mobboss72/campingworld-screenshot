# Base on official Python 3.13 slim image (Debian-based) for pre-installed Python
FROM python:3.13-slim

# Install system dependencies required for Chromium Headless Shell
RUN apt-get update && apt-get install -y \
    libglib2.0-0 \
    libnss3 \
    libatk1.0-0 \
    libatk-bridge2.0-0 \
    libdrm2 \
    libxkbcommon0 \
    libxcomposite1 \
    libxdamage1 \
    libxfixes3 \
    libxrandr2 \
    libgbm1 \
    libasound2 \
    libpangocairo-1.0-0 \
    libpango-1.0-0 \
    libcairo2 \
    libatspi2.0-0 \
    libgtk-3-0 \
    libdbus-glib-1-2 \
    libxt6 \
    fonts-liberation \
    libappindicator3-1 \
    libnspr4 \
    lsb-release \
    wget \
    xdg-utils \
    && rm -rf /var/lib/apt/lists/*

# Set working directory
WORKDIR /app

# Copy requirements and install Python dependencies
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# Install Playwright browsers with custom path
RUN mkdir -p /app/ms-playwright && \
    PLAYWRIGHT_BROWSERS_PATH=/app/ms-playwright python -m playwright install --with-deps chromium

# Copy app code
COPY . .

# Expose port and set entrypoint
EXPOSE $PORT
CMD ["gunicorn", "-w", "1", "--timeout", "180", "-b", "0.0.0.0:$PORT", "server:app"]
