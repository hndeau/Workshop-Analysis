#requires -Version 5.1
<#
.SYNOPSIS
    Installs prerequisites for WorkshopAnalysis and starts first-time bootstrap.

.DESCRIPTION
    This script is only for initial setup. Future runs should invoke the program
    directly with .\WorkshopAnalysis and use commands inside the interpreter.
#>

[CmdletBinding()]
param(
    [switch]$SkipBootstrap,
    [int]$WingetInstallRetryCount = 3
)

Set-StrictMode -Version 3.0
$ErrorActionPreference = 'Stop'

$MinimumPythonVersion = [version]'3.9'
$MinimumDotNetSdkVersion = [version]'8.0'
$ScriptRoot = Split-Path -Parent $MyInvocation.MyCommand.Path

function Write-Section {
    param([Parameter(Mandatory)][string]$Text)
    Write-Host ''
    Write-Host "== $Text ==" -ForegroundColor Cyan
}

function Invoke-CandidatePython {
    param(
        [Parameter(Mandatory)][string]$Exe,
        [string[]]$BaseArguments = @(),
        [Parameter(Mandatory)][string[]]$Arguments
    )

    & $Exe @BaseArguments @Arguments
}

function Join-ProcessArguments {
    param([string[]]$Arguments = @())

    $escaped = foreach ($argument in $Arguments) {
        if ($null -eq $argument) {
            continue
        }

        $text = [string]$argument
        if ($text -notmatch '[\s"]') {
            $text
            continue
        }

        '"' + ($text -replace '"', '\"') + '"'
    }

    return ($escaped -join ' ')
}

