@echo off
chcp 65001 >nul
cd /d "%~dp0"

echo ================================================
echo O2O API 工具 - 安装 Python 依赖
echo ================================================

where python >nul 2>&1
if errorlevel 1 (
    echo [错误] 未找到 python。请先安装 Python 3.8+ 并勾选 "Add python.exe to PATH"
    echo 下载: https://www.python.org/downloads/
    pause
    exit /b 1
)

python --version
echo.
python -m pip install --upgrade pip
python -m pip install -r "%~dp0requirements.txt"
echo.
if errorlevel 1 (
    echo [失败] 依赖安装未完成，请检查网络或 pip 配置
    pause
    exit /b 1
)
echo [完成] 依赖已安装，可双击「启动工具.bat」运行
pause
