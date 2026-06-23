#requires -Version 5.1
<#
.SYNOPSIS
    Downloads Steam Workshop content and prepares game-type-specific inspection tools.

.DESCRIPTION
    State is stored in JSON files under .\state by default:
      - config.json: install paths, selected game type, anonymous login preference.
      - games.json: reusable game entries and their workshop content entries.

    The script never stores Steam usernames or passwords. SteamCMD is allowed to
    handle interactive authentication when anonymous login is disabled.

.EXAMPLE
    .\WorkshopContentInspector.ps1

    First run bootstraps config. Later runs prompt for game type, game entry,
    workshop content entry, then download the selected Workshop item.

.EXAMPLE
    .\WorkshopContentInspector.ps1 -Reconfigure

    Re-run bootstrap prompts and rewrite reusable configuration.

.EXAMPLE
    .\WorkshopContentInspector.ps1 -NoToolBootstrap

    Skip Source 2 / UE5 tool installation checks for this run.
#>

[CmdletBinding()]
param(
    [string]$StateRoot = (Join-Path $PSScriptRoot 'state'),
    [switch]$Bootstrap,
    [switch]$Reconfigure,
    [switch]$NoToolBootstrap
)

Set-StrictMode -Version 3.0
$ErrorActionPreference = 'Stop'

$Script:SupportedGameTypes = @(
    [pscustomobject]@{
        Id          = 'source2'
        Name        = 'Source 2'
        Description = 'Source 2 / VPK analysis with Source 2 Viewer CLI (ValveResourceFormat).'
    },
    [pscustomobject]@{
        Id          = 'unreal5'
        Name        = 'Unreal Engine 5'
        Description = 'UE5 pak/utoc/ucas analysis with retoc/FModel-oriented tooling.'
    }
)

function Write-Section {
    param([Parameter(Mandatory)][string]$Text)
    Write-Host ''
    Write-Host "== $Text ==" -ForegroundColor Cyan
}

function Ensure-Directory {
    param([Parameter(Mandatory)][string]$Path)
    if (-not (Test-Path -LiteralPath $Path)) {
        New-Item -ItemType Directory -Path $Path | Out-Null
    }
}

function Read-JsonFile {
    param(
        [Parameter(Mandatory)][string]$Path,
        [Parameter(Mandatory)]$Default
    )

    if (-not (Test-Path -LiteralPath $Path)) {
        return $Default
    }

    $raw = Get-Content -LiteralPath $Path -Raw
    if ([string]::IsNullOrWhiteSpace($raw)) {
        return $Default
    }

    return $raw | ConvertFrom-Json
}

function Save-JsonFile {
    param(
        [Parameter(Mandatory)][string]$Path,
        [Parameter(Mandatory)]$Value
    )

    $parent = Split-Path -Parent $Path
    Ensure-Directory -Path $parent
    $Value | ConvertTo-Json -Depth 20 | Set-Content -LiteralPath $Path -Encoding UTF8
}

function Invoke-FileDownload {
    param(
        [Parameter(Mandatory)][string]$Uri,
        [Parameter(Mandatory)][string]$OutFile
    )

    $parameters = @{
        Uri     = $Uri
        OutFile = $OutFile
    }

    if ($PSVersionTable.PSVersion.Major -lt 6) {
        $parameters.UseBasicParsing = $true
    }

    Invoke-WebRequest @parameters
}

