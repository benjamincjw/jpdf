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
echo [JPDF] PyInstaller 로 단일 폴더 실행파일을 만듭니다 ^(dist\JPDF\JPDF.exe^)...
"%VENV%\Scripts\python.exe" -m pip install --upgrade pyinstaller --quiet || goto :fail
"%VENV%\Scripts\python.exe" -m PyInstaller --noconfirm --clean --windowed --name JPDF ^
  --icon "assets\jpdf.ico" --add-data "assets;assets" --collect-all tkinterdnd2 app.py || goto :fail
echo.
echo [JPDF] 완료: dist\JPDF\JPDF.exe
echo        실행파일을 쓰려면 JPDF.exe 를 실행한 뒤 [도구] 메뉴에서 .jpdf 연결을 다시 등록하세요.
pause
exit /b 0
:fail
echo [JPDF] 빌드에 실패했습니다.
pause
exit /b 1
