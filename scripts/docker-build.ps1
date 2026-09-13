$ErrorActionPreference = "Stop"
Set-StrictMode -Version Latest

$Root = Split-Path -Parent $PSScriptRoot
Push-Location $Root

try {
    Write-Host "== Reclaim dangling NiuOne images before build =="
    & docker image prune --force --filter "label=org.opencontainers.image.title=NiuOne"
    if ($LASTEXITCODE -ne 0) { throw "Failed to prune dangling NiuOne images." }

    Write-Host "== Build NiuOne Compose images =="
    & docker compose build
    if ($LASTEXITCODE -ne 0) { throw "NiuOne image build failed." }

    Write-Host "== Reclaim the superseded NiuOne image =="
    & docker image prune --force --filter "label=org.opencontainers.image.title=NiuOne"
    if ($LASTEXITCODE -ne 0) { throw "Failed to prune superseded NiuOne image." }
}
finally {
    Pop-Location
}
