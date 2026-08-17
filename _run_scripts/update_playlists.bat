@echo off
chcp 65001 >nul 2>&1
setlocal
cd /d "%~dp0.."

echo.
echo ============================================================
echo  Update playlist cache from YouTube and create folders
echo ============================================================
echo  Workspace: %CD%
if not "%~1"=="" echo  Channel:   %~1
echo.

python src\channels\check_playlists.py --update --create %*
set "EXITCODE=%ERRORLEVEL%"

echo.
if "%EXITCODE%"=="0" (
    echo [BAT] Update finished successfully.
) else (
    echo [BAT] Update finished with errors. Exit code: %EXITCODE%
)
echo.
pause
exit /b %EXITCODE%
