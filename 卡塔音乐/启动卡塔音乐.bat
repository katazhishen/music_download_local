@echo off
setlocal EnableDelayedExpansion
cd /d "%~dp0"

set PORT=5000
if not "%~1"=="" set PORT=%~1

if not exist "%~dp0data" mkdir "%~dp0data" >nul 2>&1

echo ==========================================
echo   Kata Music v3 - Music Downloader
echo ==========================================
echo.

:: ---- Find Python ----
set PYTHON=
if exist "%~dp0venv\Scripts\python.exe" set "PATH=%~dp0venv\Scripts;%PATH%"

python --version >nul 2>&1
if errorlevel 1 (
    echo [ERROR] Python not found!
    pause
    exit /b 1
)

:: ---- Create venv if missing ----
if not exist "%~dp0venv\Scripts\python.exe" (
    echo Creating virtual environment on D: drive...
    python -m venv "%~dp0venv"
    if not errorlevel 1 (
        set "PATH=%~dp0venv\Scripts;%PATH%"
        echo Installing packages...
        pip install flask requests mutagen pycryptodomex beautifulsoup4 lxml aiohttp deep-translator
    )
)

:: ---- Check packages ----
echo Checking packages...
python -c "import flask,requests,mutagen,bs4,lxml,aiohttp" >nul 2>&1
if errorlevel 1 (
    echo Installing missing packages...
    pip install flask requests mutagen pycryptodomex beautifulsoup4 lxml aiohttp deep-translator
)

:: ---- Clear cache ----
if exist "%~dp0__pycache__" rmdir /s /q "%~dp0__pycache__" >nul 2>&1

:: ---- Firewall ----
netsh advfirewall firewall add rule name="KataMusic" dir=in action=allow protocol=TCP localport=%PORT% >nul 2>&1

:: ---- Kill old server ----
for /f "tokens=5" %%a in ('netstat -ano ^| findstr ":%PORT%.*LISTENING" 2^>nul') do taskkill /F /PID %%a >nul 2>&1

:: ---- Get LAN IP ----
set LAN_IP=YOUR_LAN_IP
for /f "tokens=2 delims=:" %%a in ('ipconfig ^| findstr /c:"IPv4" 2^>nul') do (
    set "RAW=%%a"
    set "LAN_IP=!RAW: =!"
)

echo.
echo ==========================================
echo   Local:  http://localhost:%PORT%
echo   LAN:    http://!LAN_IP!:%PORT%
echo ==========================================
echo.
echo   [!!] DO NOT CLOSE THIS WINDOW [!!]
echo ==========================================
echo.

:: ---- Start server ----
echo Starting server...
start "KataMusic" cmd /k python "%~dp0web.py" --port %PORT% --debug

echo Waiting 5 seconds...
ping -n 6 127.0.0.1 >nul 2>&1

:: ---- Verify ----
python -c "import urllib.request; urllib.request.urlopen('http://localhost:%PORT%/api/status', timeout=3)" >nul 2>&1
if errorlevel 1 (
    echo [WARNING] Server may not be ready - check the KataMusic window
    pause
    exit /b 1
)

echo [OK] Server is running!
echo Opening browser...
start http://localhost:%PORT%

echo.
echo Press any key to STOP the server...
pause >nul

taskkill /fi "WINDOWTITLE eq KataMusic*" /f >nul 2>&1
echo Server stopped.
endlocal
