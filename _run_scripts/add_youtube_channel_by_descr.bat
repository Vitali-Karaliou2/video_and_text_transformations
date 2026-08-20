@echo off
setlocal EnableDelayedExpansion

rem ====================================================================
rem Nothing in this file needs editing. What the run works on - the
rem channel description to search for (DESCR) and the folder under
rem _channels\ to create the channel in (CHANNEL_PATH) - is edited in
rem
rem     add_youtube_channel_by_descr.settings.txt
rem
rem next to this bat. The values are kept there rather than here because
rem cmd.exe reads a bat in the console code page and would turn a Russian
rem description into garbage on its way into a variable; Python reads the
rem settings file as UTF-8 instead. (Do not use chcp 65001 to get around
rem that: it switches cmd to a raster font and breaks Cyrillic on screen.)
rem
rem The two bat parameters, if given, override the two settings.
rem ====================================================================

cd /d D:\Vitali\Streams_from_Youtube_Channels

for /f %%I in ('powershell -NoProfile -Command "Get-Date -Format yyyy-MM-dd_HH_mm"') do set "LOGSTAMP=%%I"

set "LOGDIR=_run_scripts\_logs"
if not exist "%LOGDIR%" mkdir "%LOGDIR%"
set "LOGFILE=%LOGDIR%\find_youtube_channel_by_descr_%LOGSTAMP%.log"

set "SETTINGS=%~dp0add_youtube_channel_by_descr.settings.txt"

call :logecho === find a YouTube channel by description ===
call :logecho Log file: %LOGFILE%
call :logecho Settings: %SETTINGS%
call :logblank
call :logecho Up to 3 matching channels are shown with their video and
call :logecho playlist counts; each asks for a y/n/q confirmation before
call :logecho its summary is built. On "y" the channel folder with _cache,
call :logecho _playlists, _run_scripts and _summaries is created under
call :logecho the folder named by the CHANNEL_PATH setting, and the
call :logecho transcribe_and_edit_next bat files are generated in the
call :logecho channel _run_scripts folder.
call :logblank

set "RUNCMD=python -u src\channels\find_youtube_channel_by_descr.py --settings '%SETTINGS%'"
if not "%~1"=="" set "RUNCMD=%RUNCMD% '%~1'"
if not "%~2"=="" set "RUNCMD=%RUNCMD% --container '%~2'"
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
