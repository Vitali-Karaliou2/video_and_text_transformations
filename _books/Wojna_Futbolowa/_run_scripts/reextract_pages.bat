@echo off
setlocal EnableDelayedExpansion

rem ====================================================================
rem Re-extraction pipeline for page scans that lost content.
rem
rem Preparation: copy the defective *.png scans into a subfolder of
rem _books\%BOOK%\pages_extracted\ and put its name into FOLDER below.
rem That subfolder defines WHICH spreads are re-processed.
rem
rem Pipeline steps - one line per step below. Before each run, enable
rem the steps you need by removing "rem " at the start of the line,
rem and disable the rest by putting "rem " back.
rem
rem   --scan       re-capture the spreads from Archive.org
rem                (needs Edge with debugging + active Borrow:
rem                 powershell -File _run_scripts\book_start_edge_for_scan.ps1)
rem   --ocr        re-extract text from the new scans (Claude vision, per half)
rem   --assemble   rebuild OUTPUT\Book_PL.md / .docx / .pdf
rem   --translate  re-translate affected, already-translated sections in place
rem                in every working translation file (Book_%TARGET_LANG%*_*.md)
rem ====================================================================

set "BOOK=Wojna_Futbolowa"
set "FOLDER=reextracted_2026_08_09"
set "TARGET_LANG=RU"

set "STEPS="
set "STEPS=!STEPS! --scan"
rem set "STEPS=!STEPS! --ocr"
rem set "STEPS=!STEPS! --assemble"
rem set "STEPS=!STEPS! --translate"

rem ====================================================================

cd /d D:\Vitali\Streams_from_Youtube_Channels
set "PYTHONIOENCODING=utf-8"

if "!STEPS!"=="" (
  echo No pipeline steps enabled. Remove "rem " before at least one STEPS line.
  pause
  exit /b 1
)

for /f %%I in ('powershell -NoProfile -Command "Get-Date -Format yyyy-MM-dd_HH_mm"') do set "LOGSTAMP=%%I"
set "LOGDIR=_books\%BOOK%\_run_scripts\_logs"
if not exist "%LOGDIR%" mkdir "%LOGDIR%"
set "LOGFILE=%LOGDIR%\reextract_pages_%LOGSTAMP%.log"

set "RUNCMD=python -u src\book_ocr\reextract_pages.py --book %BOOK% --folder %FOLDER% --to %TARGET_LANG%!STEPS!"

echo === reextract pipeline: book=%BOOK% folder=%FOLDER% steps:!STEPS! ===
echo RUN: !RUNCMD!
echo Log: %LOGFILE%
echo.

powershell -NoProfile -Command "[Console]::OutputEncoding = [Text.UTF8Encoding]::new(); & !RUNCMD! 2>&1 | ForEach-Object { [Console]::WriteLine($_); Add-Content -LiteralPath '%LOGFILE%' -Value $_ -Encoding utf8 }; exit $LASTEXITCODE"
set "RUNEXIT=!ERRORLEVEL!"
if not "!RUNEXIT!"=="0" (
  echo ERROR: exit code !RUNEXIT!
  >>"%LOGFILE%" echo ERROR: exit code !RUNEXIT!
)

echo.
echo === done ===
echo Log saved: %LOGFILE%
echo.
pause
exit /b !RUNEXIT!
