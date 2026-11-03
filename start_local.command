#!/bin/zsh
set -e
cd "$(dirname "$0")"

if ! command -v python3 >/dev/null 2>&1; then
  echo "Python 3 is required. Install it from https://www.python.org/downloads/macos/"
  read -r "?Press Return to close..."
  exit 1
fi

if [ ! -x .venv/bin/python ]; then
  echo "Creating the TankoPress Python environment..."
  python3 -m venv .venv
fi

if ! .venv/bin/python -c 'import fastapi, httpx, playwright, bs4' >/dev/null 2>&1; then
  echo "Installing TankoPress dependencies..."
  .venv/bin/python -m pip install --upgrade pip
  .venv/bin/python -m pip install -r requirements.txt
fi

if ! .venv/bin/python -c 'from playwright.sync_api import sync_playwright; p=sync_playwright().start(); p.chromium.launch(headless=True).close(); p.stop()' >/dev/null 2>&1; then
  echo "Installing Playwright Chromium..."
  .venv/bin/python -m playwright install chromium
fi

echo ""
echo "TankoPress is starting locally."
echo "Open http://127.0.0.1:8000 in your browser."
echo "Keep this Terminal window open while using TankoPress."
echo "Press Ctrl+C to stop the local server."
echo ""
exec .venv/bin/python -m uvicorn main:app --host 127.0.0.1 --port 8000
