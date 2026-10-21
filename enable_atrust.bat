@echo off
net session >nul 2>&1
if %errorlevel% neq 0 (
    echo Requesting administrator privileges...
    powershell -Command "Start-Process -FilePath '%~f0' -Verb RunAs"
    exit /b
)
sc config aTrustService start= auto
net start aTrustService
echo.
echo [OK] aTrust auto-start restored.
pause