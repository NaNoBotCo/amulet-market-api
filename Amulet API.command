#!/bin/zsh
# Double-click launcher: build the snapshot if missing, then serve the API and
# open the index page in the browser.
cd "$(dirname "$0")" || exit 1
if [ ! -f market.db ]; then
  echo "Building snapshot (first run)…"
  python3 export_snapshot.py || { echo "snapshot build failed"; read; exit 1; }
fi
PORT=8787
( sleep 1.5; open "http://127.0.0.1:$PORT/" ) &
echo "Starting Amulet Market API on http://127.0.0.1:$PORT  (close this window to stop)"
python3 api.py --port $PORT
