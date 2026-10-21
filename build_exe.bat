@echo off
cd /d "%~dp0"

echo ================================================
echo 正在安装依赖...
echo ================================================
pip install -r requirements.txt

echo.
echo ================================================
echo 正在安装 PyInstaller...
echo ================================================
pip install pyinstaller

echo.
echo ================================================
echo 正在打包 EXE...
echo ================================================
pyinstaller --onefile --windowed --name "O2O_API_Tool" --icon=none api_tool.py

echo.
echo ================================================
echo 打包完成！
echo ================================================
echo 生成的文件: dist\O2O_API_Tool.exe
pause
