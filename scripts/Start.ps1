[CmdletBinding()]
param([string]$DataDirectory = '')
$ErrorActionPreference = 'Stop'
$projectRoot = [IO.Path]::GetFullPath((Join-Path $PSScriptRoot '..'))
$cinemaPython = Join-Path $projectRoot '.venv/Scripts/pythonw.exe'
if (-not (Test-Path -LiteralPath $cinemaPython)) { throw 'Run scripts/Install.ps1 first.' }
# Argument quoting is handled by a PowerShell-native argv list; no command shell.
$launchArguments = @('-m', 'personal_cinema')
if ($DataDirectory) {
    if ($DataDirectory.Contains('"')) { throw 'Invalid data directory.' }
    $launchArguments += @('--data-dir', ('"' + [IO.Path]::GetFullPath($DataDirectory) + '"'))
}
Start-Process -FilePath $cinemaPython -ArgumentList $launchArguments -WorkingDirectory $projectRoot -WindowStyle Hidden
