@echo off
setlocal
set "APP=%~dp0app_web.py"
set "BUNDLED_PYTHON=C:\Users\cwx6580960\.cache\codex-runtimes\codex-primary-runtime\dependencies\python\python.exe"
where python.exe >nul 2>nul
if %errorlevel%==0 (set "PYTHON=python.exe" & goto :launch)
if exist "%BUNDLED_PYTHON%" (set "PYTHON=%BUNDLED_PYTHON%" & goto :launch)
echo Python 3.11 or newer was not found.
pause
goto :end
:launch
start "QQ Album Archiver" "%PYTHON%" "%APP%"
timeout /t 1 /nobreak >nul
start "" http://127.0.0.1:8767/
:end
endlocal
