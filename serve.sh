#!/bin/bash
# Keep the app running: libgphoto2 occasionally segfaults on odd camera events,
# so restart it; state.json lets it resume recording/schedule where it was.
cd "$(dirname "$0")"
while true; do
  .venv/bin/python app.py
  code=$?
  # 0 = clean exit (Ctrl+C / SIGTERM handled in app.py): stop for real
  [ $code -eq 0 ] && break
  echo "$(date '+%H:%M:%S') app exited with code $code — restarting in 2s"
  sleep 2
done