function New-DefaultConfig {
    $toolRoot = Join-Path $StateRoot 'tools'
    [pscustomobject]@{
        SchemaVersion = 1
        CreatedUtc    = (Get-Date).ToUniversalTime().ToString('o')
        UpdatedUtc    = (Get-Date).ToUniversalTime().ToString('o')
        SteamCmd      = [pscustomobject]@{
            Installed = $false
            InstallDir = (Join-Path $toolRoot 'steamcmd')
            ExePath    = $null
        }
        Defaults      = [pscustomobject]@{
            GameTypeId          = $null
            UseAnonymousSteam   = $true
            WorkshopDownloadRoot = (Join-Path $StateRoot 'workshop')
            ToolRoot            = $toolRoot
        }
        Tools         = [pscustomobject]@{
            Source2 = [pscustomobject]@{
                Installed = $false
                InstallDir = (Join-Path $toolRoot 'source2viewer')
                CliPath    = $null
            }
            Unreal5 = [pscustomobject]@{
                Installed       = $false
                InstallDir      = (Join-Path $toolRoot 'unreal5')
                RetocPath       = $null
                FModelPath      = $null
                UnrealPakPath   = $null
                UnrealEngineDir = $null
            }
        }
    }
}

function New-DefaultDatabase {
    [pscustomobject]@{
        SchemaVersion = 1
        Games = @()
    }
}

function Get-StatePaths {
    [pscustomobject]@{
        ConfigPath = Join-Path $StateRoot 'config.json'
        DbPath     = Join-Path $StateRoot 'games.json'
    }
}

function Update-ConfigTimestamp {
    param([Parameter(Mandatory)]$Config)
    $Config.UpdatedUtc = (Get-Date).ToUniversalTime().ToString('o')
}

function Prompt-Choice {
    param(
        [Parameter(Mandatory)][string]$Title,
        [Parameter(Mandatory)][object[]]$Choices,
        [string]$LabelProperty = 'Name',
        [string]$DefaultId,
        [string]$IdProperty = 'Id'
    )

    Write-Section -Text $Title
    for ($i = 0; $i -lt $Choices.Count; $i++) {
        $choice = $Choices[$i]
        $label = [string]$choice.$LabelProperty
        $id = [string]$choice.$IdProperty
        $suffix = if ($DefaultId -and $id -eq $DefaultId) { ' [default]' } else { '' }
        Write-Host ("[{0}] {1}{2}" -f ($i + 1), $label, $suffix)
        if ($choice.PSObject.Properties.Name -contains 'Description') {
            Write-Host "    $($choice.Description)" -ForegroundColor DarkGray
        }
    }

    while ($true) {
        $answer = Read-Host 'Select a number'
        if ([string]::IsNullOrWhiteSpace($answer) -and $DefaultId) {
            return $Choices | Where-Object { [string]$_.$IdProperty -eq $DefaultId } | Select-Object -First 1
        }
        $index = 0
        if ([int]::TryParse($answer, [ref]$index) -and $index -ge 1 -and $index -le $Choices.Count) {
            return $Choices[$index - 1]
        }
        Write-Warning 'Invalid selection.'
    }
}

function Prompt-YesNo {
    param(
        [Parameter(Mandatory)][string]$Question,
        [bool]$Default = $true
    )

    $suffix = if ($Default) { '[Y/n]' } else { '[y/N]' }
    while ($true) {
        $answer = Read-Host "$Question $suffix"
        if ([string]::IsNullOrWhiteSpace($answer)) {
            return $Default
        }
        switch -Regex ($answer.Trim()) {
            '^(y|yes)$' { return $true }
            '^(n|no)$'  { return $false }
            default     { Write-Warning 'Please answer yes or no.' }
        }
    }
}

function Prompt-NonEmpty {
    param(
        [Parameter(Mandatory)][string]$Prompt,
        [string]$Default
    )

    while ($true) {
        $suffix = if ($Default) { " [$Default]" } else { '' }
        $value = Read-Host "$Prompt$suffix"
        if ([string]::IsNullOrWhiteSpace($value) -and $Default) {
            return $Default
        }
        if (-not [string]::IsNullOrWhiteSpace($value)) {
            return $value.Trim()
        }
        Write-Warning 'A value is required.'
    }
}

