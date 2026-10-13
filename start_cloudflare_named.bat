@echo off
REM ===========================================================================
REM  Daily launcher: backend (:8010) + FIXED Cloudflare named tunnel "contract"
REM  Fixed URL: https://contract.<yourdomain>  (configured by setup bat)
REM  Keep this window open; closing it stops the public URL. The URL never
REM  changes on restart - that is the whole point of the named tunnel.
REM ===========================================================================
setlocal
cd /d "%~dp0"

REM Clear proxy vars so cloudflared reaches Cloudflare's edge directly
REM (the system HTTP(S)_PROXY makes the tunnel connection flaky on this box).
set HTTP_PROXY=
set HTTPS_PROXY=
set http_proxy=
set https_proxy=
set ALL_PROXY=
set all_proxy=

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
echo [2/2] starting named tunnel "contract" (fixed public URL)
echo       Keep this window open; closing it turns the public URL off.
echo.
"C:\Users\zmy\cloudflared\cloudflared.exe" tunnel run contract

endlocal
