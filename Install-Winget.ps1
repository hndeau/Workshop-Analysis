<# 
.SYNOPSIS
Installs winget/App Installer in Windows Sandbox or normal Windows VM images where winget is missing.

.DESCRIPTION
Downloads the latest Microsoft winget release, installs required framework packages, then installs
Microsoft.DesktopAppInstaller. It prefers the dependency package published with the winget release
and falls back to Microsoft VCLibs + Microsoft.UI.Xaml from public Microsoft/NuGet endpoints.

In Windows Sandbox, the Microsoft Store winget source often fails with msstore REST errors, so Auto
mode removes msstore and validates the community winget source. On normal VMs, Auto mode preserves
the Microsoft Store source.

Run from an elevated PowerShell session when possible.

.PARAMETER InstallMode
Auto detects Windows Sandbox by the WDAGUtilityAccount profile. Sandbox removes the msstore winget
source by default. Normal preserves it. Use Sandbox or Normal to override detection.

.PARAMETER MicrosoftStoreSource
Auto removes msstore only in Sandbox mode. Keep preserves it. Remove disables it when it breaks
normal package installs.
#>

[CmdletBinding()]
param(
    [string]$DownloadDirectory = (Join-Path $env:TEMP 'winget-bootstrap'),
    [ValidateSet('Auto', 'Sandbox', 'Normal')]
    [string]$InstallMode = 'Auto',
    [ValidateSet('Auto', 'Keep', 'Remove')]
    [string]$MicrosoftStoreSource = 'Auto',
    [switch]$Force,
    [switch]$KeepMicrosoftStoreSource,
    [switch]$KeepDownloads
)

Set-StrictMode -Version Latest
$ErrorActionPreference = 'Stop'

[Net.ServicePointManager]::SecurityProtocol = [Net.ServicePointManager]::SecurityProtocol -bor [Net.SecurityProtocolType]::Tls12

function Write-Step {
    param([Parameter(Mandatory)][string]$Message)
    Write-Host "==> $Message" -ForegroundColor Cyan
}

function Write-Ok {
    param([Parameter(Mandatory)][string]$Message)
    Write-Host "    $Message" -ForegroundColor Green
}

function Test-WindowsSandboxEnvironment {
    if ($env:USERNAME -eq 'WDAGUtilityAccount') {
        return $true
    }

    if ($env:USERPROFILE -and ($env:USERPROFILE -match '\\WDAGUtilityAccount$')) {
        return $true
    }

    return $false
}

function Resolve-InstallMode {
    param([Parameter(Mandatory)][string]$Mode)

    if ($Mode -ne 'Auto') {
        return $Mode
    }

    if (Test-WindowsSandboxEnvironment) {
        return 'Sandbox'
    }

    return 'Normal'
}

function Resolve-MicrosoftStoreSourceAction {
    param(
        [Parameter(Mandatory)][string]$Mode,
        [Parameter(Mandatory)][string]$SourcePolicy,
        [switch]$KeepStoreSource
    )

    if ($KeepStoreSource) {
        return 'Keep'
    }

    if ($SourcePolicy -ne 'Auto') {
        return $SourcePolicy
    }

    if ($Mode -eq 'Sandbox') {
        return 'Remove'
    }

    return 'Keep'
}

function Invoke-Download {
    param(
        [Parameter(Mandatory)][string]$Uri,
        [Parameter(Mandatory)][string]$OutFile
    )

    $parent = Split-Path -Parent $OutFile
    if (-not (Test-Path $parent)) {
        New-Item -ItemType Directory -Path $parent -Force | Out-Null
    }

    Write-Host "    Downloading $Uri"

    try {
        Start-BitsTransfer -Source $Uri -Destination $OutFile -ErrorAction Stop
    }
    catch {
        Invoke-WebRequest -Uri $Uri -OutFile $OutFile -UseBasicParsing -Headers @{
            'User-Agent' = 'winget-bootstrap'
        }
    }

    if (-not (Test-Path $OutFile) -or ((Get-Item $OutFile).Length -eq 0)) {
        throw "Download failed or produced an empty file: $Uri"
    }
}

function Get-NativeArchitecture {
    $arch = $env:PROCESSOR_ARCHITECTURE
    if ($env:PROCESSOR_ARCHITEW6432) {
        $arch = $env:PROCESSOR_ARCHITEW6432
    }

    switch -Regex ($arch) {
        'AMD64' { 'x64'; break }
        'ARM64' { 'arm64'; break }
        'X86'   { 'x86'; break }
        default { throw "Unsupported processor architecture: $arch" }
    }
}

