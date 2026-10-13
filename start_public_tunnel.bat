@echo off
REM ---------------------------------------------------------------------------
REM  Contract Review System - local service + public URL
REM    [1/2] starts the backend on http://0.0.0.0:8010 (skips if already up)
REM    [2/2] starts a cloudflared quick tunnel and prints a public https URL
REM  Login: same as before (no access code). Pick a role / register / scan-QR.
REM  NOTE: this window must stay open - closing it stops the tunnel.
REM        The quick-tunnel URL is random and CHANGES every time you restart.
REM ---------------------------------------------------------------------------
setlocal
cd /d "%~dp0"

REM Clear proxy vars so cloudflared reaches Cloudflare's edge directly
REM (the system HTTP(S)_PROXY can make the tunnel connection flaky).
set HTTP_PROXY=
set HTTPS_PROXY=
set http_proxy=
set https_proxy=
set ALL_PROXY=
set all_proxy=

REM Real Python that runs the backend on this machine (uv-managed CPython 3.11)
set PY=C:\Users\zmy\AppData\Roaming\uv\python\cpython-3.11-windows-x86_64-none\python.exe
if not exist "%PY%" set PY=python

echo [1/2] checking backend on port 8010 ...
netstat -ano | findstr ":8010" >nul
if errorlevel 1 (
  echo     port free - starting backend ...
  start "contract-review-backend" "%PY%" -m uvicorn backend.main:app --host 0.0.0.0 --port 8010
  echo     waiting for backend to come up ...
  timeout /t 5 /nobreak >nul
) else (
  echo     backend already running on 8010 - skip start.
)

echo.
echo [2/2] starting public tunnel - the https URL will appear below.
echo       Keep this window open; closing it turns the public URL off.
echo.
"C:\Users\zmy\cloudflared\cloudflared.exe" tunnel --url http://127.0.0.1:8010 --no-autoupdate --protocol http2

endlocal
