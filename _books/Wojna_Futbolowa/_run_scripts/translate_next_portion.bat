@echo off
setlocal EnableDelayedExpansion

rem ====================================================================
rem Translate the next portion of the book (~10% of main stories by default).
rem Edit defaults here if needed:

set "TARGET_LANG=RU"
set "PORTION=10"
set "MODEL=opus"
rem MODEL=opus | sonnet  (opus chosen after comparing the first ~10% portion)
set "BATCH=1"
rem BATCH=1 use Message Batches API (half price); BATCH=0 realtime

rem ====================================================================
rem Usage:
rem   translate_next_portion.bat
rem   translate_next_portion.bat estimate
rem   translate_next_portion.bat opus
rem   translate_next_portion.bat sonnet nobatch
rem ====================================================================

cd /d D:\Vitali\Streams_from_Youtube_Channels
set "PYTHONIOENCODING=utf-8"

for /f %%I in ('powershell -NoProfile -Command "Get-Date -Format yyyy-MM-dd_HH_mm"') do set "LOGSTAMP=%%I"
set "LOGDIR=_books\Wojna_Futbolowa\_run_scripts\_logs"
if not exist "%LOGDIR%" mkdir "%LOGDIR%"
set "LOGFILE=%LOGDIR%\translate_next_portion_%LOGSTAMP%.log"

set "ESTIMATE="
set "YES=--yes"
set "INCLUDE=--include-front"

:parse
if "%~1"=="" goto run
if /I "%~1"=="estimate" set "ESTIMATE=--estimate-only" & set "YES=" & shift & goto parse
if /I "%~1"=="sonnet" set "MODEL=sonnet" & shift & goto parse
if /I "%~1"=="sonn" set "MODEL=sonnet" & shift & goto parse
if /I "%~1"=="opus" set "MODEL=opus" & shift & goto parse
if /I "%~1"=="batch" set "BATCH=1" & shift & goto parse
if /I "%~1"=="nobatch" set "BATCH=0" & shift & goto parse
shift
goto parse

:run
set "BATCHFLAG=--batch"
if "%BATCH%"=="0" set "BATCHFLAG=--no-batch"

call :logecho === translate next ~%PORTION%%%  %MODEL%  batch=%BATCH%  to %TARGET_LANG% ===
call :logecho Log: %LOGFILE%
call :logblank

set "RUNCMD=python -u src\book_translate\translate_book.py --book Wojna_Futbolowa --to %TARGET_LANG% --portion %PORTION% --model %MODEL% %BATCHFLAG% %ESTIMATE% %YES% %INCLUDE%"
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