function Install-SteamCmd {
    param([Parameter(Mandatory)]$Config)

    Write-Section -Text 'SteamCMD setup'
    $installDir = Prompt-NonEmpty -Prompt 'SteamCMD install directory' -Default $Config.SteamCmd.InstallDir
    Ensure-Directory -Path $installDir

    $zipPath = Join-Path $installDir 'steamcmd.zip'
    $exePath = Join-Path $installDir 'steamcmd.exe'

    if (-not (Test-Path -LiteralPath $exePath)) {
        Write-Host 'Downloading SteamCMD...'
        Invoke-FileDownload -Uri 'https://steamcdn-a.akamaihd.net/client/installer/steamcmd.zip' -OutFile $zipPath
        Expand-Archive -LiteralPath $zipPath -DestinationPath $installDir -Force
        Remove-Item -LiteralPath $zipPath -Force
    }
    else {
        Write-Host "SteamCMD already exists at $exePath"
    }

    $Config.SteamCmd.Installed = $true
    $Config.SteamCmd.InstallDir = $installDir
    $Config.SteamCmd.ExePath = $exePath
}

function Get-GitHubLatestReleaseAsset {
    param(
        [Parameter(Mandatory)][string]$Repository,
        [Parameter(Mandatory)][string]$AssetNameRegex
    )

    $release = Invoke-RestMethod -Uri "https://api.github.com/repos/$Repository/releases/latest" -Headers @{
        'User-Agent' = 'WorkshopContentInspector'
    }

    $asset = $release.assets |
        Where-Object { $_.name -match $AssetNameRegex } |
        Select-Object -First 1

    if (-not $asset) {
        throw "Could not find a release asset matching '$AssetNameRegex' in $Repository latest release."
    }

    return $asset
}

function Install-ZipToolFromGitHub {
    param(
        [Parameter(Mandatory)][string]$Repository,
        [Parameter(Mandatory)][string]$AssetNameRegex,
        [Parameter(Mandatory)][string]$InstallDir,
        [Parameter(Mandatory)][string]$ExpectedExeName
    )

    Ensure-Directory -Path $InstallDir
    $asset = Get-GitHubLatestReleaseAsset -Repository $Repository -AssetNameRegex $AssetNameRegex
    $zipPath = Join-Path $InstallDir $asset.name
    Write-Host "Downloading $($asset.name) from $Repository..."
    Invoke-FileDownload -Uri $asset.browser_download_url -OutFile $zipPath
    Expand-Archive -LiteralPath $zipPath -DestinationPath $InstallDir -Force
    Remove-Item -LiteralPath $zipPath -Force

    $exe = Get-ChildItem -LiteralPath $InstallDir -Recurse -File -Filter $ExpectedExeName |
        Select-Object -First 1

    if (-not $exe) {
        throw "Installed $Repository, but could not find $ExpectedExeName under $InstallDir."
    }

    return $exe.FullName
}

function Ensure-Source2Tools {
    param([Parameter(Mandatory)]$Config)

    if ($Config.Tools.Source2.Installed -and
        $Config.Tools.Source2.CliPath -and
        (Test-Path -LiteralPath $Config.Tools.Source2.CliPath)) {
        return
    }

    Write-Section -Text 'Source 2 tool setup'
    if (-not (Prompt-YesNo -Question 'Install Source 2 Viewer CLI now?' -Default $true)) {
        Write-Warning 'Source 2 tools are not installed. Analysis will remain unavailable.'
        return
    }

    $installDir = Prompt-NonEmpty -Prompt 'Source 2 Viewer install directory' -Default $Config.Tools.Source2.InstallDir
    try {
        $cliPath = Install-ZipToolFromGitHub `
            -Repository 'ValveResourceFormat/ValveResourceFormat' `
            -AssetNameRegex 'win.*(x64|64).*\.zip$|Source2Viewer.*Windows.*\.zip$|S2V.*Windows.*\.zip$' `
            -InstallDir $installDir `
            -ExpectedExeName 'Source2Viewer-CLI.exe'

        $Config.Tools.Source2.Installed = $true
        $Config.Tools.Source2.CliPath = $cliPath
    }
    catch {
        Write-Warning "Source 2 Viewer CLI auto-install failed: $($_.Exception.Message)"
        $manualPath = Read-Host 'Optional path to existing Source2Viewer-CLI.exe (blank to skip)'
        if (-not [string]::IsNullOrWhiteSpace($manualPath) -and (Test-Path -LiteralPath $manualPath)) {
            $Config.Tools.Source2.Installed = $true
            $Config.Tools.Source2.CliPath = $manualPath
        }
    }

    $Config.Tools.Source2.InstallDir = $installDir
}

