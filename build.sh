#!/usr/bin/env bash
# Render build script — runs on every deploy
set -o errexit

echo "→ Installing dependencies..."
pip install --upgrade pip
pip install -r requirements-prod.txt

echo "→ Collecting static files..."
python manage.py collectstatic --no-input

echo "→ Running migrations..."
python manage.py migrate --no-input

echo "✓ Build complete."
