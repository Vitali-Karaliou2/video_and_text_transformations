@echo off
setlocal EnableDelayedExpansion

rem ====================================================================
rem Convert the next pages of the DjVu book into pages_text\, one .md
rem per book page, using the embedded OCR text layer (local and free,
rem no API calls). How many pages one run takes is set in
rem
rem     convert_next_pages.settings.txt
rem
rem next to this bat. Each run picks up where the last one stopped: a
rem page whose .md is already in pages_text\ is skipped, so running the
rem bat again simply advances through the book.
rem
rem Usage:
rem   convert_next_pages.bat                     -> next PAGES pages
rem   convert_next_pages.bat page_0031 page_0032 -> redo just these pages
rem ====================================================================

cd /d D:\Vitali\Streams_from_Youtube_Channels
set "PYTHONIOENCODING=utf-8"

set "SETTINGS=%~dp0convert_next_pages.settings.txt"

for /f %%I in ('powershell -NoProfile -Command "Get-Date -Format yyyy-MM-dd_HH_mm"') do set "LOGSTAMP=%%I"
set "LOGDIR=%~dp0_logs"
if not exist "%LOGDIR%" mkdir "%LOGDIR%"
set "LOGFILE=%LOGDIR%\convert_next_pages_%LOGSTAMP%.log"

set "PYARGS="
if not "%~1"=="" set "PYARGS=--slugs %*"

call :logecho === Convert next pages of the DjVu book ===
call :logecho Settings: %SETTINGS%
call :logecho Log: %LOGFILE%
call :logblank

set "RUNCMD=python -u src\book_ocr\djvu_to_text.py --settings '%SETTINGS%' %PYARGS%"
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
