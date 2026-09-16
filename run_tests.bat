@echo off
chcp 65001 >nul
setlocal
cd /d "%~dp0"
set "VENV=%LOCALAPPDATA%\jpdf\venv"
if not exist "%VENV%\Scripts\python.exe" (
  echo [JPDF] 먼저 run.bat 을 한 번 실행해 환경을 만들어 주세요.
  pause
  exit /b 1
)
"%VENV%\Scripts\python.exe" -m pip install -r requirements-dev.txt --quiet
"%VENV%\Scripts\python.exe" -m pytest tests -q
pause
