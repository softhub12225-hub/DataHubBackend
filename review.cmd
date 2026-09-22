@echo off
setlocal EnableExtensions

rem Windows entry point for scripts/source_review.py.
rem
rem WHY THIS FILE EXISTS
rem   `make` is not installed on this machine, and the Makefile was the only thing
rem   that loaded .env into the process environment. source_review.py reads its
rem   database credentials from the environment, so invoking it directly fails.
rem   This reproduces exactly what `make review-*` does: include .env, cd apps/api,
rem   run the script under uv.
rem
rem DELAYED EXPANSION IS OFF ON PURPOSE
rem   Credentials may contain `!`, which EnableDelayedExpansion would eat.
rem
rem IF THIS FILE IS EMPTY OR MISSING, cmd.exe RUNS IT AS A NO-OP AND RETURNS 0.
rem   That is indistinguishable from success at the prompt. Every path below
rem   therefore prints something before exiting: NO OUTPUT AT ALL MEANS THIS
rem   SCRIPT DID NOT RUN. Never read silence as a successful decision.

set "REPO=%~dp0"
if "%REPO:~-1%"=="\" set "REPO=%REPO:~0,-1%"

if not exist "%REPO%\apps\api\scripts\source_review.py" (
  echo review.cmd: cannot find apps\api\scripts\source_review.py under "%REPO%".
  exit /b 3
)

if not exist "%REPO%\.env" (
  echo review.cmd: no .env at "%REPO%\.env" -- the script cannot reach the database.
  exit /b 3
)

rem eol=# skips comments; blank lines are skipped by for /f. tokens=1* keeps any
rem `=` inside the value. %%~A / %%~B strip surrounding quotes if present.
for /f "usebackq eol=# tokens=1* delims==" %%A in ("%REPO%\.env") do (
  if not "%%~A"=="" set "%%~A=%%~B"
)

set "PYTHONIOENCODING=utf-8"
cd /d "%REPO%\apps\api" || (echo review.cmd: cannot enter apps\api & exit /b 3)

uv run python scripts/source_review.py %*
exit /b %ERRORLEVEL%