function Ensure-Unreal5Tools {
    param([Parameter(Mandatory)]$Config)

    $retocOk = $Config.Tools.Unreal5.RetocPath -and (Test-Path -LiteralPath $Config.Tools.Unreal5.RetocPath)
    $fmodelOk = $Config.Tools.Unreal5.FModelPath -and (Test-Path -LiteralPath $Config.Tools.Unreal5.FModelPath)
    if ($Config.Tools.Unreal5.Installed -and ($retocOk -or $fmodelOk)) {
        return
    }

    Write-Section -Text 'Unreal Engine 5 tool setup'
    $installDir = Prompt-NonEmpty -Prompt 'UE5 tool install directory' -Default $Config.Tools.Unreal5.InstallDir
    Ensure-Directory -Path $installDir

    if (-not $retocOk -and (Prompt-YesNo -Question 'Install retoc CLI for .utoc/.ucas extraction?' -Default $true)) {
        try {
            $retocDir = Join-Path $installDir 'retoc'
            $Config.Tools.Unreal5.RetocPath = Install-ZipToolFromGitHub `
                -Repository 'trumank/retoc' `
                -AssetNameRegex 'retoc-x86_64-pc-windows-msvc\.zip$' `
                -InstallDir $retocDir `
                -ExpectedExeName 'retoc.exe'
        }
        catch {
            Write-Warning "retoc auto-install failed: $($_.Exception.Message)"
            $manualPath = Read-Host 'Optional path to existing retoc.exe (blank to skip)'
            if (-not [string]::IsNullOrWhiteSpace($manualPath) -and (Test-Path -LiteralPath $manualPath)) {
                $Config.Tools.Unreal5.RetocPath = $manualPath
            }
        }
    }

    if (-not $fmodelOk -and (Prompt-YesNo -Question 'Install FModel portable build if a release asset is available?' -Default $true)) {
        try {
            $fmodelDir = Join-Path $installDir 'FModel'
            $Config.Tools.Unreal5.FModelPath = Install-ZipToolFromGitHub `
                -Repository '4sval/FModel' `
                -AssetNameRegex 'FModel.*(win|Windows|x64).*\.zip$|FModel.*\.zip$' `
                -InstallDir $fmodelDir `
                -ExpectedExeName 'FModel.exe'
        }
        catch {
            Write-Warning "FModel auto-install failed: $($_.Exception.Message)"
            Write-Warning 'You can install FModel manually later and store the path in config.json.'
        }
    }

    $engineDir = Read-Host 'Optional Unreal Engine install dir for UnrealPak.exe (blank to skip)'
    if (-not [string]::IsNullOrWhiteSpace($engineDir)) {
        $unrealPak = Join-Path $engineDir 'Engine\Binaries\Win64\UnrealPak.exe'
        if (Test-Path -LiteralPath $unrealPak) {
            $Config.Tools.Unreal5.UnrealEngineDir = $engineDir
            $Config.Tools.Unreal5.UnrealPakPath = $unrealPak
        }
        else {
            Write-Warning "UnrealPak.exe was not found at $unrealPak"
        }
    }

    $Config.Tools.Unreal5.Installed = [bool](
        ($Config.Tools.Unreal5.RetocPath -and (Test-Path -LiteralPath $Config.Tools.Unreal5.RetocPath)) -or
        ($Config.Tools.Unreal5.FModelPath -and (Test-Path -LiteralPath $Config.Tools.Unreal5.FModelPath)) -or
        ($Config.Tools.Unreal5.UnrealPakPath -and (Test-Path -LiteralPath $Config.Tools.Unreal5.UnrealPakPath))
    )
    $Config.Tools.Unreal5.InstallDir = $installDir
}

