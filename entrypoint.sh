#!/bin/sh
set -e

echo "Running database seed..."
python seed.py

echo "Starting gunicorn..."
# --threads (gthread): export/import requests spend most of their time waiting on
#   a subprocess (LibreOffice) or I/O with the GIL released, so threads let light
#   traffic flow past a slow export instead of head-of-line blocking on it.
# --timeout 180: must exceed the LibreOffice per-conversion timeout (120s in
#   export_docx.py). The gunicorn default of 30s would reap a worker mid-export
#   on a large multi-site PDF.
# --max-requests: recycle workers periodically to bound slow memory growth from
#   openpyxl / python-pptx / LibreOffice over long uptime.
exec gunicorn --bind 0.0.0.0:5000 \
    --workers 3 --threads 6 --worker-class gthread \
    --timeout 180 \
    --max-requests 500 --max-requests-jitter 50 \
    app:app
