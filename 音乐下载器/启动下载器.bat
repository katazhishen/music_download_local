@echo off
cd /d "%~dp0"

:: find Python
set PY=python
%PY% --version >nul 2>&1
if errorlevel 1 set PY=python3
%PY% --version >nul 2>&1
if errorlevel 1 (
    echo Python not found! Please install Python 3.8+
    echo https://www.python.org/downloads/
    pause
    exit /b 1
)

:: parse port (must be early)
set PORT=5000
if not "%~1"=="" set PORT=%~1

:: clear old cache
if exist "%cd%\__pycache__" rmdir /s /q "%cd%\__pycache__" >nul 2>&1

:: set up local package dir (D drive, not C)
set PKG_DIR=%cd%\_packages
if not exist "%PKG_DIR%" mkdir "%PKG_DIR%" >nul 2>&1
set PYTHONPATH=%PKG_DIR%

:: check and auto-install dependencies
echo Checking dependencies...
%PY% -c "import flask" >nul 2>&1
if errorlevel 1 (
    echo Installing dependencies to project folder...
    %PY% -m pip install flask requests mutagen pycryptodomex beautifulsoup4 lxml --target="%PKG_DIR%" -q --disable-pip-version-check >nul 2>&1
    if errorlevel 1 (
        echo Trying default install location...
        %PY% -m pip install flask requests mutagen pycryptodomex beautifulsoup4 lxml -q --disable-pip-version-check >nul 2>&1
    )
    echo Dependencies installed!
)

:: add firewall rule (needs admin, ignore errors)
netsh advfirewall firewall add rule name="MusicDownloader" dir=in action=allow protocol=TCP localport=%PORT% >nul 2>&1

:: kill old server
for /f "tokens=5" %%a in ('netstat -ano ^| findstr ":%PORT%.*LISTENING" 2^>nul') do taskkill /F /PID %%a >nul 2>&1

:: get LAN IP
for /f "tokens=2 delims=:" %%a in ('ipconfig ^| findstr /c:"IPv4" 2^>nul') do set LAN_IP=%%a
set LAN_IP=%LAN_IP: =%
if "%LAN_IP%"=="" set LAN_IP=YOUR_LAN_IP

echo.
echo ==========================================
echo   Music Downloader v3
echo   Author: KataBiubiubiu QQ:3424409635
echo ------------------------------------------
echo   Local:  http://localhost:%PORT%
echo   LAN:    http://%LAN_IP%:%PORT%
echo ==========================================
echo.
echo   NOTE: Others MUST use LAN address, not localhost!
echo.

:: start server
echo Starting server...
start "MusicDownloader" %PY% web.py --port %PORT% --debug

:: wait
echo Waiting for server to be ready...
ping -n 6 127.0.0.1 >nul 2>&1

:: verify
%PY% -c "import urllib.request; urllib.request.urlopen('http://localhost:%PORT%/api/status', timeout=3)" >nul 2>&1
if errorlevel 1 (
    echo.
    echo [WARNING] Server might not be ready yet.
    echo If this keeps failing, check the black popup window for errors.
    echo Common issue: missing dependencies.
    echo Run: %PY% -m pip install flask requests mutagen pycryptodomex beautifulsoup4 lxml
    echo.
    pause
    exit /b 1
)

echo [OK] Server ready!
echo Opening browser...
start http://localhost:%PORT%

echo.
echo Press any key to stop server...
pause >nul

:: cleanup
taskkill /fi "WINDOWTITLE eq MusicDownloader*" /f >nul 2>&1
echo Server stopped.
