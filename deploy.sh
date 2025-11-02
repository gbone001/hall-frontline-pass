#!/usr/bin/env bash
set -euo pipefail

echo "Deploy: pulling latest code from origin/main..."
git pull origin main

echo "Installing Python dependencies..."
if [ -f requirements.txt ]; then
  python -m pip install --upgrade pip
  python -m pip install -r requirements.txt
fi

# Add build steps here if you have any build process (packaging, bundling, etc.)
echo "Restarting service (attempting systemd then pm2)..."
if command -v systemctl >/dev/null 2>&1; then
  # Try a templated service name first, fall back to common names
  sudo systemctl restart hall-frontline-pass@$(whoami).service || sudo systemctl restart frontline-pass.service || true
elif command -v pm2 >/dev/null 2>&1; then
  pm2 restart frontline-pass || true
fi

echo "Deploy completed."
