@echo off
echo ============================================================
echo  MANUFACTURING MANAGEMENT SYSTEM - STARTING...
echo ============================================================
echo.
echo Checking Python installation...
python --version
if errorlevel 1 (
    echo.
    echo ERROR: Python is not installed or not in PATH!
    echo Please install Python from https://www.python.org/downloads/
    echo Make sure to check "Add Python to PATH" during installation.
    echo.
    pause
    exit /b 1
)

echo.
echo Installing/updating dependencies...
pip install -r requirements.txt --quiet

echo.
echo Starting server...
echo.
echo ============================================================
echo  IMPORTANT: KEEP THIS WINDOW OPEN!
echo  The server will stop if you close this window.
echo ============================================================
echo.
python app.py

pause
