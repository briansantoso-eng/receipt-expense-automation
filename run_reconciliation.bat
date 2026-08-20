@echo off
REM ---------------------------------------------------------------------------
REM Wrapper for Task Scheduler.
REM
REM Why this file exists: a scheduled task starts in C:\Windows\System32, not
REM in the project folder. The script looks for .env, clients.json and the
REM CSV by relative path, so without the cd below it would either find no
REM roster or find no credentials — and on a weekly unattended run you would
REM not notice for days.
REM
REM Any arguments passed to this .bat are forwarded to the script, so the same
REM wrapper serves the weekly task and a manual double-click.
REM ---------------------------------------------------------------------------

cd /d "%~dp0"

REM Log every run, newest appended, so an unattended failure leaves a trace.
if not exist "logs" mkdir "logs"

python check_and_reconcile.py %* >> "logs\run.log" 2>&1
set EXITCODE=%ERRORLEVEL%

echo [%DATE% %TIME%] exit code %EXITCODE% >> "logs\run.log"
exit /b %EXITCODE%
