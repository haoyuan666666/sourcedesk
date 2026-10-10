@echo off
cd /d "%~dp0"
echo Open http://127.0.0.1:5193/ after the server starts.
if exist ".venv\Scripts\python.exe" (
  ".venv\Scripts\python.exe" app.py
) else (
  py -3 app.py
)
pause
