@echo off
setlocal

REM ==========================================================
REM  Merge dev -> master and push both branches to gitee.
REM  Run from anywhere; it auto-locates this repo directory.
REM
REM  Usage:
REM      merge_push_gitee.bat
REM      merge_push_gitee.bat "fix: 环境切换后立即刷新授权与接口地址"
REM
REM  Notes:
REM      - Auto-commits any uncommitted changes onto dev first.
REM      - Then merges dev into master (fast-forward when possible).
REM      - Finally pushes dev and master to the "gitee" remote.
REM      - Does NOT force-push / reset / rebase (destructive ops are avoided).
REM ==========================================================

cd /d "%~dp0"

set "REMOTE=gitee"
set "DEV=dev"
set "MASTER=master"

set "MSG=%~1"
if "%MSG%"=="" set "MSG=chore: sync dev and master"

echo ============================================
echo   Merge %DEV% -^> %MASTER% then push to %REMOTE%
echo ============================================

echo [check] verifying git repository...
git rev-parse --is-inside-work-tree >nul 2>&1
if errorlevel 1 (
    echo [ERROR] current directory is not a git repository
    exit /b 1
)

echo [1/6] fetch latest refs from %REMOTE%...
git fetch %REMOTE% --prune --quiet
if errorlevel 1 (
    echo [WARN] fetch failed, continuing anyway (network may be unreachable)
)

echo [2/6] checkout %DEV%...
git checkout %DEV%
if errorlevel 1 (
    echo [ERROR] checkout %DEV% failed. Resolve local uncommitted conflicts first.
    exit /b 1
)

echo [3/6] stage and commit pending changes...
git add -A
if errorlevel 1 (
    echo [ERROR] git add failed
    exit /b 1
)
git diff --cached --quiet
if errorlevel 1 (
    echo        commit: %MSG%
    git commit -m "%MSG%"
    if errorlevel 1 (
        echo [ERROR] git commit failed
        exit /b 1
    )
) else (
    echo        nothing to commit
)

echo [4/6] checkout %MASTER%...
git checkout %MASTER%
if errorlevel 1 (
    echo [ERROR] checkout %MASTER% failed
    exit /b 1
)

echo [5/6] merge %DEV% into %MASTER%...
git merge %DEV%
if errorlevel 1 (
    echo [ERROR] merge conflict detected. Resolve manually, then re-run this script.
    exit /b 1
)

echo [6/6] push %DEV% and %MASTER% to %REMOTE%...
git push %REMOTE% %DEV%
if errorlevel 1 (
    echo [ERROR] push %DEV% failed (retry after `git pull` if remote has new commits)
    exit /b 1
)
git push %REMOTE% %MASTER%
if errorlevel 1 (
    echo [ERROR] push %MASTER% failed (retry after `git pull` if remote has new commits)
    exit /b 1
)

echo.
echo [DONE] %DEV% and %MASTER% are merged and pushed to %REMOTE%.
endlocal