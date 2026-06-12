#!/usr/bin/env bash
# HL Reaper dashboard launcher — UI at http://localhost:8888 (localhost only).
# Starts the FastAPI bridge (127.0.0.1:8801) and the Next.js frontend (8888).
set -e
cd "$(dirname "$0")"

mkdir -p data

if ! curl -s -o /dev/null localhost:8801/api/status; then
  echo "starting api bridge on 127.0.0.1:8801..."
  nohup venv/bin/python dashboard/api.py >> data/dashboard_api.log 2>&1 &
  sleep 3
else
  echo "api bridge already running on 8801"
fi

if ! curl -s -o /dev/null localhost:8888; then
  echo "starting frontend on 127.0.0.1:8888..."
  cd dashboard/web
  nohup npm run start >> ../../data/dashboard_web.log 2>&1 &
  cd ../..
  sleep 4
else
  echo "frontend already running on 8888"
fi

echo
echo "HL Reaper dashboard: http://localhost:8888"
