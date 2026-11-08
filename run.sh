#!/bin/bash
cd "$(dirname "$0")"
python3 -m pip install -r requirements.txt
echo "Open http://localhost:5000  (Ctrl+C to stop)"
python3 app.py
