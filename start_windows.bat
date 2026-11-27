@echo off
cd /d %~dp0
if not exist .venv (
  python -m venv .venv
)
call .venv\Scriptsctivate.bat
python -m pip install -q -r requirements.txt
if not exist backend\storage\kg_database.sqlite (
  if exist sample_data\kg_database_sample.sqlite (
    if not exist backend\storage mkdir backend\storage
    copy sample_data\kg_database_sample.sqlite backend\storage\kg_database.sqlite >nul
  )
)
start "" http://127.0.0.1:8000
set PYTHONPATH=.
python -m uvicorn backend.app:app --host 127.0.0.1 --port 8000
