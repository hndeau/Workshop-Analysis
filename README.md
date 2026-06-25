# Workshop Analysis

Workshop Analysis is a Windows-oriented command-line tool for downloading Steam Workshop content and organizing repeatable inspection workflows for Source 2 and Unreal Engine 5 workshop items.

The tool keeps reusable game and workshop metadata in a local SQLite catalog, stores runtime configuration separately, and can bootstrap SteamCMD plus optional game-specific analysis tools.

## Status

This project is early-stage. It currently handles setup, catalog management, Steam Workshop downloads, Source 2 file listing analysis, and analysis workflow scaffolding for deeper tooling.

Supported game/tooling profiles:

- Source 2: Source 2 Viewer CLI / ValveResourceFormat-oriented workflow.
- Unreal Engine 5: retoc, FModel, and UnrealPak-oriented workflow.

## Requirements

- Windows PowerShell 5.1 or newer.
- Python 3.9 or newer.
- Internet access for first-time setup and tool downloads.
- SteamCMD, installed by the bootstrap flow.

If Python is missing, `setup.ps1` attempts to install it with `winget`. If `winget` is also missing, setup first runs `Install-Winget.ps1` to bootstrap winget/App Installer.

Runtime Python package dependencies are listed in `requirements.txt`. The project currently uses only the Python standard library, so the file contains no active third-party packages.

## First-Time Setup

Run setup once from Command Prompt or PowerShell:

```cmd
.\setup.cmd
```

`setup.cmd` is the Windows setup entrypoint. It invokes PowerShell with an execution-policy bypass for this script run:

```cmd
powershell.exe -NoProfile -ExecutionPolicy Bypass -File .\setup.ps1
```

`setup.ps1` remains the setup implementation and is only for initial dependency setup. It:

1. Finds Python 3.9+ if already installed.
2. Installs winget when needed by invoking `Install-Winget.ps1`.
3. Installs Python 3.12 through winget when Python is missing.
4. Installs Python package requirements if `requirements.txt` exists.
5. Runs `.\WorkshopAnalysis`.

To validate dependencies without launching bootstrap:

```cmd
.\setup.cmd -SkipBootstrap
```

After setup, use `.\WorkshopAnalysis` directly for future runs. The first interpreter launch automatically walks through bootstrap if no configuration exists.

## Running

Open Workshop Analysis:

```powershell
.\WorkshopAnalysis
```

By default, this opens an in-place terminal UI that redraws a color-coded dashboard and action menu instead of appending every command prompt to the terminal history. If this is the first run and `state\config.json` does not exist, the UI starts initial setup before showing the dashboard.

Use the action keys shown in the UI, or press `:` to type a command directly. Common commands are:

```text
WorkshopAnalysis> help
WorkshopAnalysis> bootstrap
WorkshopAnalysis> download
WorkshopAnalysis> download 730 3735111145 --type source2 --anonymous
WorkshopAnalysis> analyze
WorkshopAnalysis> catalog
WorkshopAnalysis> status
WorkshopAnalysis> exit
```

To preserve the original raw text input/output interpreter:

```powershell
.\WorkshopAnalysis --raw
```

Normal operation suppresses routine SteamCMD chatter and shows a concise running status/spinner during downloads. To show raw SteamCMD output while diagnosing a problem:

```powershell
.\WorkshopAnalysis --debug download 730 3735111145 --type source2 --anonymous
```

Use `reconfigure` later to revisit bootstrap settings without deleting catalog data.

`download` asks for the game first, then uses the selected game's analysis type to choose the Source 2 or Unreal Engine 5 workflow. New games still prompt for an analysis type when they are created.

When adding games, enter the Steam AppID first. The tool attempts to resolve the game title from Steam app metadata and uses it as the title default. When adding workshop content, enter the Workshop ContentID first. The tool attempts to resolve the workshop title from Steam's published-file metadata and uses it as the title default. Manual title entry remains available when either lookup is unavailable or incorrect.

Use `catalog` to re-download cataloged workshop content. From the catalog menu, `U` updates all or selected workshop items across the catalog. From a specific game menu, `U` updates all or selected workshop items for that game. Blank input accepts obvious defaults; in update selection, blank input selects all listed workshop content.

After a download, Workshop Analysis prints a file inventory with file count, total size, extension counts, detected VPK/pak/utoc/ucas package files, interesting metadata such as `publish_data.txt`, and executable/script-like files worth reviewing. The guided flow then offers inline actions:

```text
[A] Analyze automatic
[M] Analyze manual
[L] List downloaded files
[O] Open folder
[B] Back
```

Analysis is routed automatically by the selected game's type. The user chooses only the mode:

- Automatic: lists likely code, scripts, config, package contents, and other programmatic files while excluding low-signal assets such as textures/audio/models.
- Manual: lists every detected file, still ordered by potential security severity.

Each analysis writes a full raw report to `state\analysis\<AppID>\<ContentID>\analysis.json`. That report includes every observed file, generated file, event, warning, and error from the analysis pass, including corrupt or partially corrupt archives that may indicate decompression risk. Automatic and manual modes only control the curated presentation shown to the user; they do not reduce what is written to `analysis.json`.

Source 2 analysis currently scans downloaded files, safely expands ZIP archives, parses VPK directory files to list contained package entries, records archive/VPK parsing errors as events, and marks the downloaded content as analyzed.

