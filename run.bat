@echo off
title LLMSwitch Studio
setlocal enabledelayedexpansion
REM ============================================================
REM  LLMSwitch Studio launcher (pure ASCII on purpose)
REM
REM  Interpreter detection: every candidate is *actually executed*
REM  and asked for its version. Never trust "where py" -- on this
REM  machine py.exe still points at D:/kaifa/python.exe, which no
REM  longer exists, so "py -3" fails with exit code 101.
REM
REM  Usage:  run.bat [--mini] [--topmost] [--serve] [--browser]
REM                  [--port 8770] [--llama-port 8080] [--check]
REM ============================================================

set "PY="
set "TRIED="

call :probe "py -3"
call :probe "D:/dev/python/python.exe"

REM anything on PATH, but skip the Microsoft Store stub
for /f "delims=" %%P in ('where python 2^>nul') do call :probe_file "%%P"

call :probe "%LOCALAPPDATA%\Programs\Python\Python313\python.exe"
call :probe "%LOCALAPPDATA%\Programs\Python\Python312\python.exe"
call :probe "%LOCALAPPDATA%\Programs\Python\Python311\python.exe"
call :probe "%ProgramFiles%\Python313\python.exe"
call :probe "%ProgramFiles%\Python312\python.exe"
call :probe "E:/ComfyUI-aki-v3.2/python/python.exe"
call :probe "python"

if not defined PY (
  echo [ERROR] No usable Python 3.8+ found.
  echo.
  echo   Tried:
  echo   !TRIED!
  echo.
  echo   If "py" is broken, run:  py -0p   to see what it points at.
  echo   Or edit the candidate list at the top of this file and add
  echo   the full path to your python.exe.
  echo.
  pause
  exit /b 1
)

set "PYV="
for /f "delims=" %%V in ('%PY% -c "import sys;print(sys.version.split()[0])" 2^>nul') do set "PYV=%%V"

if /i "%~1"=="--check" (
  echo Interpreter: %PY%
  echo Version    : %PYV%
  %PY% -c "import sys,platform;print('Executable :', sys.executable);print('Bits       :', platform.architecture()[0])"
  exit /b 0
)

REM No third-party packages needed: backend is stdlib only,
REM the UI runs in Edge (Chromium) via --app mode.
start "" %PY% "%~dp0app.py" %*
exit /b 0

:probe_file
REM arg 1 = a full path from "where"; skip Store aliases
echo %~1 | findstr /i /c:"WindowsApps" >nul && exit /b 0
call :probe "%~1"
exit /b 0

:probe
REM arg 1 = a command line or path. Accept the first one that
REM reports Python >= 3.8. Exit code and version are both checked,
REM because a broken launcher exits non-zero but prints nothing.
if defined PY exit /b 0
set "TRIED=!TRIED! [%~1]"
%~1 -c "import sys;sys.exit(0 if sys.version_info[:2]>=(3,8) else 1)" >nul 2>&1
if errorlevel 1 exit /b 0
set "PY=%~1"
exit /b 0
