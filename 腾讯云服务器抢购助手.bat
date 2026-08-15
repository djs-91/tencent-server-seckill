@echo off
chcp 65001 >nul
title Tencent Cloud Server Seckill Helper
cd /d "%~dp0"

rem ---- auto-detect python ----
if exist venv\Scripts\python.exe (
    set "PYTHON=venv\Scripts\python.exe"
) else if exist "[REDACTED-PYTHON-PATH]" (
    set "PYTHON=[REDACTED-PYTHON-PATH]"
) else (
    set "PYTHON=python"
)

if not exist "%PYTHON%" (
    echo [ERROR] Python not found. Install Python 3.10+ first, then:
    echo        pip install playwright colorama requests
    echo        python -m playwright install chromium
    pause
    exit /b 1
)

echo.
echo =============================================
echo   Tencent Cloud Server Seckill Helper
echo   Activity:  Featured-202607 Seckill
echo   Product:   Lighthouse 4C4G 38Y/Year
echo   URL:       https://cloud.tencent.com/act/pro/featured-202607#MS
echo   Disclaimer: For personal learning only. Use at your own risk.
echo =============================================
echo.
"%PYTHON%" seckill_ui.py
pause