function Ensure-ToolsForGameType {
    param(
        [Parameter(Mandatory)]$Config,
        [Parameter(Mandatory)][string]$GameTypeId
    )

    if ($NoToolBootstrap) {
        return
    }

    switch ($GameTypeId) {
        'source2' { Ensure-Source2Tools -Config $Config }
        'unreal5' { Ensure-Unreal5Tools -Config $Config }
        default { throw "Unsupported game type '$GameTypeId'." }
    }
}

function Invoke-Bootstrap {
    param(
        [Parameter(Mandatory)]$Config,
        [Parameter(Mandatory)][string]$ConfigPath,
        [Parameter(Mandatory)][string]$DbPath
    )

    Write-Section -Text 'Bootstrap'
    Ensure-Directory -Path $StateRoot
    Ensure-Directory -Path $Config.Defaults.ToolRoot
    Ensure-Directory -Path $Config.Defaults.WorkshopDownloadRoot

    Install-SteamCmd -Config $Config

    $gameType = Prompt-Choice -Title 'Default game type analysis' -Choices $Script:SupportedGameTypes -DefaultId $Config.Defaults.GameTypeId
    $Config.Defaults.GameTypeId = $gameType.Id

    $Config.Defaults.UseAnonymousSteam = Prompt-YesNo -Question 'Use anonymous SteamCMD login by default?' -Default ([bool]$Config.Defaults.UseAnonymousSteam)

    Update-ConfigTimestamp -Config $Config
    Save-JsonFile -Path $ConfigPath -Value $Config

    if (-not (Test-Path -LiteralPath $DbPath)) {
        Save-JsonFile -Path $DbPath -Value (New-DefaultDatabase)
    }

    Write-Host ''
    Write-Host "Bootstrap complete. Config written to $ConfigPath" -ForegroundColor Green
    Write-Host 'Run the script again without -Bootstrap to select a game and workshop item.'
}

function Select-OrCreateGame {
    param(
        [Parameter(Mandatory)]$Database,
        [Parameter(Mandatory)][string]$GameTypeId
    )

    Write-Section -Text 'Game entry'
    $games = @($Database.Games)

    if ($games.Count -gt 0) {
        for ($i = 0; $i -lt $games.Count; $i++) {
            Write-Host ("[{0}] {1} ({2})" -f ($i + 1), $games[$i].Title, $games[$i].AppId)
        }
    }
    Write-Host '[N] New game entry'

    while ($true) {
        $answer = Read-Host 'Select a game or N'
        if ($answer -match '^(n|new)$') {
            $title = Prompt-NonEmpty -Prompt 'Game title'
            $appId = Prompt-NonEmpty -Prompt 'Steam AppID'
            $game = [pscustomobject]@{
                Id = [guid]::NewGuid().ToString()
                Title = $title
                AppId = $appId
                GameTypeId = $GameTypeId
                CreatedUtc = (Get-Date).ToUniversalTime().ToString('o')
                WorkshopContent = @()
            }
            $Database.Games = @($Database.Games) + $game
            return $game
        }

        $index = 0
        if ([int]::TryParse($answer, [ref]$index) -and $index -ge 1 -and $index -le $games.Count) {
            $selected = $games[$index - 1]
            if (-not ($selected.PSObject.Properties.Name -contains 'GameTypeId')) {
                $selected | Add-Member -NotePropertyName 'GameTypeId' -NotePropertyValue $GameTypeId
            }
            elseif ([string]::IsNullOrWhiteSpace($selected.GameTypeId)) {
                $selected.GameTypeId = $GameTypeId
            }
            return $selected
        }
        Write-Warning 'Invalid selection.'
    }
}

