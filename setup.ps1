#requires -Version 5.1
<#
.SYNOPSIS
    Installs prerequisites for WorkshopAnalysis and starts first-time bootstrap.

.DESCRIPTION
    This script is only for initial setup. Future runs should invoke the program
    directly with .\WorkshopAnalysis and any desired flags.
#>

[CmdletBinding()]
param(
    [switch]$SkipBootstrap
)

Set-StrictMode -Version 3.0
$ErrorActionPreference = 'Stop'

$MinimumPythonVersion = [version]'3.9'
$ScriptRoot = Split-Path -Parent $MyInvocation.MyCommand.Path

function Write-Section {
    param([Parameter(Mandatory)][string]$Text)
    Write-Host ''
    Write-Host "== $Text ==" -ForegroundColor Cyan
}

function Get-CommandArguments {
    param([Parameter(Mandatory)][string[]]$Command)

    if ($Command.Count -le 1) {
        return @()
    }

    return $Command[1..($Command.Count - 1)]
}

function Invoke-CandidatePython {
    param(
        [Parameter(Mandatory)][string[]]$Command,
        [Parameter(Mandatory)][string[]]$Arguments
    )

    $exe = $Command[0]
    $baseArgs = Get-CommandArguments -Command $Command
    & $exe @baseArgs @Arguments
}

function Test-CommandExists {
    param([Parameter(Mandatory)][string]$Command)

    if ($Command -match '[\\/]') {
        return Test-Path -LiteralPath $Command
    }

    return [bool](Get-Command $Command -ErrorAction SilentlyContinue)
}

function Add-WindowsAppsToPath {
    $windowsApps = Join-Path $env:LocalAppData 'Microsoft\WindowsApps'
    if ((Test-Path -LiteralPath $windowsApps) -and ($env:Path -notlike "*$windowsApps*")) {
        $env:Path = "$env:Path;$windowsApps"
    }
}

function Get-WingetPath {
    $cmd = Get-Command winget.exe -ErrorAction SilentlyContinue
    if ($cmd) {
        return $cmd.Source
    }

    Add-WindowsAppsToPath

    $cmd = Get-Command winget.exe -ErrorAction SilentlyContinue
    if ($cmd) {
        return $cmd.Source
    }

    $candidate = Join-Path $env:LocalAppData 'Microsoft\WindowsApps\winget.exe'
    if (Test-Path -LiteralPath $candidate) {
        return $candidate
    }

    return $null
}

function Ensure-Winget {
    $winget = Get-WingetPath
    if ($winget) {
        return $winget
    }

    $installer = Join-Path $ScriptRoot 'Install-Winget.ps1'
    if (-not (Test-Path -LiteralPath $installer)) {
        throw 'Python 3.9+ was not found, winget is unavailable, and Install-Winget.ps1 is missing. Install Python 3.9+ manually, then run setup.ps1 again.'
    }

    Write-Section -Text 'winget setup'
    Write-Host 'winget was not found. Running Install-Winget.ps1...'
    & $installer

    Add-WindowsAppsToPath
    $winget = Get-WingetPath
    if (-not $winget) {
        throw 'Install-Winget.ps1 completed, but winget.exe was not found in the current session. Open a new PowerShell window and run setup.ps1 again.'
    }

    return $winget
}

function Get-PythonCandidates {
    $candidates = New-Object System.Collections.Generic.List[object]
    $venvPython = Join-Path $ScriptRoot '.venv\Scripts\python.exe'
    $candidates.Add(@($venvPython)) | Out-Null
    $candidates.Add(@('py', '-3')) | Out-Null
    $candidates.Add(@('python')) | Out-Null
    $candidates.Add(@('python3')) | Out-Null

    $localPrograms = Join-Path $env:LocalAppData 'Programs\Python'
    if (Test-Path -LiteralPath $localPrograms) {
        Get-ChildItem -LiteralPath $localPrograms -Recurse -File -Filter python.exe -ErrorAction SilentlyContinue |
            Sort-Object FullName -Descending |
            ForEach-Object { $candidates.Add(@($_.FullName)) | Out-Null }
    }

    return $candidates
}

function Find-Python {
    $versionScript = @'
import sys
print("{}.{}.{}".format(*sys.version_info[:3]))
'@

    foreach ($candidate in Get-PythonCandidates) {
        if (-not (Test-CommandExists -Command $candidate[0])) {
            continue
        }

        try {
            $output = Invoke-CandidatePython -Command $candidate -Arguments @('-c', $versionScript) 2>$null
            if (-not $output) {
                continue
            }

            $version = [version]($output | Select-Object -First 1)
            if ($version -ge $MinimumPythonVersion) {
                return [pscustomobject]@{
                    Command = [string[]]$candidate
                    Version = $version
                }
            }
        }
        catch {
            continue
        }
    }

    return $null
}

function Install-Python {
    Write-Section -Text 'Python setup'

    $winget = Ensure-Winget

    Write-Host 'Python 3.9+ was not found. Installing Python 3.12 with winget...'
    & $winget install --id Python.Python.3.12 --exact --source winget --accept-package-agreements --accept-source-agreements
    if ($LASTEXITCODE -ne 0) {
        throw "winget failed to install Python. Exit code: $LASTEXITCODE"
    }
}

function Install-PythonRequirements {
    param([Parameter(Mandatory)]$Python)

    $requirementsPath = Join-Path $ScriptRoot 'requirements.txt'
    if (-not (Test-Path -LiteralPath $requirementsPath)) {
        Write-Host 'No Python package requirements file found; skipping package install.'
        return
    }

    $requirements = Get-Content -LiteralPath $requirementsPath |
        Where-Object { -not [string]::IsNullOrWhiteSpace($_) -and $_.TrimStart() -notlike '#*' }

    if (-not $requirements) {
        Write-Host 'requirements.txt has no active packages; skipping package install.'
        return
    }

    Write-Section -Text 'Python package setup'
    Invoke-CandidatePython -Command $Python.Command -Arguments @('-m', 'pip', 'install', '-r', $requirementsPath)
    if ($LASTEXITCODE -ne 0) {
        throw "pip failed to install requirements. Exit code: $LASTEXITCODE"
    }
}

Write-Section -Text 'WorkshopAnalysis setup'
$python = Find-Python
if (-not $python) {
    Install-Python
    $python = Find-Python
}

if (-not $python) {
    throw 'Python installation completed, but Python 3.9+ could not be found in this shell. Open a new PowerShell window and run setup.ps1 again.'
}

Write-Host "Using Python $($python.Version)."
Install-PythonRequirements -Python $python

if ($SkipBootstrap) {
    Write-Host 'Dependency setup complete. Bootstrap was skipped.'
    return
}

Write-Section -Text 'Initial bootstrap'
$launcher = Join-Path $ScriptRoot 'WorkshopAnalysis.cmd'
if (-not (Test-Path -LiteralPath $launcher)) {
    throw "WorkshopAnalysis launcher was not found at $launcher"
}

& $launcher -Bootstrap
if ($LASTEXITCODE -ne 0) {
    exit $LASTEXITCODE
}