function Test-AppxInstalled {
    param([Parameter(Mandatory)][string]$Name)
    [bool](Get-AppxPackage -Name $Name -ErrorAction SilentlyContinue)
}

function Install-AppxFile {
    param([Parameter(Mandatory)][string]$Path)

    Write-Host "    Installing $(Split-Path -Leaf $Path)"
    Add-AppxPackage -Path $Path -ForceApplicationShutdown
}

function Test-WindowsAppRuntimeInstalled {
    $package = Get-AppxPackage -Name 'Microsoft.WindowsAppRuntime.1.8' -ErrorAction SilentlyContinue |
        Sort-Object Version -Descending |
        Select-Object -First 1

    if (-not $package) {
        return $false
    }

    return ([Version]$package.Version -ge [Version]'8000.616.304.0')
}

function Install-WindowsAppRuntime {
    param(
        [Parameter(Mandatory)][string]$Root,
        [Parameter(Mandatory)][string]$Architecture
    )

    if (Test-WindowsAppRuntimeInstalled) {
        Write-Ok 'Windows App Runtime 1.8 is already installed.'
        return
    }

    $runtimeArchitecture = switch ($Architecture) {
        'x64'   { 'x64' }
        'arm64' { 'arm64' }
        'x86'   { 'x86' }
        default { throw "Unsupported Windows App Runtime architecture: $Architecture" }
    }

    $runtimeInstaller = Join-Path $Root "windowsappruntimeinstall-$runtimeArchitecture.exe"
    $runtimeUrl = "https://aka.ms/windowsappsdk/1.8/latest/windowsappruntimeinstall-$runtimeArchitecture.exe"

    Invoke-Download -Uri $runtimeUrl -OutFile $runtimeInstaller

    Write-Host "    Installing Windows App Runtime 1.8 ($runtimeArchitecture)"
    $process = Start-Process -FilePath $runtimeInstaller -ArgumentList '--quiet' -Wait -PassThru
    if ($process.ExitCode -ne 0) {
        throw "Windows App Runtime installer failed with exit code $($process.ExitCode)."
    }

    if (-not (Test-WindowsAppRuntimeInstalled)) {
        throw 'Windows App Runtime installer completed, but Microsoft.WindowsAppRuntime.1.8 was not found.'
    }
}

function Expand-ZipFile {
    param(
        [Parameter(Mandatory)][string]$ZipPath,
        [Parameter(Mandatory)][string]$Destination
    )

    if (Test-Path $Destination) {
        Remove-Item -Path $Destination -Recurse -Force
    }
    New-Item -ItemType Directory -Path $Destination -Force | Out-Null

    Add-Type -AssemblyName System.IO.Compression.FileSystem
    [IO.Compression.ZipFile]::ExtractToDirectory($ZipPath, $Destination)
}

function Get-WingetCommand {
    $cmd = Get-Command winget.exe -ErrorAction SilentlyContinue
    if ($cmd) {
        return $cmd.Source
    }

    $windowsApps = Join-Path $env:LOCALAPPDATA 'Microsoft\WindowsApps'
    $candidate = Join-Path $windowsApps 'winget.exe'
    if (Test-Path $candidate) {
        return $candidate
    }

    return $null
}

function Invoke-WingetCommand {
    param(
        [Parameter(Mandatory)][string]$WingetPath,
        [Parameter(Mandatory)][string[]]$ArgumentList,
        [switch]$AllowFailure
    )

    Write-Host "    winget $($ArgumentList -join ' ')"
    & $WingetPath @ArgumentList
    $exitCode = $LASTEXITCODE

    if ($exitCode -ne 0 -and -not $AllowFailure) {
        throw "winget $($ArgumentList -join ' ') failed with exit code $exitCode."
    }

    return $exitCode
}

function Initialize-WingetSources {
    param(
        [Parameter(Mandatory)][string]$WingetPath,
        [ValidateSet('Keep', 'Remove')]
        [string]$MicrosoftStoreSourceAction = 'Keep'
    )

    Write-Step 'Configuring winget sources'

    Invoke-WingetCommand -WingetPath $WingetPath -ArgumentList @(
        'source',
        'update',
        '--accept-source-agreements',
        '--disable-interactivity'
    ) -AllowFailure | Out-Null

    if ($MicrosoftStoreSourceAction -eq 'Remove') {
        Invoke-WingetCommand -WingetPath $WingetPath -ArgumentList @(
            'source',
            'remove',
            'msstore',
            '--disable-interactivity'
        ) -AllowFailure | Out-Null
    }
    else {
        Write-Ok 'Keeping the Microsoft Store winget source.'
    }

    Invoke-WingetCommand -WingetPath $WingetPath -ArgumentList @(
        'source',
        'update',
        'winget',
        '--accept-source-agreements',
        '--disable-interactivity'
    ) | Out-Null
}

