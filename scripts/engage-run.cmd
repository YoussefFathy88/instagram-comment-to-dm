@echo off
rem Scheduled comment-to-DM run (Windows Task Scheduler). Same engine as a manual "engage-run".
rem --auto: renew the Instagram token when due, then run every approved campaign. Output goes to logs\.
cd /d "%~dp0..\backend"
echo ===== %date% %time% >> "..\logs\engagement-runs.log"
".venv\Scripts\python.exe" -m app.cli engage-run --auto >> "..\logs\engagement-runs.log" 2>&1