Unreal Engine 5 analysis uses the same report flow. It scans downloaded files, safely expands ZIP archives, classifies UE package/config/script files, records pak/IO Store containers, and uses configured tools when available: `retoc` lists and extracts `.utoc`/`.ucas` IO Store contents, and `UnrealPak.exe` lists and extracts `.pak` contents into `state\analysis\<AppID>\<ContentID>\extracted`. During analysis bootstrap the app attempts to install every supported distributable UE parser/hook into the configured UE5 tool directory: retoc, FModel as the CUE4Parse-backed parser, UAssetGUI/UAssetAPI-compatible tooling, and kismet-analyzer. It also discovers common Unreal Engine installs for `UnrealPak.exe`, discovers local Oodle runtime DLLs when present, creates the `.usmap` mappings directory, and validates required tools with harmless help commands. UE5 is considered analysis-ready only when both `retoc.exe` and `UnrealPak.exe` are configured and validated. `UnrealPak.exe` is distributed with Unreal Engine under `Engine\Binaries\Win64\UnrealPak.exe`; point the bootstrap prompt at either that executable or the Unreal Engine install directory if auto-discovery does not find it.

UE5 reports now separate evidence from interpretation. `analysis.json` includes tool status, normalized container records, container entries, extracted files, loose/extracted script/config findings with hashes and safe previews, best-effort `AssetRegistry.bin` string recovery, cooked Blueprint/data-logic asset findings, and explicit blockers for missing parsers, missing `.usmap` mappings, missing Oodle support, encrypted content, and missing Kismet analysis support. Advanced paths can still be configured later for alternate CUE4Parse/FModel-compatible parsers, UAssetAPI/UAssetGUI-compatible parsers, `kismet-analyzer`, `.usmap` mappings, `Crypto.json` or an AES key, and Oodle. These hooks improve cooked asset and Blueprint bytecode recovery when the external backend supports it; the tool does not claim to recover original C++ source from cooked packages.

You can also run one command and exit, similar to tools like SBT:

```powershell
.\WorkshopAnalysis download
.\WorkshopAnalysis download 730 3735111145 --type source2 --anonymous
.\WorkshopAnalysis analyze
.\WorkshopAnalysis catalog
.\WorkshopAnalysis status
```

The one-shot download form creates or reuses catalog entries, resolves Steam titles when available, downloads the workshop item, records download metadata, and prints the same file inventory without opening the interactive action menu. Optional flags include `--type source2`, `--type unreal5`, `--anonymous`, `--no-anonymous`, `--game-title`, `--title` / `--workshop-title`, and `--with-tools`.

Use a custom state directory:

```powershell
.\WorkshopAnalysis -StateRoot C:\Path\To\State
```

Skip optional Source 2 / UE5 tool checks for download commands:

```powershell
.\WorkshopAnalysis --no-tool-bootstrap download
```

When installing Source 2 Viewer CLI, the tool detects the local CPU architecture and selects the matching release asset, for example `cli-windows-x64.zip` on typical Windows Sandbox and x64 VM installs.

## Catalog Management

Open the catalog manager from the interpreter:

```text
WorkshopAnalysis> catalog
```

The catalog manager supports:

- Add, edit, and remove games.
- Add, edit, and remove workshop content associated with a game.
- Prompt to download/install newly added workshop content immediately.
- Status badges for workshop items, including downloaded/not downloaded, tool ready, needs extraction, analysis complete, and last updated.
- Re-download all or selected workshop content from the catalog or game menus.
- Purge downloaded workshop content when removing a workshop item.
- Purge all associated workshop content when removing a game.

Deletion is constrained to configured SteamCMD/workshop content roots to avoid deleting arbitrary paths.

The normal download flow also exposes catalog management from the game selection prompt with `M`.

## State Files

By default, state is stored under `.\state`:

- `config.json`: SteamCMD path, tool paths, default game type, and download preferences.
- `workshop_analysis.db`: SQLite database containing games and workshop content.
- `tools\`: downloaded SteamCMD and optional analysis tools.
- `workshop\`: workshop download root.

The `state/` directory is ignored by Git.

Older `state/games.json` catalogs are migrated into SQLite automatically if `workshop_analysis.db` is empty.

## Project Layout

- `workshop_analysis.py`: compatibility entrypoint that preserves the existing import and launcher surface.
- `workshop_analysis_app\cli.py`: command-line parsing and process entrypoint.
- `workshop_analysis_app\app.py`: interactive workflows, command interpreter, downloads, and catalog orchestration.
- `workshop_analysis_app\analysis.py`: game-type analysis, Source 2 VPK listing, archive expansion, and severity ordering.
- `workshop_analysis_app\database.py`: SQLite schema, migration, and catalog persistence.
- `workshop_analysis_app\tooling.py`: download helpers and external tool installation helpers.
- `workshop_analysis_app\prompts.py`: reusable interactive prompt helpers.
- `workshop_analysis_app\common.py`: shared constants, path helpers, and JSON config helpers.

## Tests

Run the test suite:

```powershell
python -m unittest discover -v
```

The tests use only the Python standard library. They cover bootstrap behavior, SQLite catalog persistence, game/workshop add/edit/remove flows, download metadata, Source 2 analysis, deletion safety, tool setup branches, and CLI error handling.

The implementation is split into reusable modules under `workshop_analysis_app`, while `workshop_analysis.py` remains the stable wrapper for existing launchers and imports.

## Repository Description

Windows CLI for downloading Steam Workshop content, managing a local SQLite game/workshop catalog, and scaffolding Source 2 and Unreal Engine 5 inspection workflows.
