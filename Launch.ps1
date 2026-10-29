$ErrorActionPreference = 'Stop'
$appRoot = $PSScriptRoot
$runtimePython = Join-Path $env:USERPROFILE '.cache\codex-runtimes\codex-primary-runtime\dependencies\python\python.exe'
$venvPython = Join-Path $appRoot '.venv\Scripts\python.exe'
$appPython = if (Test-Path -LiteralPath $venvPython) { $venvPython } elseif (Test-Path -LiteralPath $runtimePython) { $runtimePython } else { (Get-Command python -ErrorAction Stop).Source }
Push-Location -LiteralPath $appRoot
try {
    & $appPython -c 'import pypdfium2, pypdf, PIL, numpy, pdfplumber'
    if ($LASTEXITCODE -ne 0) { throw 'Dependencies are missing. Run Setup.cmd once, then Launch.cmd.' }
    $serverInfo = Join-Path $appRoot 'data\server.json'
    if (Test-Path -LiteralPath $serverInfo) {
        $oldServer = Get-Content -LiteralPath $serverInfo -Raw | ConvertFrom-Json
        try {
            $health = Invoke-RestMethod -Uri ($oldServer.url + '/api/health') -TimeoutSec 2
            if ($health.ok) { Start-Process $oldServer.url; exit 0 }
        } catch { }
    }
    $appData = Join-Path $appRoot 'data'
    New-Item -ItemType Directory -Path $appData -Force | Out-Null
    $appProcess = Start-Process -FilePath $appPython -ArgumentList '-X utf8 -m leaf.server' -WorkingDirectory $appRoot -WindowStyle Hidden -RedirectStandardOutput (Join-Path $appData 'server-out.log') -RedirectStandardError (Join-Path $appData 'server-error.log') -PassThru
    for ($attempt = 0; $attempt -lt 40; $attempt++) {
        Start-Sleep -Milliseconds 200
        if ($appProcess.HasExited) { throw ('Leaf could not start. See ' + (Join-Path $appData 'server-error.log')) }
        if (Test-Path -LiteralPath $serverInfo) {
            try {
                $newServer = Get-Content -LiteralPath $serverInfo -Raw | ConvertFrom-Json
                if ($newServer.pid -eq $appProcess.Id) {
                    Start-Process $newServer.url
                    Write-Host 'Leaf is open in your browser. All conversion stays on this computer.'
                    exit 0
                }
            } catch { }
        }
    }
    throw ('Leaf is taking longer to start. See ' + (Join-Path $appData 'server-error.log'))
} finally { Pop-Location }
