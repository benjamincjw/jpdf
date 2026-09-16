@echo off
chcp 65001 >nul
setlocal
cd /d "%~dp0"
set "VENV=%LOCALAPPDATA%\jpdf\venv"

set "PY="
where py >nul 2>nul && set "PY=py -3"
if not defined PY where python >nul 2>nul && set "PY=python"
if not defined PY (
  echo [JPDF] Python 3.10 이상이 필요합니다.
  echo        https://www.python.org/downloads/ 에서 설치할 때 "Add python.exe to PATH"를 체크하세요.
  pause
  exit /b 1
)

if not exist "%VENV%\Scripts\pythonw.exe" (
  echo [JPDF] 처음 실행: 전용 파이썬 환경을 만들고 패키지를 설치합니다...  ^(%VENV%^)
  %PY% -m venv "%VENV%" || goto :fail
  "%VENV%\Scripts\python.exe" -m pip install --upgrade pip --quiet
  "%VENV%\Scripts\python.exe" -m pip install -r requirements.txt || goto :fail
  echo [JPDF] 설치 완료.
)

start "" "%VENV%\Scripts\pythonw.exe" "%~dp0app.py" %*
exit /b 0

:fail
echo [JPDF] 설치에 실패했습니다. 인터넷 연결을 확인한 뒤 다시 실행하세요.
pause
exit /b 1
