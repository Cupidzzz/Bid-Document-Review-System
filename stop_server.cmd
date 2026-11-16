@echo off
rem ============================================================
rem stop_server.cmd [port] -- stop whatever listens on the port (default 8767)
rem
rem Prints the PID it stopped. Safe to run when nothing is listening.
rem ============================================================
chcp 65001 >nul
setlocal
cd /d "%~dp0"
rem optional port arg (e.g. stop_server.cmd 8770); default 8767
set "PORT=%~1"
if "%PORT%"=="" set "PORT=8767"
set "PY=C:\Users\janni\AppData\Local\Programs\Python\Python39\python.exe"
if not exist "%PY%" set "PY=python"

rem the real work: ops\_stop_port.py finds the listener by port and kills it,
rem not "kill all python", so other scripts are safe
echo Stopping whatever listens on 127.0.0.1:%PORT% ...
"%PY%" "%~dp0ops\_stop_port.py" %PORT%
if errorlevel 1 (
  echo.
  echo [STOP] failed to stop -- check the message above.
  echo.
  pause
  exit /b 1
)
echo.
rem stops the service only; projects/queue/results/OCR cache on disk are untouched
echo Next: run start_server.cmd to start it again.
pause
