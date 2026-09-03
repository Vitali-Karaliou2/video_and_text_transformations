@echo off
setlocal EnableDelayedExpansion

rem ====================================================================
rem Assemble pages_text\ into OUTPUT\Book_N_RU.md/.docx/.pdf — one set
rem per book part (see assemble.settings.txt in the book folder).
rem
rem Usage:
rem   assemble_book.bat              -> all parts + appendices
rem   assemble_book.bat 1            -> only Book_1_RU.*
rem   assemble_book.bat 1 9          -> parts 1 and 9 (appendices)
rem   assemble_book.bat --no-pdf     -> md+docx only
rem ====================================================================

cd /d D:\Vitali\Streams_from_Youtube_Channels
set "PYTHONIOENCODING=utf-8"
set "BOOK=Artificial_Intelligence_2006"

for /f %%I in ('powershell -NoProfile -Command "Get-Date -Format yyyy-MM-dd_HH_mm"') do set "LOGSTAMP=%%I"
set "LOGDIR=%~dp0_logs"
if not exist "%LOGDIR%" mkdir "%LOGDIR%"
set "LOGFILE=%LOGDIR%\assemble_book_%LOGSTAMP%.log"

set "EXTRA="
set "PARTS="
:parse
if "%~1"=="" goto run
if /I "%~1"=="--no-pdf" (
  set "EXTRA=!EXTRA! --no-pdf"
  shift
  goto parse
)
set "PARTS=!PARTS! %~1"
shift
goto parse

:run
set "PYARGS=--book %BOOK% %EXTRA%"
if not "%PARTS%"=="" set "PYARGS=!PYARGS! --parts%PARTS%"

call :logecho === Assemble %BOOK% ===
call :logecho Log: %LOGFILE%
call :logecho Args: %PYARGS%
call :logblank

set "RUNCMD=python -u src\book_ocr\assemble_book.py %PYARGS%"
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
