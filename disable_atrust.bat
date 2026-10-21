@echo off
net session >nul 2>&1
if %errorlevel% neq 0 (
    echo Requesting administrator privileges...
    powershell -Command "Start-Process -FilePath '%~f0' -Verb RunAs"
    exit /b
)
net stop aTrustService
sc config aTrustService start= demand
echo.
echo [OK] aTrust service stopped and auto-start disabled.
echo Please REBOOT your computer now, then run start_web.bat.
pause