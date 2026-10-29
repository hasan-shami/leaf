@echo off
cd /d "%~dp0"
python -m venv .venv
if errorlevel 1 goto failed
".venv\Scripts\python.exe" -m pip install -r requirements.txt
if errorlevel 1 goto failed
echo Setup complete. Open Launch.cmd to start Leaf.
pause
exit /b 0
:failed
echo Setup did not complete. Install Python 3.11 or newer and try again.
pause
exit /b 1
