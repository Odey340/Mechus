@echo off
REM One-time local dev setup on Windows: creates .venv and installs Koov + Tiger Data deps.
REM Usage (from the repo folder):  setup_windows.bat
setlocal
cd /d "%~dp0"

set PY=
where py >nul 2>nul && set PY=py -3
if not defined PY (
  where python >nul 2>nul && python -c "import sys" >nul 2>nul && set PY=python
)
if not defined PY (
  echo.
  echo Python was not found. Install Python 3.11 from https://www.python.org/downloads/
  echo and tick "Add python.exe to PATH" in the installer, then run this again.
  goto :error
)
echo Using: %PY%
%PY% --version

if not exist .venv (
  echo Creating virtual environment in .venv ...
  %PY% -m venv .venv || goto :error
)
call .venv\Scripts\activate.bat || goto :error

python -m pip install --upgrade pip
REM webrtcvad has no official Windows wheels; webrtcvad-wheels is a drop-in prebuilt copy.
pip install webrtcvad-wheels
findstr /v /i /r "^webrtcvad" requirements.txt > "%TEMP%\koov-req-win.txt"
pip install -r "%TEMP%\koov-req-win.txt" -r requirements-prod.txt pytest || goto :error

if not exist .env if exist .env.example copy .env.example .env >nul

echo.
echo Done. Activate with:  .venv\Scripts\activate
echo Run tests:            python -m pytest tests\test_tiger_vultr_integration.py -q
echo Start the API:        python scripts\start_api_server.py
pause
exit /b 0

:error
echo Setup failed. See the error above.
pause
exit /b 1
