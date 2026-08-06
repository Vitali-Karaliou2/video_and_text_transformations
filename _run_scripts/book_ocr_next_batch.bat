@echo off
setlocal EnableDelayedExpansion

rem ====================================================================
rem Change ONLY this value to set how many spreads to OCR per run:

set "BATCH_SIZE=10"

rem ====================================================================
rem Usage:
rem   book_ocr_next_batch.bat                 -> next BATCH_SIZE pending spreads
rem   book_ocr_next_batch.bat overwrite-first -> re-OCR first BATCH_SIZE spreads
rem   book_ocr_next_batch.bat page_016        -> BATCH_SIZE spreads from page_016
rem ====================================================================

cd /d D:\Vitali\Streams_from_Youtube_Channels

for /f %%I in ('powershell -NoProfile -Command "Get-Date -Format yyyy-MM-dd_HH_mm"') do set "LOGSTAMP=%%I"

set "LOGDIR=_run_scripts\_logs"
if not exist "%LOGDIR%" mkdir "%LOGDIR%"
set "LOGFILE=%LOGDIR%\book_ocr_next_batch_%LOGSTAMP%.log"

call :logecho === OCR next %BATCH_SIZE% book spreads with Claude vision ===
call :logecho Log file: %LOGFILE%
call :logblank

if not exist ".env" (
  call :logecho ERROR: missing .env with ANTHROPIC_API_KEY in %CD%
  echo.
  pause
  exit /b 2
)

set "PYARGS=--next-batch %BATCH_SIZE%"
if /I "%~1"=="overwrite-first" set "PYARGS=--limit %BATCH_SIZE% --overwrite"
if not "%~1"=="" if /I not "%~1"=="overwrite-first" set "PYARGS=--start-from %~1 --limit %BATCH_SIZE% --overwrite"

set "RUNCMD=python -u src\book_ocr\ocr_claude.py %PYARGS%"
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
