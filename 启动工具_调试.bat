@echo off
chcp 65001 >nul
cd /d "%~dp0"

echo ================================================
echo O2O API 工具 - 调试启动（有控制台，可看报错）
echo ================================================

where python >nul 2>&1
if errorlevel 1 (
    echo [错误] 未找到 python。请先运行「安装依赖.bat」或安装 Python 3.8+
    pause
    exit /b 1
)

python -c "import pymysql,yaml,requests" 2>nul
if errorlevel 1 (
    echo [提示] 缺少依赖，正在安装...
    python -m pip install -r "%~dp0requirements.txt"
    if errorlevel 1 (
        echo [错误] 依赖安装失败
        pause
        exit /b 1
    )
)

echo 正在启动...
python "%~dp0api_tool.py" %*
if errorlevel 1 (
    echo.
    echo [错误] 程序异常退出，请把上方红色报错发给维护人
    pause
)
