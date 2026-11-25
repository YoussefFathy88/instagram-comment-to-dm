@echo off
rem Comment to DM control board: http://127.0.0.1:8765 (this PC only). Close this window to stop it.
cd /d "%~dp0..\backend"
start "" "http://127.0.0.1:8765"
".venv\Scripts\python.exe" -m app.api
