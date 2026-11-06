# Use official lightweight Python image
FROM python:3.11-slim

# Set environment variables
ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PORT=7860

# Install system dependencies if required (e.g. for Pillow/curl/libpq)
RUN apt-get update && apt-get install -y --no-install-recommends \
    curl \
    libpq-dev \
    && rm -rf /var/lib/apt/lists/*

# Set up user with UID 1000 (Hugging Face Spaces standard)
RUN useradd -m -u 1000 user
ENV HOME=/home/user \
    PATH=/home/user/.local/bin:$PATH

# Set work directory
WORKDIR /home/user/app

# Copy dependencies first for efficient Docker layer caching
COPY --chown=user:user requirements.txt .

# Install Python packages
RUN pip install --no-cache-dir --upgrade pip && \
    pip install --no-cache-dir -r requirements.txt

# Copy application files
COPY --chown=user:user . .

# Ensure instance and uploads directories exist and are fully writable for SQLite
RUN mkdir -p instance uploads && \
    chmod -R 777 instance uploads /home/user/app

# Switch to non-root user
USER user

# Expose port (7860 is the default for Hugging Face Spaces)
EXPOSE 7860

# Run with Gunicorn — optimised for free-tier containers:
# • 2 workers + 4 threads = handles concurrent requests well
# • 300s timeout = prevents gunicorn worker kills on slow AI inferences
# • keep-alive 5 = connection reuse
CMD exec gunicorn --bind 0.0.0.0:${PORT:-7860} --workers 2 --threads 2 --worker-class gthread --timeout 120 --keep-alive 5 --max-requests 500 --max-requests-jitter 50 app:app

