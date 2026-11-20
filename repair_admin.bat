@echo off
setlocal
cd /d "%~dp0"
echo [*] Rebuilding OBS patch rules (restore + apply) ...
python "%~dp0mateobs.py" --repair
echo.
echo [*] Done. Press any key to close.
pause >nul
