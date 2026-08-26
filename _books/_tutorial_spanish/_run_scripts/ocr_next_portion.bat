@echo off
setlocal EnableDelayedExpansion

rem ====================================================================
rem OCR the next portion of the textbook into pages_text\, one .md per
rem spread. Nothing in this file needs editing: how much a run takes,
rem which model reads it and how the pages are cut is set in
rem
rem     ocr_next_portion.settings.txt
rem
rem next to this bat. Python reads that file as UTF-8, which cmd.exe
rem cannot do for its own variables.
rem
rem Each run picks up where the last one stopped: a spread whose .md is
rem already in pages_text\ is skipped, so running the bat again simply
rem advances through the book.
rem
rem Usage:
rem   ocr_next_portion.bat            -> OCR the next portion
rem   ocr_next_portion.bat estimate   -> only say what it would cost
rem   ocr_next_portion.bat page_009 page_013
rem                                   -> re-OCR just these spreads
rem ====================================================================

cd /d D:\Vitali\Streams_from_Youtube_Channels
set "PYTHONIOENCODING=utf-8"

set "SETTINGS=%~dp0ocr_next_portion.settings.txt"

for /f %%I in ('powershell -NoProfile -Command "Get-Date -Format yyyy-MM-dd_HH_mm"') do set "LOGSTAMP=%%I"
set "LOGDIR=%~dp0_logs"
if not exist "%LOGDIR%" mkdir "%LOGDIR%"
set "LOGFILE=%LOGDIR%\ocr_next_portion_%LOGSTAMP%.log"

if not exist ".env" (
  echo ERROR: missing .env with ANTHROPIC_API_KEY in %CD%
  echo.
  pause
  exit /b 2
)

set "PYARGS="
if /I "%~1"=="estimate" (
  set "PYARGS=--estimate-only"
) else if not "%~1"=="" (
  set "PYARGS=--slugs %*"
)

call :logecho === OCR next portion of the textbook ===
call :logecho Settings: %SETTINGS%
call :logecho Log: %LOGFILE%
call :logblank

set "RUNCMD=python -u src\book_ocr\ocr_claude.py --settings '%SETTINGS%' %PYARGS%"
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
