#!/bin/bash
# Start the M50 control app: http://localhost:5050
cd "$(dirname "$0")"
export PATH="/opt/homebrew/bin:$PATH"  # gphoto2 (also used for USB resets)
URL=http://localhost:5050

# Already running? Two copies would fight over the camera — just open it.
if curl -s -m 2 -o /dev/null "$URL/api/status"; then
  echo "האפליקציה כבר רצה — פותח את הדפדפן"
  open -a Safari "$URL"
  exit 0
fi

if ! command -v gphoto2 >/dev/null; then brew install gphoto2; fi
if [ ! -d .venv ]; then
  python3 -m venv .venv && .venv/bin/pip install -q -r requirements.txt
fi
(sleep 3; open -a Safari "$URL") &
exec ./serve.sh
