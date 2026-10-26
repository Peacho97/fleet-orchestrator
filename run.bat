@echo off
REM Starts the backend. Build the frontend first (cd frontend && npm install && npm run build).
cd /d %~dp0backend
python server.py
