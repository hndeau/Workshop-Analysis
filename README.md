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

Open the command interpreter:

```powershell
.\WorkshopAnalysis
```

If this is the first run and `state\config.json` does not exist, the interpreter starts initial setup before showing the command prompt.

Inside the interpreter, use commands:

```text
WorkshopAnalysis> help
WorkshopAnalysis> bootstrap
WorkshopAnalysis> download
WorkshopAnalysis> update
WorkshopAnalysis> catalog
WorkshopAnalysis> status
WorkshopAnalysis> exit
```

Use `reconfigure` later to revisit bootstrap settings without deleting catalog data.

`download` asks for the game first, then uses the selected game's analysis type to choose the Source 2 or Unreal Engine 5 workflow. New games still prompt for an analysis type when they are created.

When adding games, enter the Steam AppID first. The tool attempts to resolve the game title from Steam app metadata and uses it as the title default. When adding workshop content, enter the Workshop ContentID first. The tool attempts to resolve the workshop title from Steam's published-file metadata and uses it as the title default. Manual title entry remains available when either lookup is unavailable or incorrect.

Use `update` to re-download cataloged workshop content. It can update all workshop items, selected workshop items across the catalog, or selected items for one game.

You can also run one command and exit, similar to tools like SBT:

```powershell
.\WorkshopAnalysis download
.\WorkshopAnalysis update
.\WorkshopAnalysis catalog
.\WorkshopAnalysis status
```

Use a custom state directory:

```powershell
.\WorkshopAnalysis -StateRoot C:\Path\To\State
```

Skip optional Source 2 / UE5 tool checks for download commands:

```powershell
.\WorkshopAnalysis --no-tool-bootstrap download
```

## Catalog Management

Open the catalog manager from the interpreter:

```text
WorkshopAnalysis> catalog
```

The catalog manager supports:

- Add, edit, and remove games.
- Add, edit, and remove workshop content associated with a game.
- Re-download all or selected workshop content with the `update` command.
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
- `workshop_analysis_app\database.py`: SQLite schema, migration, and catalog persistence.
- `workshop_analysis_app\tooling.py`: download helpers and external tool installation helpers.
- `workshop_analysis_app\prompts.py`: reusable interactive prompt helpers.
- `workshop_analysis_app\common.py`: shared constants, path helpers, and JSON config helpers.

## Tests

Run the test suite:

```powershell
python -m unittest discover -v
```

The tests use only the Python standard library. They cover bootstrap behavior, SQLite catalog persistence, game/workshop add/edit/remove flows, download metadata, deletion safety, tool setup branches, and CLI error handling.

The implementation is split into reusable modules under `workshop_analysis_app`, while `workshop_analysis.py` remains the stable wrapper for existing launchers and imports.

## Repository Description

Windows CLI for downloading Steam Workshop content, managing a local SQLite game/workshop catalog, and scaffolding Source 2 and Unreal Engine 5 inspection workflows.
