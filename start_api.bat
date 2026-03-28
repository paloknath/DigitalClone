@echo off

:: Kill existing API server if running on port 6789
for /f "tokens=5" %%a in ('netstat -aon ^| findstr ":6789" ^| findstr "LISTENING"') do (
    taskkill /F /PID %%a >nul 2>&1
)

:: Kill any process holding the WSS port (8765)
for /f "tokens=5" %%a in ('netstat -aon ^| findstr ":8765" ^| findstr "LISTENING"') do (
    taskkill /F /PID %%a >nul 2>&1
)

:: Kill any lingering browser/python instances from previous bot sessions
taskkill /F /IM chrome.exe >nul 2>&1
taskkill /F /IM msedge.exe >nul 2>&1
taskkill /F /IM pythonw.exe >nul 2>&1

:: Small delay to ensure ports are released
timeout /t 2 /nobreak >nul

:: Start the API server as a fully detached background process (no window)
cd /d %~dp0
start "" /B pythonw -u -c "import uvicorn; from api_server import app; uvicorn.run(app, host='0.0.0.0', port=6789)" > api_server.log 2>&1

:: Wait for server to be ready
timeout /t 5 /nobreak >nul

:: Verify it started
netstat -aon | findstr ":6789" | findstr "LISTENING" >nul
if %errorlevel%==0 (
    echo API server running on http://0.0.0.0:6789
    echo Logs: api_server.log
) else (
    echo ERROR: Check api_server.log
)
