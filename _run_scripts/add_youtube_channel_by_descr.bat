@echo off
setlocal EnableDelayedExpansion

rem ====================================================================
rem EDIT THIS LINE:

rem !!!! You need to edit the next line (editable line 1 of 2) 
rem DESCR=The AI for problem solvers. Built by Anthropic to be safe, accurate, and secure. Talk to Claude on claude.ai or download the app on desktop & mobile.
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

rem !!!! You need to edit the next line (editable line 2 of 2) 
rem CHANNEL_PATH=IT\LLMs
rem
rem The folder under _channels\ to create the channel folder in; leave it
rem empty for _channels\ itself. The folder is created if it is not there.
rem A channel that already has a folder somewhere keeps it - move it and run
rem synchronize_folders_in_bats.bat to regroup it. The second bat parameter,
rem if given, overrides this line.
rem
rem It is a rem line for the same reason as the one above: PowerShell reads
rem it out of this file rather than taking it from a cmd variable.

rem ====================================================================

rem Do not use chcp 65001 here: it switches cmd to a raster font and breaks
rem Cyrillic in the classic console. Keep the system OEM code page.

cd /d D:\Vitali\Streams_from_Youtube_Channels

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
call :logecho the folder named in the CHANNEL_PATH line above, and the
call :logecho transcribe_and_edit_next bat files are generated in the
call :logecho channel _run_scripts folder.
call :logblank

set "RUNCMD=python -u src\channels\find_youtube_channel_by_descr.py $DESCR @CONTAINER"
call :logecho RUN: %RUNCMD%
powershell -NoProfile -Command "[Console]::OutputEncoding = [Text.UTF8Encoding]::new(); $bat = Get-Content -LiteralPath '%~f0' -Encoding utf8; $lines = @($bat -match '^rem DESCR='); if (-not $lines) { [Console]::WriteLine('ERROR: the DESCR line is missing'); exit 1 }; $DESCR = ($lines[0] -replace '^rem DESCR=', '').TrimEnd(); if ('%~1' -ne '') { $DESCR = '%~1' }; $found = @($bat -match '^rem CHANNEL_PATH='); $where = ''; if ($found) { $where = ($found[0] -replace '^rem CHANNEL_PATH=', '').Trim().Trim('\') }; if ('%~2' -ne '') { $where = '%~2' }; $CONTAINER = @(); $shown = '_channels\ itself'; if ($where) { $CONTAINER = @('--container', $where); $shown = '_channels\' + $where }; foreach ($say in @('Description: ' + $DESCR, 'Channel folder goes into: ' + $shown)) { [Console]::WriteLine($say); Add-Content -LiteralPath '%LOGFILE%' -Value $say -Encoding utf8 }; & %RUNCMD% 2>&1 | ForEach-Object { [Console]::WriteLine($_); Add-Content -LiteralPath '%LOGFILE%' -Value $_ -Encoding utf8 }; exit $LASTEXITCODE"
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
