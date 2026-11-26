@echo off
rem Builds dist\HM30Link.exe — one file, runs on any Windows PC without Python.
cd /d "%~dp0"
python -m pip install -r requirements.txt pillow pyinstaller || goto :error
python -m PyInstaller --noconfirm --onefile --windowed --name HM30Link ^
  --icon hm30_link.ico ^
  --add-data "hm30_link.ico;." --add-data "hm30_link_icon.png;." ^
  hm30_link_app.py || goto :error
echo.
echo Done: %~dp0dist\HM30Link.exe
exit /b 0
:error
echo Build failed.
exit /b 1
