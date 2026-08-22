@echo off
setlocal
set "APP=%~dp0auth_web_poc.py"
set "BUNDLED_PYTHON=C:\Users\cwx6580960\.cache\codex-runtimes\codex-primary-runtime\dependencies\python\python.exe"

where python.exe >nul 2>nul
if %errorlevel%==0 (
  start "" http://127.0.0.1:8766/
  python.exe "%APP%"
  goto :end
)

if exist "%BUNDLED_PYTHON%" (
  start "" http://127.0.0.1:8766/
  "%BUNDLED_PYTHON%" "%APP%"
  goto :end
)

echo Python 3.11 or newer with Pillow was not found.
pause

:end
endlocal
