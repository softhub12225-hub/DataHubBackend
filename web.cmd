@echo off
setlocal EnableExtensions

rem Start the reviewer console (Next.js) and point it at the API.
rem
rem API_BASE_URL IS SERVER-SIDE ONLY
rem   It is deliberately not NEXT_PUBLIC_, so Next will not inline it into the browser
rem   bundle. The console reaches the API only through the route handler at
rem   /api/review/*, which is what keeps the session cookie HttpOnly end to end and the
rem   backend address out of the client.
rem
rem Run api.cmd FIRST. Without it the console loads and every panel reports
rem API_UNREACHABLE, which is the proxy telling you the truth.

set "REPO=%~dp0"
if "%REPO:~-1%"=="\" set "REPO=%REPO:~0,-1%"

cd /d "%REPO%\apps\web" || (echo web.cmd: cannot enter apps\web & exit /b 3)

if not exist "node_modules\.bin\next.CMD" (
  echo web.cmd: dependencies are not installed. Run: pnpm install --engine-strict=false
  exit /b 3
)

set "API_BASE_URL=http://127.0.0.1:8099"
set "NEXT_PUBLIC_APP_ENV=local"

echo Starting the reviewer console on http://localhost:3099
echo   sign in : http://localhost:3099/review/login
echo   api     : %API_BASE_URL%  (start it with api.cmd first)
echo   stop    : Ctrl+C
echo.
if /I "%~1"=="dev" (
  call "node_modules\.bin\next.CMD" dev --port 3099
) else (
  call "node_modules\.bin\next.CMD" start --port 3099
)
exit /b %ERRORLEVEL%
