@echo off
setlocal
cd /d "%~dp0"
net session >nul 2>&1
if not "%errorlevel%"=="0" goto elevate
goto run

:elevate
echo [*] Requesting administrator privileges - click YES on the UAC prompt...
powershell -NoProfile -Command "Start-Process -Verb RunAs -FilePath '%~f0'"
exit /b

:run
where python >nul 2>&1
if not "%errorlevel%"=="0" goto nopy
python "%~dp0patchmate.py" --kill --restore
pause
exit /b

:nopy
echo [x] python not found in PATH.
pause
exit /b
