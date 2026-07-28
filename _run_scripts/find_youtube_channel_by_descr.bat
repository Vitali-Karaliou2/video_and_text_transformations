@echo off
setlocal EnableDelayedExpansion

rem ====================================================================
rem EDIT THIS LINE: the free-text description of the YouTube channel
rem to search for (the first bat parameter, if given, overrides it).
set "DESCR=Политолог Аббас Галлямов YouTube"
rem ====================================================================
if not "%~1"=="" set "DESCR=%~1"

rem Do not use chcp 65001 here: it switches cmd to a raster font and breaks
rem Cyrillic in the classic console. Keep the system OEM code page (cp866).

cd /d D:\Vitali\Streams_from_Youtube_Channels\yt-dlp

for /f %%I in ('powershell -NoProfile -Command "Get-Date -Format yyyy-MM-dd_HH_mm"') do set "LOGSTAMP=%%I"

set "LOGDIR=_run_scripts\_logs"
if not exist "%LOGDIR%" mkdir "%LOGDIR%"
set "LOGFILE=%LOGDIR%\find_youtube_channel_by_descr_%LOGSTAMP%.log"

call :logecho === find a YouTube channel by description ===
call :logecho Log file: %LOGFILE%
call :logecho Description: %DESCR%
call :logblank
call :logecho Up to 3 matching channels are shown with their video and
call :logecho playlist counts; each asks for a y/n/q confirmation before
call :logecho its summary is built. On "y" the channel folder with _cache,
call :logecho _playlists, _run_scripts and _summaries is created under
call :logecho _channels and the transcribe_and_edit_next bat files are
call :logecho generated in the channel _run_scripts folder.
call :logblank

set "RUNCMD=python -u src\find_youtube_channel_by_descr.py '%DESCR%'"
call :logecho RUN: %RUNCMD%
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