function Select-OrCreateWorkshopContent {
    param([Parameter(Mandatory)]$Game)

    Write-Section -Text "Workshop content for $($Game.Title)"
    $items = @($Game.WorkshopContent)

    if ($items.Count -gt 0) {
        for ($i = 0; $i -lt $items.Count; $i++) {
            Write-Host ("[{0}] {1} ({2})" -f ($i + 1), $items[$i].Title, $items[$i].ContentId)
        }
    }
    Write-Host '[N] New workshop content entry'

    while ($true) {
        $answer = Read-Host 'Select workshop content or N'
        if ($answer -match '^(n|new)$') {
            $title = Prompt-NonEmpty -Prompt 'Workshop content title'
            $contentId = Prompt-NonEmpty -Prompt 'Workshop ContentID'
            $item = [pscustomobject]@{
                Id = [guid]::NewGuid().ToString()
                Title = $title
                ContentId = $contentId
                CreatedUtc = (Get-Date).ToUniversalTime().ToString('o')
                LastDownloadUtc = $null
                LastDownloadPath = $null
            }
            $Game.WorkshopContent = @($Game.WorkshopContent) + $item
            return $item
        }

        $index = 0
        if ([int]::TryParse($answer, [ref]$index) -and $index -ge 1 -and $index -le $items.Count) {
            return $items[$index - 1]
        }
        Write-Warning 'Invalid selection.'
    }
}

function Invoke-WorkshopDownload {
    param(
        [Parameter(Mandatory)]$Config,
        [Parameter(Mandatory)]$Game,
        [Parameter(Mandatory)]$WorkshopItem
    )

    if (-not $Config.SteamCmd.ExePath -or -not (Test-Path -LiteralPath $Config.SteamCmd.ExePath)) {
        throw 'SteamCMD is not installed or config.json points to a missing steamcmd.exe. Run bootstrap again.'
    }

    Ensure-Directory -Path $Config.Defaults.WorkshopDownloadRoot
    $loginArgs = if ($Config.Defaults.UseAnonymousSteam) {
        @('+login', 'anonymous')
    }
    else {
        Write-Host ''
        Write-Host 'SteamCMD will prompt for any required password or Steam Guard code. Credentials are not stored by this script.' -ForegroundColor Yellow
        $steamUsername = Prompt-NonEmpty -Prompt 'Steam username (not stored)'
        @('+login', $steamUsername)
    }

    $steamCmdArgs = @() +
        $loginArgs +
        @(
            '+force_install_dir', $Config.Defaults.WorkshopDownloadRoot,
            '+workshop_download_item', $Game.AppId, $WorkshopItem.ContentId,
            '+quit'
        )

    Write-Section -Text 'Workshop download'
    Write-Host "Game: $($Game.Title) / AppID $($Game.AppId)"
    Write-Host "Workshop item: $($WorkshopItem.Title) / ContentID $($WorkshopItem.ContentId)"
    Write-Host "SteamCMD: $($Config.SteamCmd.ExePath)"

    & $Config.SteamCmd.ExePath @steamCmdArgs
    if ($LASTEXITCODE -ne 0) {
        throw "SteamCMD exited with code $LASTEXITCODE."
    }

    $primaryPath = Join-Path $Config.SteamCmd.InstallDir "steamapps\workshop\content\$($Game.AppId)\$($WorkshopItem.ContentId)"
    $alternatePath = Join-Path $Config.Defaults.WorkshopDownloadRoot "steamapps\workshop\content\$($Game.AppId)\$($WorkshopItem.ContentId)"
    if (Test-Path -LiteralPath $primaryPath) {
        $downloadPath = $primaryPath
    }
    elseif (Test-Path -LiteralPath $alternatePath) {
        $downloadPath = $alternatePath
    }
    else {
        throw "SteamCMD completed, but downloaded content was not found under '$primaryPath' or '$alternatePath'."
    }

    $WorkshopItem.LastDownloadUtc = (Get-Date).ToUniversalTime().ToString('o')
    $WorkshopItem.LastDownloadPath = $downloadPath
    return $downloadPath
}

