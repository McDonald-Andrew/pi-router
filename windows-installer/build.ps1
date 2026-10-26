$ErrorActionPreference = 'Stop'
$routerArchive = [System.IO.Directory]::GetFiles((Get-Location), '*.tar.gz')[0]
$routerArchiveSha256 = (Get-FileHash -LiteralPath $routerArchive -Algorithm SHA256).Hash
Set-Location $PSScriptRoot

if (-not (Get-Command dotnet -ErrorAction SilentlyContinue)) {
    throw '.NET 8 SDK is required to build pi-router-installer.exe.'
}

$publish = Join-Path $PSScriptRoot 'publish'
if (Test-Path $publish) { Remove-Item $publish -Recurse -Force }

& dotnet publish .\PiRouterInstaller.csproj `
    -c Release `
    -r win-x64 `
    --self-contained true `
    -o $publish `
    -p:PublishSingleFile=true `
    -p:EnableCompressionInSingleFile=true `
    -p:PublishTrimmed=false `
    "-p:CertifiedArchiveSha256=$routerArchiveSha256"

if ($LASTEXITCODE -ne 0) {
    throw "dotnet publish exited with code $LASTEXITCODE"
}

$exe = Join-Path $publish 'pi-router-installer.exe'
if (-not (Test-Path $exe)) { throw 'Published installer EXE was not produced.' }

$hash = (Get-FileHash $exe -Algorithm SHA256).Hash.ToLowerInvariant()
$hashFile = Join-Path $publish 'pi-router-installer.sha256.txt'
Set-Content -Path $hashFile -Value $hash -Encoding ascii
Copy-Item .\THIRD-PARTY-NOTICES.txt $publish

Write-Host ''
Write-Host 'BUILD PASS'
Write-Host $exe
Write-Host "Certified router archive: $routerArchive"
Write-Host "Certified archive SHA256: $routerArchiveSha256"
Write-Host "Windows Installer SHA256: $hash"
