[CmdletBinding()]
param([string]$Python = 'python', [switch]$NoLaunch, [string]$PlayerRuntimeDirectory = '')
$ErrorActionPreference = 'Stop'
$projectRoot = [IO.Path]::GetFullPath((Join-Path $PSScriptRoot '..'))
$environmentRoot = Join-Path $projectRoot '.venv'
if (-not (Test-Path -LiteralPath (Join-Path $projectRoot 'pyproject.toml'))) { throw 'Project manifest is missing.' }
& $Python -c "import sys, struct; assert sys.platform == 'win32' and struct.calcsize('P') == 8 and sys.version_info >= (3,11), '64-bit Windows Python 3.11 or newer is required'"
if ($LASTEXITCODE -ne 0) { throw 'A supported Python interpreter is required.' }
if (-not (Test-Path -LiteralPath (Join-Path $environmentRoot 'Scripts/python.exe'))) {
    & $Python -m venv $environmentRoot
    if ($LASTEXITCODE -ne 0) { throw 'Could not create the isolated Python environment.' }
}
$cinemaPython = Join-Path $environmentRoot 'Scripts/python.exe'
& $cinemaPython -m pip install -c (Join-Path $projectRoot 'requirements-windows.lock') $projectRoot
if ($LASTEXITCODE -ne 0) { throw 'Installation failed. Existing media and user services were not changed.' }
$runtimeArgs = @('-m', 'personal_cinema.embedded_runtime')
if ($PlayerRuntimeDirectory) { $runtimeArgs += @('--from-directory', [IO.Path]::GetFullPath($PlayerRuntimeDirectory)) }
& $cinemaPython @runtimeArgs
if ($LASTEXITCODE -ne 0) { throw 'Embedded MPV setup failed. Run Install.ps1 again to retry; existing media and services were not changed.' }
Write-Host 'Installed with embedded MPV. First launch will configure Jellyfin and optional sources.'
if (-not $NoLaunch) {
    Start-Process -FilePath (Join-Path $environmentRoot 'Scripts/pythonw.exe') -ArgumentList @('-m', 'personal_cinema') -WorkingDirectory $projectRoot -WindowStyle Hidden
}
