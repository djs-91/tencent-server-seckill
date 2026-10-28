@echo off
rem No `chcp` here on purpose: on Windows, CPython writes console output through
rem the wide-char API, so Chinese renders correctly whatever the console codepage
rem is. `chcp` would additionally swallow a redirected stdin, which breaks
rem `助手.bat < answers.txt` style automation.
rem
rem This file is UTF-8, so every echo/title line below stays pure ASCII:
rem cmd prints its own text using the console codepage, and Chinese here
rem would show as mojibake on a CP936 or CP437 console. All Chinese UI text
rem is printed by Python instead, which writes through the wide-char console
rem API and is codepage-independent.
setlocal EnableExtensions
title Tencent Cloud Server Seckill Helper
cd /d "%~dp0"

rem ---- auto-detect python ----
rem `if exist python` never searches PATH in cmd, so `where` must be used.
rem Order: local venv -> PATH python -> py launcher -> standard install dirs.
rem Every candidate must actually run on Python 3.10+, which also rejects the
rem Microsoft Store stub at WindowsApps\python.exe (it exits non-zero).
set "PYTHON="
set "PY_DISPLAY="

if exist "%~dp0venv\Scripts\python.exe"  call :try "%~dp0venv\Scripts\python.exe"  "venv"
if not defined PYTHON if exist "%~dp0.venv\Scripts\python.exe" call :try "%~dp0.venv\Scripts\python.exe" ".venv"

if not defined PYTHON (
    for /f "usebackq delims=" %%i in (`where python 2^>nul`) do (
        if not defined PYTHON call :try "%%i" "PATH"
    )
)

if not defined PYTHON (
    for /f "usebackq delims=" %%i in (`where py 2^>nul`) do (
        if not defined PYTHON call :try "%%i" "py launcher"
    )
)

rem Installer default locations, for users who did not tick "Add to PATH".
if not defined PYTHON (
    for /d %%d in ("%LocalAppData%\Programs\Python\Python3*") do (
        if not defined PYTHON call :try "%%d\python.exe" "installed"
    )
)
if not defined PYTHON (
    for /d %%d in ("%ProgramFiles%\Python3*") do (
        if not defined PYTHON call :try "%%d\python.exe" "installed"
    )
)

if not defined PYTHON (
    echo [ERROR] No usable Python 3.10+ found on this machine.
    echo.
    echo         Install Python 3.10 or newer and tick "Add python.exe to PATH".
    echo         Avoid the Microsoft Store build: its alias is not a real python.
    echo         Official download: https://www.python.org/downloads/
    echo.
    echo         Then install the dependencies once:
    echo             python -m venv venv
    echo             venv\Scripts\activate
    echo             pip install -r requirements.txt
    echo.
    pause
    exit /b 1
)

echo.
echo =============================================
echo   Tencent Cloud Server Seckill Helper
echo   Python:    %PYTHON%  [%PY_DISPLAY%]
echo   Activity:  Featured Seckill (act_id auto-detected at runtime)
echo   Product:   Lighthouse 4C4G3M  38 CNY/year
echo   URL:       https://cloud.tencent.com/act/pro/double11-202611#MS
echo   Disclaimer: For personal learning only. Use at your own risk.
echo =============================================
echo.
"%PYTHON%" "%~dp0seckill_ui.py"
set "RC=%ERRORLEVEL%"
pause
exit /b %RC%

rem ---- helper: accept a python candidate only if it runs on 3.10+ ----
:try
rem %~1 = executable path, %~2 = where it came from (display only)
rem `<nul` matters: the probe must not touch stdin, otherwise a piped/redirected
rem stdin gets consumed by the version check before the menu ever reads it.
"%~1" -c "import sys; sys.exit(0 if sys.version_info[:2] >= (3, 10) else 3)" <nul >nul 2>nul
if errorlevel 1 exit /b 0
set "PYTHON=%~1"
set "PY_DISPLAY=%~2"
exit /b 0
