@echo off
chcp 65001 > nul
cd /d "%~dp0"
echo 必要な部品を確認しています...
python -m pip install -r requirements.txt
if errorlevel 1 goto fail
echo PCモニターツールを起動します...
python main.py
if errorlevel 1 goto fail
exit /b 0

:fail
echo.
echo 起動できませんでした。上のメッセージを確認してください。
pause
exit /b 1
