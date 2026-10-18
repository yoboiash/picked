#!/bin/bash
# Double-click this file in Finder to start NFL Pick'em.
cd "$(dirname "$0")"

if ! command -v python3 >/dev/null 2>&1; then
  echo "Python 3 isn't installed. macOS will offer to install it (Command Line Tools)."
  xcode-select --install
  exit 1
fi

if [ ! -d ".venv" ]; then
  echo "First run: setting things up (takes a minute)..."
  python3 -m venv .venv
  ./.venv/bin/pip install --quiet --upgrade pip
  ./.venv/bin/pip install --quiet -r requirements.txt
fi

(sleep 2 && open "http://localhost:8000") &
./.venv/bin/python app.py