function Invoke-ProcessCapture {
    param(
        [Parameter(Mandatory)][string]$Exe,
        [string[]]$Arguments = @()
    )

    $startInfo = New-Object System.Diagnostics.ProcessStartInfo
    $startInfo.FileName = $Exe
    $startInfo.Arguments = Join-ProcessArguments -Arguments $Arguments
    $startInfo.UseShellExecute = $false
    $startInfo.RedirectStandardOutput = $true
    $startInfo.RedirectStandardError = $true
    $startInfo.CreateNoWindow = $true

    $process = New-Object System.Diagnostics.Process
    $process.StartInfo = $startInfo
    if (-not $process.Start()) {
        throw "Failed to start $Exe"
    }

    $stdout = $process.StandardOutput.ReadToEnd()
    $stderr = $process.StandardError.ReadToEnd()
    $process.WaitForExit()

    return [pscustomobject]@{
        ExitCode = $process.ExitCode
        Output = (($stdout + "`n" + $stderr).Trim())
    }
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

function Refresh-ProcessPath {
    $pathParts = New-Object System.Collections.Generic.List[string]

    foreach ($scope in @('Machine', 'User')) {
        $scopePath = [Environment]::GetEnvironmentVariable('Path', $scope)
        if (-not [string]::IsNullOrWhiteSpace($scopePath)) {
            foreach ($part in $scopePath -split ';') {
                if (-not [string]::IsNullOrWhiteSpace($part)) {
                    $pathParts.Add($part.Trim()) | Out-Null
                }
            }
        }
    }

    if (-not [string]::IsNullOrWhiteSpace($env:Path)) {
        foreach ($part in $env:Path -split ';') {
            if (-not [string]::IsNullOrWhiteSpace($part)) {
                $pathParts.Add($part.Trim()) | Out-Null
            }
        }
    }

    $knownRoots = @(
        (Join-Path $env:LocalAppData 'Microsoft\WindowsApps'),
        (Join-Path $env:LocalAppData 'Programs\Python'),
        (Join-Path $env:ProgramFiles 'Python'),
        (Join-Path $env:ProgramFiles 'dotnet')
    )

    if (-not [string]::IsNullOrWhiteSpace(${env:ProgramFiles(x86)})) {
        $knownRoots += (Join-Path ${env:ProgramFiles(x86)} 'dotnet')
    }

    foreach ($root in $knownRoots) {
        if (Test-Path -LiteralPath $root) {
            $pathParts.Add($root) | Out-Null
        }
    }

    $pythonRoots = @(
        (Join-Path $env:LocalAppData 'Programs\Python'),
        (Join-Path $env:ProgramFiles 'Python')
    )

    foreach ($root in $pythonRoots) {
        if (-not (Test-Path -LiteralPath $root)) {
            continue
        }

        Get-ChildItem -LiteralPath $root -Directory -ErrorAction SilentlyContinue |
            ForEach-Object {
                $pathParts.Add($_.FullName) | Out-Null
                $scripts = Join-Path $_.FullName 'Scripts'
                if (Test-Path -LiteralPath $scripts) {
                    $pathParts.Add($scripts) | Out-Null
                }
            }
    }

    $seen = New-Object 'System.Collections.Generic.HashSet[string]' ([System.StringComparer]::OrdinalIgnoreCase)
    $resolvedParts = New-Object System.Collections.Generic.List[string]
    foreach ($part in $pathParts) {
        if ([string]::IsNullOrWhiteSpace($part)) {
            continue
        }
        if ($seen.Add($part)) {
            $resolvedParts.Add($part) | Out-Null
        }
    }

    $env:Path = $resolvedParts -join ';'
}

function New-PythonCandidate {
    param(
        [Parameter(Mandatory)][string]$Exe,
        [string[]]$Arguments = @()
    )

    return [pscustomobject]@{
        Exe = $Exe
        Arguments = $Arguments
    }
}

function Resolve-CandidateExecutable {
    param([Parameter(Mandatory)][string]$Exe)

    if ($Exe -match '[\\/]') {
        if (Test-Path -LiteralPath $Exe) {
            return $Exe
        }
        return $null
    }

    $command = Get-Command $Exe -ErrorAction SilentlyContinue
    if ($command -and $command.Source) {
        return $command.Source
    }

    return $null
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
    $candidates.Add((New-PythonCandidate -Exe $venvPython)) | Out-Null
    $candidates.Add((New-PythonCandidate -Exe 'py' -Arguments @('-3'))) | Out-Null
    $candidates.Add((New-PythonCandidate -Exe 'python')) | Out-Null
    $candidates.Add((New-PythonCandidate -Exe 'python3')) | Out-Null

    $localPrograms = Join-Path $env:LocalAppData 'Programs\Python'
    if (Test-Path -LiteralPath $localPrograms) {
        Get-ChildItem -LiteralPath $localPrograms -Recurse -File -Filter python.exe -ErrorAction SilentlyContinue |
            Sort-Object FullName -Descending |
            ForEach-Object { $candidates.Add((New-PythonCandidate -Exe $_.FullName)) | Out-Null }
    }

    return $candidates
}

function Find-Python {
    function Test-PythonCandidate {
        param(
            [Parameter(Mandatory)][string]$Exe,
            [string[]]$Arguments = @()
        )

        $resolvedExe = Resolve-CandidateExecutable -Exe $Exe
        if (-not $resolvedExe) {
            return $null
        }

        try {
            $probeArguments = @($Arguments) + @('--version')
            $probe = Invoke-ProcessCapture -Exe $resolvedExe -Arguments $probeArguments
            if ($probe.ExitCode -ne 0 -or [string]::IsNullOrWhiteSpace($probe.Output)) {
                return $null
            }

            if ($probe.Output -notmatch 'Python\s+([0-9]+(?:\.[0-9]+){1,2})') {
                return $null
            }

            $version = [version]$Matches[1]
            if ($version -lt $MinimumPythonVersion) {
                return $null
            }

            return [pscustomobject]@{
                Exe = $resolvedExe
                Arguments = $Arguments
                Version = $version
            }
        }
        catch {
            return $null
        }
    }

    $venvPython = Join-Path $ScriptRoot '.venv\Scripts\python.exe'
    foreach ($candidate in @(
        @{ Exe = $venvPython; Arguments = @() },
        @{ Exe = 'py'; Arguments = @('-3') },
        @{ Exe = 'python'; Arguments = @() },
        @{ Exe = 'python3'; Arguments = @() }
    )) {
        $result = Test-PythonCandidate -Exe $candidate.Exe -Arguments $candidate.Arguments
        if ($result) {
            return $result
        }
    }

    $localPrograms = Join-Path $env:LocalAppData 'Programs\Python'
    if (Test-Path -LiteralPath $localPrograms) {
        foreach ($pythonExe in Get-ChildItem -LiteralPath $localPrograms -Recurse -File -Filter python.exe -ErrorAction SilentlyContinue | Sort-Object FullName -Descending) {
            $result = Test-PythonCandidate -Exe $pythonExe.FullName
            if ($result) {
                return $result
            }
        }
    }

    return $null
}

function Install-Python {
    Write-Section -Text 'Python setup'

    $winget = Ensure-Winget

    Write-Host 'Python 3.9+ was not found. Installing Python 3.12 with winget...'
    for ($attempt = 1; $attempt -le $WingetInstallRetryCount; $attempt++) {
        if ($WingetInstallRetryCount -gt 1) {
            Write-Host "winget install attempt $attempt of $WingetInstallRetryCount."
        }

        & $winget install --id Python.Python.3.12 --exact --source winget --accept-package-agreements --accept-source-agreements --disable-interactivity
        $exitCode = $LASTEXITCODE
        Refresh-ProcessPath
        Add-WindowsAppsToPath

        if ($exitCode -eq 0) {
            return
        }

        $installedPython = Find-Python
        if ($installedPython) {
            Write-Host "Python $($installedPython.Version) is available after winget returned exit code $exitCode."
            return
        }

        if ($attempt -lt $WingetInstallRetryCount) {
            Write-Warning "winget failed to install Python. Exit code: $exitCode. Retrying in $attempt second(s)."
            & $winget source update winget --accept-source-agreements --disable-interactivity 2>$null | Out-Null
            Start-Sleep -Seconds $attempt
            continue
        }

        throw "winget failed to install Python after $WingetInstallRetryCount attempt(s). Last exit code: $exitCode"
    }
}

function Find-DotNetSdk {
    $dotnetCommand = Get-Command dotnet.exe -ErrorAction SilentlyContinue
    if (-not $dotnetCommand) {
        return $null
    }

    try {
        $probe = Invoke-ProcessCapture -Exe $dotnetCommand.Source -Arguments @('--list-sdks')
        if ($probe.ExitCode -ne 0 -or [string]::IsNullOrWhiteSpace($probe.Output)) {
            return $null
        }

        $versions = New-Object System.Collections.Generic.List[version]
        foreach ($line in ($probe.Output -split "`r?`n")) {
            if ($line -match '^([0-9]+(?:\.[0-9]+){1,2})') {
                $versions.Add([version]$Matches[1]) | Out-Null
            }
        }

        $selected = $versions |
            Where-Object { $_ -ge $MinimumDotNetSdkVersion } |
            Sort-Object -Descending |
            Select-Object -First 1

        if (-not $selected) {
            return $null
        }

        return [pscustomobject]@{
            Exe = $dotnetCommand.Source
            Version = $selected
        }
    }
    catch {
        return $null
    }
}

function Install-DotNetSdk {
    Write-Section -Text '.NET SDK setup'

    $winget = Ensure-Winget

    Write-Host '.NET SDK 8+ was not found. Installing .NET SDK 8 with winget...'
    for ($attempt = 1; $attempt -le $WingetInstallRetryCount; $attempt++) {
        if ($WingetInstallRetryCount -gt 1) {
            Write-Host "winget install attempt $attempt of $WingetInstallRetryCount."
        }

        & $winget install --id Microsoft.DotNet.SDK.8 --exact --source winget --accept-package-agreements --accept-source-agreements --disable-interactivity
        $exitCode = $LASTEXITCODE
        Refresh-ProcessPath

        $installedDotNet = Find-DotNetSdk
        if ($installedDotNet) {
            if ($exitCode -ne 0) {
                Write-Host ".NET SDK $($installedDotNet.Version) is available after winget returned exit code $exitCode."
            }
            return
        }

        if ($attempt -lt $WingetInstallRetryCount) {
            Write-Warning "winget failed to install .NET SDK 8. Exit code: $exitCode. Retrying in $attempt second(s)."
            & $winget source update winget --accept-source-agreements --disable-interactivity 2>$null | Out-Null
            Start-Sleep -Seconds $attempt
            continue
        }

        throw "winget failed to install .NET SDK 8 after $WingetInstallRetryCount attempt(s). Last exit code: $exitCode"
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
    Invoke-CandidatePython -Exe $Python.Exe -BaseArguments $Python.Arguments -Arguments @('-m', 'pip', 'install', '--disable-pip-version-check', '-r', $requirementsPath)
    if ($LASTEXITCODE -ne 0) {
        throw "pip failed to install requirements. Exit code: $LASTEXITCODE"
    }
}

Write-Section -Text 'WorkshopAnalysis setup'
$python = Find-Python
if (-not $python) {
    Install-Python
    Refresh-ProcessPath
    $python = Find-Python
}

if (-not $python) {
    throw 'Python installation completed, but Python 3.9+ could not be found in this shell. Open a new PowerShell window and run setup.ps1 again.'
}

Write-Host "Using Python $($python.Version)."

$dotnet = Find-DotNetSdk
if (-not $dotnet) {
    Install-DotNetSdk
    Refresh-ProcessPath
    $dotnet = Find-DotNetSdk
}

if (-not $dotnet) {
    throw '.NET SDK installation completed, but .NET SDK 8+ could not be found in this shell. Open a new PowerShell window and run setup.ps1 again.'
}

Write-Host "Using .NET SDK $($dotnet.Version)."
Install-PythonRequirements -Python $python

if ($SkipBootstrap) {
    Write-Host 'Dependency setup complete. Bootstrap was skipped.'
    return
}

Write-Section -Text 'Launching WorkshopAnalysis'
$launcher = Join-Path $ScriptRoot 'WorkshopAnalysis.cmd'
if (-not (Test-Path -LiteralPath $launcher)) {
    throw "WorkshopAnalysis launcher was not found at $launcher"
}

& $launcher
if ($LASTEXITCODE -ne 0) {
    exit $LASTEXITCODE
}
