@echo off
cd /d "%~dp0"

:: find Python
set PY=python
python --version >nul 2>&1
if errorlevel 1 set PY=python3
%PY% --version >nul 2>&1
if errorlevel 1 (
    echo Python not found! Install Python 3.8+
    echo https://www.python.org/downloads/
    pause
    exit /b 1
)

:: clear old cache so latest code runs
echo Clearing cache...
del /s /q "%cd%\__pycache__" >nul 2>&1
for /d /r "%cd%" %%d in (__pycache__) do @if exist "%%d" rd /s /q "%%d" >nul 2>&1

:: check deps
%PY% -c "import flask,requests" >nul 2>&1
if errorlevel 1 (
    echo Installing dependencies...
    %PY% -m pip install flask requests -q --disable-pip-version-check
)

:: parse port
set PORT=5000
if not "%~1"=="" set PORT=%~1

echo.
echo ====================================
echo   Music Downloader v3
echo   http://127.0.0.1:%PORT%
echo ====================================
echo.

:: start server in new window (auto-reload templates on change)
start "MusicDownloader" /min %PY% web.py --port %PORT% --debug

:: wait for server
echo Waiting for server...
timeout /t 4 /nobreak >nul

:: open browser
echo Opening browser...
start http://127.0.0.1:%PORT%

echo.
echo Server running. Press any key to stop server and exit...
pause >nul

:: cleanup
taskkill /fi "WINDOWTITLE eq MusicDownloader*" /f >nul 2>&1
