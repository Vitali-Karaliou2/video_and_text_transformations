@echo off
setlocal

rem ====================================================================
rem Checks the folder paths hardcoded in every bat of the project and
rem repairs the stale ones: the project root in "cd /d", the CHANNEL
rem folder under _channels\ and the PLAYLIST folder under the channel.
rem
rem Run it after moving the project to another folder or drive, and
rem after regrouping channel folders into other containers.
rem
rem This bat locates the project through its own path (%~dp0), so it
rem keeps working wherever the project is moved. Pass --check to only
rem see what would change, without writing anything.
rem ====================================================================

cd /d "%~dp0.."

echo.
echo ============================================================
echo  Synchronize the folder paths in the bat files
echo ============================================================
echo.

powershell -NoProfile -Command "[Console]::OutputEncoding = [Text.UTF8Encoding]::new(); & python -u src\synchronize_folders_in_bats.py %* 2>&1 | ForEach-Object { [Console]::WriteLine($_) }; exit $LASTEXITCODE"
set "EXITCODE=%ERRORLEVEL%"

echo.
if "%EXITCODE%"=="0" (
    echo [BAT] Finished successfully.
) else (
    echo [BAT] Something is still out of sync. Exit code: %EXITCODE%
    echo [BAT] See the lines marked WARNING above.
)
echo.
pause
exit /b %EXITCODE%
