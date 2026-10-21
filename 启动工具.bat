@echo off
chcp 65001 >nul
cd /d "%~dp0"

REM 优先 pythonw（无黑窗口）；找不到则回退 python
set "PY="
where pythonw >nul 2>&1 && set "PY=pythonw"
if not defined PY where python >nul 2>&1 && set "PY=python"
if not defined PY (
    mshta "javascript:alert('未检测到 Python。\n\n请先安装 Python 3.8+ 并勾选 Add to PATH，\n然后双击「安装依赖.bat」。\n\n下载: https://www.python.org/downloads/');close()"
    exit /b 1
)

REM 依赖缺失时自动安装（pythonw 无控制台，失败时提示用调试启动）
python -c "import pymysql,yaml,requests" 2>nul
if errorlevel 1 (
    python -m pip install -r "%~dp0requirements.txt" > "%~dp0启动失败.log" 2>&1
    python -c "import pymysql,yaml,requests" 2>nul
    if errorlevel 1 (
        mshta "javascript:alert('Python 依赖未安装成功。\n\n请双击「安装依赖.bat」，\n或「启动工具_调试.bat」查看具体报错。');close()"
        exit /b 1
    )
)

start "" %PY% "%~dp0api_tool.py" %*
exit
