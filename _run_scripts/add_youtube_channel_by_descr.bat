@echo off
setlocal EnableDelayedExpansion

rem ====================================================================
rem EDIT THIS LINE:
rem
rem DESCR=makingitright9305
rem
rem The free-text description of the YouTube channel to search for. The
rem first bat parameter, if given, overrides it.
rem
rem Earlier searches, to copy into that line:
rem   Политолог Аббас Галлямов YouTube
rem   YouTube Using Modern AI for Game Design
rem
rem It is a rem line on purpose: cmd.exe reads a bat in the console code
rem page and would turn Cyrillic into garbage on its way into a variable,
rem so PowerShell reads that line straight from the bat as UTF-8. Keep
rem this file UTF-8 without BOM, and edit only the text after the '='.

rem set "CHANNEL_PATH=AI_for_Game_Design\"  

rem ====================================================================

rem Do not use chcp 65001 here: it switches cmd to a raster font and breaks
rem Cyrillic in the classic console. Keep the system OEM code page.

cd /d D:\Vitali\Streams_from_Youtube_Channels\yt-dlp

for /f %%I in ('powershell -NoProfile -Command "Get-Date -Format yyyy-MM-dd_HH_mm"') do set "LOGSTAMP=%%I"

set "LOGDIR=_run_scripts\_logs"
if not exist "%LOGDIR%" mkdir "%LOGDIR%"
set "LOGFILE=%LOGDIR%\find_youtube_channel_by_descr_%LOGSTAMP%.log"

call :logecho === find a YouTube channel by description ===
call :logecho Log file: %LOGFILE%
call :logblank
call :logecho Up to 3 matching channels are shown with their video and
call :logecho playlist counts; each asks for a y/n/q confirmation before
call :logecho its summary is built. On "y" the channel folder with _cache,
call :logecho _playlists, _run_scripts and _summaries is created under
call :logecho _channels and the transcribe_and_edit_next bat files are
call :logecho generated in the channel _run_scripts folder.
call :logblank

set "RUNCMD=python -u src\find_youtube_channel_by_descr.py $DESCR"
call :logecho RUN: %RUNCMD%
powershell -NoProfile -Command "[Console]::OutputEncoding = [Text.UTF8Encoding]::new(); $lines = @((Get-Content -LiteralPath '%~f0' -Encoding utf8) -match '^rem DESCR='); if (-not $lines) { [Console]::WriteLine('ERROR: the DESCR line is missing'); exit 1 }; $DESCR = ($lines[0] -replace '^rem DESCR=', '').TrimEnd(); if ('%~1' -ne '') { $DESCR = '%~1' }; [Console]::WriteLine('Description: ' + $DESCR); Add-Content -LiteralPath '%LOGFILE%' -Value ('Description: ' + $DESCR) -Encoding utf8; & %RUNCMD% 2>&1 | ForEach-Object { [Console]::WriteLine($_); Add-Content -LiteralPath '%LOGFILE%' -Value $_ -Encoding utf8 }; exit $LASTEXITCODE"
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
exit /b 0

:logecho
set "MSG=%*"
echo(!MSG!
>>"%LOGFILE%" echo(!MSG!
exit /b 0

:logblank
echo.
>>"%LOGFILE%" echo.
exit /b 0