function Test-WingetCommunitySource {
    param([Parameter(Mandatory)][string]$WingetPath)

    Write-Step 'Testing winget community source'

    Invoke-WingetCommand -WingetPath $WingetPath -ArgumentList @(
        'search',
        '--id',
        'Google.Chrome',
        '--exact',
        '--source',
        'winget',
        '--accept-source-agreements',
        '--disable-interactivity'
    ) | Out-Null
}

function Download-WingetBundle {
    param([Parameter(Mandatory)][string]$Destination)

    $latestDownload = 'https://github.com/microsoft/winget-cli/releases/latest/download/Microsoft.DesktopAppInstaller_8wekyb3d8bbwe.msixbundle'

    try {
        Invoke-Download -Uri $latestDownload -OutFile $Destination
        return
    }
    catch {
        Write-Warning "Direct latest-release download failed. Falling back to the GitHub releases API. $($_.Exception.Message)"
    }

    $release = Invoke-RestMethod -Uri 'https://api.github.com/repos/microsoft/winget-cli/releases/latest' -Headers @{
        'User-Agent' = 'winget-bootstrap'
    }

    $asset = $release.assets |
        Where-Object { $_.name -like 'Microsoft.DesktopAppInstaller*.msixbundle' } |
        Select-Object -First 1

    if (-not $asset) {
        throw 'Could not find Microsoft.DesktopAppInstaller msixbundle in the latest winget release.'
    }

    Invoke-Download -Uri $asset.browser_download_url -OutFile $Destination
}

function Get-DependencyFilesFromReleaseZip {
    param(
        [Parameter(Mandatory)][string]$Root,
        [Parameter(Mandatory)][string]$Architecture
    )

    $zipPath = Join-Path $Root 'DesktopAppInstaller_Dependencies.zip'
    $expandedPath = Join-Path $Root 'release-dependencies'

    try {
        Invoke-Download -Uri 'https://github.com/microsoft/winget-cli/releases/latest/download/DesktopAppInstaller_Dependencies.zip' -OutFile $zipPath
        Expand-ZipFile -ZipPath $zipPath -Destination $expandedPath

        $dependencyFiles = Get-ChildItem -Path $expandedPath -Recurse -File -Include *.appx, *.msix |
            Where-Object {
                $_.FullName -match "\\$Architecture\\" -or
                $_.Name -match "\.$Architecture\." -or
                $_.Name -match "_$Architecture" -or
                ($_.Name -match 'x64|arm64|x86') -eq $false
            }

        $dependencyFiles = $dependencyFiles |
            Where-Object { $_.Name -match 'VCLibs|UI\.Xaml' } |
            Sort-Object @{
                Expression = {
                    if ($_.Name -match 'VCLibs') { 0 }
                    elseif ($_.Name -match 'UI\.Xaml') { 1 }
                    else { 2 }
                }
            }, FullName

        return @($dependencyFiles.FullName)
    }
    catch {
        Write-Warning "Release dependency ZIP path failed. $($_.Exception.Message)"
        return @()
    }
}

function Get-FallbackDependencyFiles {
    param(
        [Parameter(Mandatory)][string]$Root,
        [Parameter(Mandatory)][string]$Architecture
    )

    $files = New-Object System.Collections.Generic.List[string]

    $vclibsPath = Join-Path $Root "Microsoft.VCLibs.$Architecture.14.00.Desktop.appx"
    $vclibsUrl = "https://aka.ms/Microsoft.VCLibs.$Architecture.14.00.Desktop.appx"

    try {
        Invoke-Download -Uri $vclibsUrl -OutFile $vclibsPath
        $files.Add($vclibsPath)
    }
    catch {
        Write-Warning "Could not download VCLibs from $vclibsUrl. $($_.Exception.Message)"
    }

    $xamlPackageVersion = '2.8.7'
    $xamlNupkg = Join-Path $Root "Microsoft.UI.Xaml.$xamlPackageVersion.nupkg"
    $xamlExtracted = Join-Path $Root 'Microsoft.UI.Xaml'
    $xamlUrl = "https://www.nuget.org/api/v2/package/Microsoft.UI.Xaml/$xamlPackageVersion"

    Invoke-Download -Uri $xamlUrl -OutFile $xamlNupkg
    Expand-ZipFile -ZipPath $xamlNupkg -Destination $xamlExtracted

    $xamlPath = Join-Path $xamlExtracted "tools\AppX\$Architecture\Release\Microsoft.UI.Xaml.2.8.appx"
    if (-not (Test-Path $xamlPath)) {
        throw "Could not find Microsoft.UI.Xaml appx at expected path: $xamlPath"
    }

    $files.Add($xamlPath)
    return @($files)
}

