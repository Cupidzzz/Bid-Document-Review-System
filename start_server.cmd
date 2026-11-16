@echo off
rem ============================================================
rem start_server.cmd -- start the review server (port 8767) and SHOW the banner
rem
rem What this window shows:
rem   1) the wake target that will actually be used (binding file wins)
rem   2) whether 8767 is already in use (then it refuses; stop it first)
rem   3) after start: the new banner lines from server.out.log, or the error
rem
rem The server itself runs in a separate minimized window; full log = server.out.log
rem
rem usage: start_server.cmd
rem stop : stop_server.cmd   (or close the minimized window)
rem ============================================================
chcp 65001 >nul
setlocal
cd /d "%~dp0"
rem Clear hostile Python env vars: a wrong PYTHONHOME/PYTHONPATH is a classic
rem cause of 0xc0000142 (merged from the old launcher script).
set "PYTHONHOME="
set "PYTHONPATH="
set "PYTHONSTARTUP="
set "PYTHONEXECUTABLE="
rem ENABLE_WAKE=1: clicking start-review in the UI really wakes the review session;
rem set 0 to only enqueue the task without waking (debug / manual mode).
if not defined ENABLE_WAKE set "ENABLE_WAKE=1"
rem Auto-create a dedicated session per project on first review (v1.3.0+).
rem The session is registered under the review workspace and pinned (v1.5.0).
rem Set ENABLE_AUTO_SESSION=0 to keep using the single default session.
rem ENABLE_AUTO_SESSION=1: a project first review auto-creates a dedicated session,
rem registered under the review workspace and pinned; set 0 for the default session.
if not defined ENABLE_AUTO_SESSION set "ENABLE_AUTO_SESSION=1"
rem OpenVINO backend: 12-page A/B = 4.34s/page -> 1.54s/page (2.82x), identical text.
rem Override with: set OCR_ENGINE=onnxruntime
rem OCR defaults to OpenVINO, about 2.8x faster than ONNX Runtime, identical text.
rem To fall back: set OCR_ENGINE=onnxruntime first, then run this script.
rem Note: switching engine invalidates the OCR cache and re-runs recognition.
if not defined OCR_ENGINE set "OCR_ENGINE=openvino"
rem Server output stays UTF-8 regardless of console code page
rem Force Python output to UTF-8 to avoid log mojibake across code pages.
if not defined PYTHONIOENCODING set "PYTHONIOENCODING=utf-8"
set "PY=python"
if not exist "%PY%" set "PY=python"

rem --- 1) wake target ---
rem --- 1) print which session the wake will target (binding file wins) ---
"%PY%" ops\_start_check.py wake

rem --- 2) already running? ---
rem --- 2) check if 8767 is already in use; refuse to avoid two instances ---
"%PY%" ops\_start_check.py port
if errorlevel 2 goto :check_fail
if errorlevel 1 goto :launch
echo.
echo [STOP] 8767 is already in use -- another instance is running.
echo        To restart: run stop_server.cmd first, then this script again.
echo.
pause
exit /b 1

:check_fail
echo.
echo [STOP] port check failed (exit %errorlevel%) -- is Python available?
echo.
pause
exit /b 2

rem --- 3) rotate the log if larger than 30 MB before start ---
:launch
rem --- rotate the log if it gets big (keeps start-up reads fast) ---
for %%A in (server.out.log) do if %%~zA GTR 31457280 (
  echo Log is larger than 30 MB -- rotating to server.out.log.1
  if exist server.out.log.1 del /q server.out.log.1
  move /y server.out.log server.out.log.1 >nul
)
rem record current log length; only echo lines added after start
set "MARK="
for /f %%i in ('"%PY%" ops\_start_check.py mark') do set "MARK=%%i"
if not defined MARK set "MARK=0"

echo Server: http://127.0.0.1:8767/
echo Log   : server.out.log
rem start the server: -u unbuffered, --host 0.0.0.0 for LAN access
rem the server runs in a separate minimized window; closing it stops the service
start "" /min cmd /c ""%PY%" -u server.py --host 0.0.0.0 --port 8767 >> server.out.log 2>&1"

rem --- 3) read back only the lines written after we started ---
rem (ping instead of timeout: timeout fails when stdin is redirected)
rem wait about 5s before reading the log (ping as sleep; timeout fails when stdin redirected)
rem first OpenVINO load may exceed 3s, so 6 pings (about 5s)
ping -n 6 127.0.0.1 >nul
echo.
echo ---------- startup result (new lines in server.out.log) ----------
"%PY%" ops\_start_check.py banner %MARK%
if errorlevel 1 goto :fail
echo ---------------------------------------------------------------
echo.
echo Next: open http://127.0.0.1:8767/  and verify /api/version , /api/engine
echo.
pause
exit /b 0

:fail
echo.
echo [STOP] server failed to start -- see server.out.log for the traceback.
echo.
pause
exit /b 1
