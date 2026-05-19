#!/bin/sh
set -e

echo "Running database seed..."
python seed.py

echo "Starting gunicorn..."
exec gunicorn --bind 0.0.0.0:5000 --workers 2 app:app
