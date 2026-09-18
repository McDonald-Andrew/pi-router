$ErrorActionPreference = 'Stop'
Set-Location $PSScriptRoot

if (-not (Get-Command dotnet -ErrorAction SilentlyContinue)) {
    throw '.NET 8 SDK is required to build proton-router-installer.exe.'
}

$publish = Join-Path $PSScriptRoot 'publish'
if (Test-Path $publish) { Remove-Item $publish -Recurse -Force }

& dotnet publish .\ProtonRouterInstaller.csproj `
    -c Release `
    -r win-x64 `
    --self-contained true `
    -o $publish `
    -p:PublishSingleFile=true `
    -p:EnableCompressionInSingleFile=true `
    -p:PublishTrimmed=false

if ($LASTEXITCODE -ne 0) {
    throw "dotnet publish exited with code $LASTEXITCODE"
}

$exe = Join-Path $publish 'proton-router-installer.exe'
if (-not (Test-Path $exe)) { throw 'Published installer EXE was not produced.' }

$hash = (Get-FileHash $exe -Algorithm SHA256).Hash.ToLowerInvariant()
$hashFile = Join-Path $publish 'proton-router-installer.sha256.txt'
Set-Content -Path $hashFile -Value $hash -Encoding ascii
Copy-Item .\THIRD-PARTY-NOTICES.txt $publish

Write-Host ''
Write-Host 'BUILD PASS'
Write-Host $exe
Write-Host "SHA256: $hash"
