@echo off
REM Wrapper for Task Scheduler - Stage 5 weekly digest.
REM The cd matters: a scheduled task starts in System32, where .env is not.
REM Costs nothing to run - it makes no API calls.

cd /d "%~dp0"

if not exist "logs" mkdir "logs"

python weekly_summary.py %* >> "logs\summary.log" 2>&1
set EXITCODE=%ERRORLEVEL%

echo [%DATE% %TIME%] exit code %EXITCODE% >> "logs\summary.log"
exit /b %EXITCODE%
