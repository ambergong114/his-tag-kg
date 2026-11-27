#!/usr/bin/env bash
set -e
cd "$(dirname "$0")"

if [ ! -d ".venv" ]; then
python3 -m venv .venv
fi

source .venv/bin/activate
python -m pip install -q -r requirements.txt

if [ ! -f "backend/storage/kg_database.sqlite" ] && [ -f "sample_data/kg_database_sample.sqlite" ]; then
mkdir -p backend/storage
cp sample_data/kg_database_sample.sqlite backend/storage/kg_database.sqlite
fi

echo "🚀 服务启动中，即将自动打开 http://127.0.0.1:8000 ..."
(sleep 2 && open "http://127.0.0.1:8000") &
PYTHONPATH=. python -m uvicorn backend.app:app --host 127.0.0.1 --port 8000