function Invoke-AnalysisTodo {
    param(
        [Parameter(Mandatory)]$Config,
        [Parameter(Mandatory)][string]$GameTypeId,
        [Parameter(Mandatory)]$Game,
        [Parameter(Mandatory)]$WorkshopItem,
        [Parameter(Mandatory)][string]$ContentPath
    )

    Write-Section -Text 'TODO Analysis'
    Write-Host "Content path: $ContentPath"

    switch ($GameTypeId) {
        'source2' {
            Write-Host 'Planned Source 2 flow:'
            Write-Host '  1. Enumerate VPK/VPK-dir files.'
            Write-Host '  2. Use Source2Viewer-CLI to list/export/decompile supported resources.'
            Write-Host '  3. Scan extracted output for executable/script red flags.'
            Write-Host "  Source2Viewer-CLI: $($Config.Tools.Source2.CliPath)"
        }
        'unreal5' {
            Write-Host 'Planned Unreal Engine 5 flow:'
            Write-Host '  1. Enumerate .pak/.utoc/.ucas/AssetRegistry.bin files.'
            Write-Host '  2. Use retoc/FModel/UnrealPak where applicable to list/extract cooked assets.'
            Write-Host '  3. Scan package listing and extracted output for Binaries/, .dll, .exe, .ps1, .bat, .cmd, .js, etc.'
            Write-Host "  retoc: $($Config.Tools.Unreal5.RetocPath)"
            Write-Host "  FModel: $($Config.Tools.Unreal5.FModelPath)"
            Write-Host "  UnrealPak: $($Config.Tools.Unreal5.UnrealPakPath)"
        }
        default {
            Write-Host "No analysis plan exists for '$GameTypeId'."
        }
    }
}

function Invoke-Main {
    $paths = Get-StatePaths
    $hasConfig = Test-Path -LiteralPath $paths.ConfigPath
    $config = Read-JsonFile -Path $paths.ConfigPath -Default (New-DefaultConfig)
    $database = Read-JsonFile -Path $paths.DbPath -Default (New-DefaultDatabase)

    if ($Bootstrap -or $Reconfigure -or -not $hasConfig) {
        Invoke-Bootstrap -Config $config -ConfigPath $paths.ConfigPath -DbPath $paths.DbPath
        return
    }

    $defaultGameTypeId = $config.Defaults.GameTypeId
    $gameType = Prompt-Choice -Title 'Game type analysis' -Choices $Script:SupportedGameTypes -DefaultId $defaultGameTypeId
    $config.Defaults.GameTypeId = $gameType.Id
    Ensure-ToolsForGameType -Config $config -GameTypeId $gameType.Id

    $game = Select-OrCreateGame -Database $database -GameTypeId $gameType.Id
    $workshopItem = Select-OrCreateWorkshopContent -Game $game
    $downloadPath = Invoke-WorkshopDownload -Config $config -Game $game -WorkshopItem $workshopItem

    Update-ConfigTimestamp -Config $config
    Save-JsonFile -Path $paths.ConfigPath -Value $config
    Save-JsonFile -Path $paths.DbPath -Value $database

    Invoke-AnalysisTodo -Config $config -GameTypeId $gameType.Id -Game $game -WorkshopItem $workshopItem -ContentPath $downloadPath

    Write-Host ''
    Write-Host 'Done.' -ForegroundColor Green
}

Invoke-Main
