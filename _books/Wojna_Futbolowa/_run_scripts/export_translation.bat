@echo off
setlocal EnableDelayedExpansion

rem ====================================================================
rem Export finished translation to Book_XX.md / .docx / .pdf (no model suffix).
rem Edit defaults here if needed:

set "BOOK=Wojna_Futbolowa"
set "TARGET_LANG=RU"
set "FROM=Book_RU_b_opus.md"
rem FROM=  leave empty to auto-pick Book_RU_b_opus.md (or other variants)

rem ====================================================================

cd /d D:\Vitali\Streams_from_Youtube_Channels
set "PYTHONIOENCODING=utf-8"

for /f %%I in ('powershell -NoProfile -Command "Get-Date -Format yyyy-MM-dd_HH_mm"') do set "LOGSTAMP=%%I"
set "LOGDIR=_books\%BOOK%\_run_scripts\_logs"
if not exist "%LOGDIR%" mkdir "%LOGDIR%"
set "LOGFILE=%LOGDIR%\export_translation_%LOGSTAMP%.log"

set "FROMARG="
if not "%FROM%"=="" set "FROMARG=--from %FROM%"

call :logecho === export translation  %TARGET_LANG%  from %FROM% ===
call :logecho Log: %LOGFILE%
call :logblank

set "RUNCMD=python -u src\book_translate\export_translation.py --book %BOOK% --lang %TARGET_LANG% %FROMARG%"
call :logecho RUN: %RUNCMD%
call :logblank

powershell -NoProfile -Command "[Console]::OutputEncoding = [Text.UTF8Encoding]::new(); & %RUNCMD% 2>&1 | ForEach-Object { [Console]::WriteLine($_); Add-Content -LiteralPath '%LOGFILE%' -Value $_ -Encoding utf8 }; exit $LASTEXITCODE"
set "RUNEXIT=!ERRORLEVEL!"
if not "!RUNEXIT!"=="0" (
  echo ERROR: exit code !RUNEXIT!
  >>"%LOGFILE%" echo ERROR: exit code !RUNEXIT!
)

call :logblank
call :logecho === done ===
call :logecho Log saved: %LOGFILE%
echo.
pause
exit /b !RUNEXIT!

:logecho
set "MSG=%*"
echo(!MSG!
>>"%LOGFILE%" echo(!MSG!
exit /b 0

:logblank
echo.
>>"%LOGFILE%" echo.
exit /b 0
