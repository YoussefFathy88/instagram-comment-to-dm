# Start the control board hidden (no console window) and open it in the browser.
# Used at Windows logon by the "CommentToDM-ControlBoard" task; safe to run by hand too.
$root = Split-Path -Parent $PSScriptRoot
$url = "http://127.0.0.1:8765"
$listening = Get-NetTCPConnection -LocalAddress 127.0.0.1 -LocalPort 8765 -State Listen -ErrorAction SilentlyContinue
if (-not $listening) {
    New-Item -ItemType Directory -Force -Path (Join-Path $root "logs") | Out-Null
    Start-Process -FilePath (Join-Path $root "backend\.venv\Scripts\python.exe") -ArgumentList "-m", "app.api" `
        -WorkingDirectory (Join-Path $root "backend") -WindowStyle Hidden `
        -RedirectStandardOutput (Join-Path $root "logs\control-board.out.log") `
        -RedirectStandardError (Join-Path $root "logs\control-board.err.log")
    for ($i = 0; $i -lt 30; $i++) {
        Start-Sleep -Milliseconds 500
        if (Get-NetTCPConnection -LocalAddress 127.0.0.1 -LocalPort 8765 -State Listen -ErrorAction SilentlyContinue) { break }
    }
}
Start-Process $url
