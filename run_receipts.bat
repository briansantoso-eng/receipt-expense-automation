@echo off
REM ---------------------------------------------------------------------------
REM Wrapper for Task Scheduler - Stage 4 receipt pipeline.
REM
REM The cd is the whole point: a scheduled task starts in C:\Windows\System32,
REM so .env, clients.json and the receipts folder would all be missing. On a
REM weekly job you would not notice for days.
REM
REM Arguments are forwarded, so this serves both the schedule and a manual run:
REM   run_receipts.bat --dry-run
REM ---------------------------------------------------------------------------

cd /d "%~dp0"

if not exist "logs" mkdir "logs"

python check_receipts.py %* >> "logs\receipts.log" 2>&1
set EXITCODE=%ERRORLEVEL%

echo [%DATE% %TIME%] exit code %EXITCODE% >> "logs\receipts.log"
exit /b %EXITCODE%
