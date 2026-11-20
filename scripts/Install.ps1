[CmdletBinding()]
param([string]$Python = 'python', [switch]$NoLaunch)
$ErrorActionPreference = 'Stop'
$projectRoot = [IO.Path]::GetFullPath((Join-Path $PSScriptRoot '..'))
$environmentRoot = Join-Path $projectRoot '.venv'
if (-not (Test-Path -LiteralPath (Join-Path $projectRoot 'pyproject.toml'))) { throw 'Project manifest is missing.' }
& $Python -c "import sys; assert sys.version_info >= (3,11), 'Python 3.11 or newer is required'"
if ($LASTEXITCODE -ne 0) { throw 'A supported Python interpreter is required.' }
if (-not (Test-Path -LiteralPath (Join-Path $environmentRoot 'Scripts/python.exe'))) {
    & $Python -m venv $environmentRoot
    if ($LASTEXITCODE -ne 0) { throw 'Could not create the isolated Python environment.' }
}
$cinemaPython = Join-Path $environmentRoot 'Scripts/python.exe'
& $cinemaPython -m pip install -c (Join-Path $projectRoot 'requirements-windows.lock') $projectRoot
if ($LASTEXITCODE -ne 0) { throw 'Installation failed. Existing media and user services were not changed.' }
Write-Host 'Installed. First launch will configure Jellyfin, MPV, and optional sources.'
if (-not $NoLaunch) {
    Start-Process -FilePath (Join-Path $environmentRoot 'Scripts/pythonw.exe') -ArgumentList @('-m', 'personal_cinema') -WorkingDirectory $projectRoot -WindowStyle Hidden
}