function Ensure-Dependencies {
    param(
        [Parameter(Mandatory)][string]$Root,
        [Parameter(Mandatory)][string]$Architecture
    )

    $dependencyFiles = @(Get-DependencyFilesFromReleaseZip -Root $Root -Architecture $Architecture)

    if ($dependencyFiles.Count -eq 0) {
        $dependencyFiles = @(Get-FallbackDependencyFiles -Root $Root -Architecture $Architecture)
    }

    foreach ($dependencyFile in $dependencyFiles) {
        Install-AppxFile -Path $dependencyFile
    }

    return @($dependencyFiles)
}

try {
    $resolvedInstallMode = Resolve-InstallMode -Mode $InstallMode
    $storeSourceAction = Resolve-MicrosoftStoreSourceAction -Mode $resolvedInstallMode -SourcePolicy $MicrosoftStoreSource -KeepStoreSource:$KeepMicrosoftStoreSource

    Write-Step 'Resolving install mode'
    Write-Ok "Install mode: $resolvedInstallMode"
    Write-Ok "Microsoft Store source action: $storeSourceAction"

    Write-Step 'Checking existing winget installation'
    $existingWinget = Get-WingetCommand
    $skipInstall = $false

    if ($existingWinget -and -not $Force) {
        Write-Ok "winget is already installed: $existingWinget"
        $skipInstall = $true
    }

    if (-not $skipInstall) {
        $architecture = Get-NativeArchitecture
        Write-Ok "Detected architecture: $architecture"

        if (-not (Test-Path $DownloadDirectory)) {
            New-Item -ItemType Directory -Path $DownloadDirectory -Force | Out-Null
        }

        Write-Step 'Downloading and installing dependencies'
        $dependencyPaths = @(Ensure-Dependencies -Root $DownloadDirectory -Architecture $architecture)

        Write-Step 'Installing Windows App Runtime'
        Install-WindowsAppRuntime -Root $DownloadDirectory -Architecture $architecture

        Write-Step 'Downloading winget/App Installer'
        $bundlePath = Join-Path $DownloadDirectory 'Microsoft.DesktopAppInstaller_8wekyb3d8bbwe.msixbundle'
        Download-WingetBundle -Destination $bundlePath

        Write-Step 'Installing winget/App Installer'
        if ($dependencyPaths.Count -gt 0) {
            Add-AppxPackage -Path $bundlePath -DependencyPath $dependencyPaths -ForceApplicationShutdown
        }
        else {
            Add-AppxPackage -Path $bundlePath -ForceApplicationShutdown
        }
    }

    $windowsApps = Join-Path $env:LOCALAPPDATA 'Microsoft\WindowsApps'
    if ($env:Path -notlike "*$windowsApps*") {
        $env:Path = "$env:Path;$windowsApps"
    }

    Write-Step 'Verifying winget'
    $winget = Get-WingetCommand
    if (-not $winget) {
        throw 'winget installed, but winget.exe was not found in the current session. Open a new PowerShell window and try winget --version.'
    }

    & $winget --version

    Initialize-WingetSources -WingetPath $winget -MicrosoftStoreSourceAction $storeSourceAction
    Test-WingetCommunitySource -WingetPath $winget

    Write-Ok 'winget is ready.'
    if ($storeSourceAction -eq 'Remove') {
        Write-Host '    Example: winget install --id Google.Chrome --exact --source winget --accept-package-agreements --accept-source-agreements' -ForegroundColor DarkGray
    }
    else {
        Write-Host '    Example: winget install --id Google.Chrome --exact --accept-package-agreements --accept-source-agreements' -ForegroundColor DarkGray
    }
}
finally {
    if (-not $KeepDownloads -and (Test-Path $DownloadDirectory)) {
        Remove-Item -Path $DownloadDirectory -Recurse -Force -ErrorAction SilentlyContinue
    }
}
