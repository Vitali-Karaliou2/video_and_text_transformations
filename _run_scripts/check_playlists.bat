@echo off
chcp 65001 >nul 2>&1
setlocal
cd /d "%~dp0.."

echo.
echo ============================================================
echo  Check playlist cache (_cache/playlists.json vs _playlists/)
echo ============================================================
echo  Workspace: %CD%
if not "%~1"=="" echo  Channel:   %~1
echo.

python src\check_playlists.py %*
set "EXITCODE=%ERRORLEVEL%"

echo.
if "%EXITCODE%"=="0" (
    echo [BAT] Finished successfully.
) else (
    echo [BAT] Finished with errors. Exit code: %EXITCODE%
    echo [BAT] To fix: run _run_scripts\update_playlists.bat
)
echo.
pause
exit /b %EXITCODE%
