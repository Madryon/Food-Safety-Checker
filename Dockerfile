# Use official lightweight Python image
FROM python:3.11-slim

# Set environment variables
ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1

# Set work directory
WORKDIR /app

# Install system dependencies if required (e.g. for Pillow/curl)
RUN apt-get update && apt-get install -y --no-install-recommends \
    curl \
    libpq-dev \
    && rm -rf /var/lib/apt/lists/*

# Copy dependencies first for efficient Docker layer caching
COPY requirements.txt .

# Install Python packages
RUN pip install --no-cache-dir --upgrade pip && \
    pip install --no-cache-dir -r requirements.txt

# Copy application files
COPY . .

# Expose port (5000 default, Render uses dynamic $PORT)
EXPOSE 5000

# Run with Gunicorn — optimised for free-tier containers:
# • 2 workers + 4 threads = handles concurrent requests well
# • 300s timeout = prevents gunicorn worker kills on slow AI inferences
# • keep-alive 5 = connection reuse
CMD exec gunicorn --bind 0.0.0.0:${PORT:-5000} --workers 2 --threads 4 --timeout 300 --keep-alive 5 app:app
