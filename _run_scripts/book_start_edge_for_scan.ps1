# Starts Edge with a CDP-friendly profile copy so src\book_ocr\scan_pages.py can attach.
# IMPORTANT: Borrow must be done in THIS Edge window (not a regular Edge window).
# Closing/killing Edge usually ends the Archive.org loan.
#
# Usage (from the workspace root):
#   1. Close regular Edge windows (optional but cleaner).
#   2. Run:  powershell -File _run_scripts\book_start_edge_for_scan.ps1
#   3. In the opened Edge: Log in to archive.org if needed, then Borrow the book.
#   4. When pages are visible, run:  python src\book_ocr\scan_pages.py

$ErrorActionPreference = "Stop"
$DebugPort = 9222
$BookUrl = "https://archive.org/details/isbn_9788307026183/page/12/mode/2up"
# Workspace root = parent of _run_scripts\
$Root = Split-Path -Parent (Split-Path -Parent $MyInvocation.MyCommand.Path)
$Src = Join-Path $env:LOCALAPPDATA "Microsoft\Edge\User Data"
$Dst = Join-Path $Root ".edge_scan_profile"

$edge = @(
    "${env:ProgramFiles(x86)}\Microsoft\Edge\Application\msedge.exe",
    "${env:ProgramFiles}\Microsoft\Edge\Application\msedge.exe"
) | Where-Object { Test-Path $_ } | Select-Object -First 1

if (-not $edge) {
    Write-Error "Microsoft Edge not found."
}

# If CDP already up, reuse it
try {
    $tcp = Test-NetConnection -ComputerName 127.0.0.1 -Port $DebugPort -WarningAction SilentlyContinue
    if ($tcp.TcpTestSucceeded) {
        Write-Host "Port $DebugPort already open. If book pages are visible, run: python src\book_ocr\scan_pages.py"
        exit 0
    }
} catch {}

# Stop browser Edge only (not msedgewebview2)
Get-Process -Name "msedge" -ErrorAction SilentlyContinue | Stop-Process -Force -ErrorAction SilentlyContinue
Start-Sleep -Seconds 2

Write-Host "Preparing scan profile (cookies/session copy)..."
if (Test-Path $Dst) {
    Remove-Item $Dst -Recurse -Force
}
New-Item -ItemType Directory -Path (Join-Path $Dst "Default\Network") -Force | Out-Null
Copy-Item (Join-Path $Src "Local State") (Join-Path $Dst "Local State") -Force

@("Preferences", "Secure Preferences", "Login Data", "Login Data-journal", "Web Data", "Web Data-journal") | ForEach-Object {
    $p = Join-Path $Src "Default\$_"
    if (Test-Path $p) { Copy-Item $p (Join-Path $Dst "Default\$_") -Force }
}
@("Cookies", "Cookies-journal", "Local Storage", "Session Storage") | ForEach-Object {
    $p = Join-Path $Src "Default\Network\$_"
    if (Test-Path $p) { Copy-Item $p (Join-Path $Dst "Default\Network\$_") -Recurse -Force }
}
if (Test-Path (Join-Path $Src "Default\Cookies")) {
    Copy-Item (Join-Path $Src "Default\Cookies") (Join-Path $Dst "Default\Cookies") -Force
}

Write-Host "Starting Edge with remote debugging on port $DebugPort ..."
Start-Process -FilePath $edge -ArgumentList @(
    "--remote-debugging-port=$DebugPort",
    "--remote-allow-origins=*",
    "--remote-debugging-address=127.0.0.1",
    "--user-data-dir=$Dst",
    "--no-first-run",
    "--no-default-browser-check",
    $BookUrl
)

Write-Host ""
Write-Host "Next steps:"
Write-Host "  1. In this Edge window: Log in (if needed) and click Borrow."
Write-Host "  2. Confirm pages flip and text is readable."
Write-Host "  3. Keep this Edge window open."
Write-Host "  4. Run:  python src\book_ocr\scan_pages.py"
Write-Host ""
