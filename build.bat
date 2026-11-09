@echo off
:: Builds a self-contained release zip: SAMD_Flasher-windows.zip
:: Contains the .exe, OpenOCD, drivers, profiles and your bootloader/firmware files.
cd /d "%~dp0"
where python >nul 2>&1 || ( echo Python not found. & pause & exit /b 1 )
python -m pip install --upgrade pyinstaller --quiet || ( echo pip failed & pause & exit /b 1 )
if exist dist rmdir /s /q dist
if exist build rmdir /s /q build
if exist release rmdir /s /q release
if not exist tools\openocd python samd_flasher.py --download-openocd
python -m PyInstaller samd_flasher.spec || ( echo PyInstaller failed & pause & exit /b 1 )
mkdir release
copy dist\SAMD_Flasher.exe release\ >nul
xcopy /e /i /q tools release\tools >nul
xcopy /e /i /q drivers release\drivers >nul
xcopy /e /i /q bootloader release\bootloader >nul
xcopy /e /i /q firmware release\firmware >nul
copy profiles.json release\ >nul
copy README.md release\ >nul
copy APP.md release\ >nul
powershell -Command "Compress-Archive -Path release\* -DestinationPath SAMD_Flasher-windows.zip -Force"
echo Done: SAMD_Flasher-windows.zip
pause
