@echo off
rem Okkults Quant Terminal — one-command launcher for Windows (cmd or PowerShell path-safe).
setlocal
cd /d "%~dp0"
chcp 65001 >nul

where py >nul 2>nul
if %errorlevel%==0 (
    set "PY=py -3.11"
) else (
    set "PY=python"
)

if not exist ".venv\Scripts\python.exe" (
    echo Creating .venv ^(%PY%^)...
    %PY% -m venv .venv || goto :fail
)

echo Installing dependencies ^(first run only, quiet updates later^)...
.venv\Scripts\python.exe -m pip install -q -r requirements.txt || goto :fail

echo Launching Okkults Quant Terminal...
.venv\Scripts\python.exe main.py %*
goto :eof

:fail
echo.
echo [X] Setup failed. Common causes:
echo     - Python 3.11+ not installed ^(install from python.org, tick "Add to PATH"^)
echo     - requirements.txt download blocked by proxy/antivirus
pause
exit /b 1
