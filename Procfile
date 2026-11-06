web: gunicorn --bind 0.0.0.0:$PORT --workers 2 --threads 2 --worker-class gthread --timeout 120 --keep-alive 5 --max-requests 500 --max-requests-jitter 50 app:app
