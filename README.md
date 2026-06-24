# Workshop Analysis

Workshop Analysis is a Windows-oriented command-line tool for downloading Steam Workshop content and organizing repeatable inspection workflows for Source 2 and Unreal Engine 5 workshop items.

The tool keeps reusable game and workshop metadata in a local SQLite catalog, stores runtime configuration separately, and can bootstrap SteamCMD plus optional game-specific analysis tools.

## Status

This project is early-stage. It currently handles setup, catalog management, Steam Workshop downloads, and analysis workflow scaffolding. The actual deep content analysis steps are still placeholders.

Supported game/tooling profiles:

- Source 2: Source 2 Viewer CLI / ValveResourceFormat-oriented workflow.
- Unreal Engine 5: retoc, FModel, and UnrealPak-oriented workflow.

## Requirements

- Windows PowerShell 5.1 or newer.
- Python 3.9 or newer.
- Internet access for first-time setup and tool downloads.
- SteamCMD, installed by the bootstrap flow.

If Python is missing, `setup.ps1` attempts to install it with `winget`. If `winget` is also missing, setup first runs `Install-Winget.ps1` to bootstrap winget/App Installer.

## First-Time Setup

Run setup once from PowerShell:

```powershell
.\setup.ps1
```

`setup.ps1` is only for initial dependency setup. It:

1. Finds Python 3.9+ if already installed.
2. Installs winget when needed by invoking `Install-Winget.ps1`.
3. Installs Python 3.12 through winget when Python is missing.
4. Installs Python package requirements if `requirements.txt` exists.
5. Runs `.\WorkshopAnalysis -Bootstrap`.

To validate dependencies without launching bootstrap:

```powershell
.\setup.ps1 -SkipBootstrap
```

After setup, use `.\WorkshopAnalysis` directly for future runs.

## Running

Start the normal interactive download flow:

```powershell
.\WorkshopAnalysis
```

Re-run bootstrap:

```powershell
.\WorkshopAnalysis -Reconfigure
```

Skip optional Source 2 / UE5 tool checks for one run:

```powershell
.\WorkshopAnalysis -NoToolBootstrap
```

Use a custom state directory:

```powershell
.\WorkshopAnalysis -StateRoot C:\Path\To\State
```

## Catalog Management

Open the catalog manager:

```powershell
.\WorkshopAnalysis -ManageCatalog
```

The catalog manager supports:

- Add, edit, and remove games.
- Add, edit, and remove workshop content associated with a game.
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

## Tests

Run the test suite:

```powershell
python -m unittest discover -v
```

The tests use only the Python standard library. They cover bootstrap behavior, SQLite catalog persistence, game/workshop add/edit/remove flows, download metadata, deletion safety, tool setup branches, and CLI error handling.

Recent local coverage estimate using Python's standard-library `trace` module:

```text
workshop_analysis.py stdlib trace coverage: 902/965 lines (93.5%)
```

## Repository Description

Windows CLI for downloading Steam Workshop content, managing a local SQLite game/workshop catalog, and scaffolding Source 2 and Unreal Engine 5 inspection workflows.
