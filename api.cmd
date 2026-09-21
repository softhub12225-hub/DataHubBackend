@echo off
setlocal EnableExtensions

rem Start the DataHub API for the reviewer console.
rem
rem WHY THIS EXISTS
rem   Same reason as review.cmd: `make` is not installed here, and the Makefile was the
rem   only thing that loaded .env into the environment. Without it the API starts but
rem   cannot reach Postgres, and the console shows API_UNREACHABLE -- which looks like a
rem   console bug and is not one.
rem
rem PORT 8099 ON PURPOSE
rem   web.cmd points the console's server-side proxy at this port. Change one and you
rem   must change the other, so they are written down together rather than remembered.
rem
rem NO OUTPUT AT ALL MEANS THIS SCRIPT DID NOT RUN. Every path below prints first.

set "REPO=%~dp0"
if "%REPO:~-1%"=="\" set "REPO=%REPO:~0,-1%"

if not exist "%REPO%\.env" (
  echo api.cmd: no .env at "%REPO%\.env" -- the API cannot reach the database.
  exit /b 3
)

for /f "usebackq eol=# tokens=1* delims==" %%A in ("%REPO%\.env") do (
  if not "%%~A"=="" set "%%~A=%%~B"
)

set "PYTHONIOENCODING=utf-8"
cd /d "%REPO%\apps\api" || (echo api.cmd: cannot enter apps\api & exit /b 3)

echo Starting the DataHub API on http://127.0.0.1:8099
echo   health : http://127.0.0.1:8099/health/ready
echo   stop   : Ctrl+C
echo.
uv run uvicorn app.main:app --host 127.0.0.1 --port 8099 %*
exit /b %ERRORLEVEL%
