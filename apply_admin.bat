@echo off
setlocal
cd /d "%~dp0"
net session >nul 2>&1
if errorlevel 1 (
    echo [*] Requesting administrator privileges - click YES on the UAC prompt...
    powershell -NoProfile -Command "Start-Process -FilePath '%~f0' -Verb RunAs"
    exit /b
)
where python >nul 2>&1
if errorlevel 1 (
    echo [!] python not found in PATH.
    pause
    exit /b 1
)
python "%~dp0mateobs.py" --apply
echo.
pause
