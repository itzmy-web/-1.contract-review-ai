@echo off
REM ---------------------------------------------------------------------------
REM  Contract Review System - domestic public URL via cpolar (China-friendly)
REM    [1] starts backend on :8010 if free
REM    [2] cpolar http 8010  -> prints a domestic-accessible https URL
REM
REM  First time only, do ONE of these to set your authtoken:
REM    a) run:  C:\Users\zmy\cpolar\cpolar.exe authtoken <your-token>
REM    b) or paste the token into CPOLAR_TOKEN below and run this file.
REM  Register free at https://www.cpolar.com to get the token.
REM
REM  NOTE: keep this window open; closing it stops the tunnel.
REM        Free plan URL changes every restart.
REM ---------------------------------------------------------------------------
setlocal
cd /d "%~dp0"

REM Clear proxy vars so cpolar reaches its domestic servers directly
set HTTP_PROXY=
set HTTPS_PROXY=
set http_proxy=
set https_proxy=
set ALL_PROXY=
set all_proxy=

REM Optional: paste your cpolar authtoken here (only needed once).
set CPOLAR_TOKEN=__PASTE_YOUR_TOKEN_HERE__

if not "%CPOLAR_TOKEN%"=="__PASTE_YOUR_TOKEN_HERE__" (
  echo [i] registering cpolar authtoken ...
  "C:\Users\zmy\cpolar\cpolar.exe" authtoken %CPOLAR_TOKEN%
)

echo [1/2] checking backend on port 8010 ...
netstat -ano | findstr ":8010" >nul
if errorlevel 1 (
  echo     port free - starting backend ...
  start "contract-review-backend" "C:\Users\zmy\AppData\Roaming\uv\python\cpython-3.11-windows-x86_64-none\python.exe" -m uvicorn backend.main:app --host 0.0.0.0 --port 8010
  echo     waiting for backend to come up ...
  timeout /t 5 /nobreak >nul
) else (
  echo     backend already running on 8010 - skip start.
)

echo.
echo [2/2] starting cpolar tunnel - the domestic URL will appear below.
echo       Keep this window open; closing it turns the URL off.
echo.
"C:\Users\zmy\cpolar\cpolar.exe" http 8010

endlocal
