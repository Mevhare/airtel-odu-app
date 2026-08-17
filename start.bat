@echo off
cd /d "%~dp0"
echo Starting WiFi Console...
echo.
for /f "tokens=2 delims=:" %%a in ('ipconfig ^| findstr /c:"IPv4"') do (
    echo   Phone/tablet:  http://%%a:8080
)
echo   This PC:       http://localhost:8080
echo.
python app.py
pause
