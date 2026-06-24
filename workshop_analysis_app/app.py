"""Application workflows and command interpreter for WorkshopAnalysis."""

import os
import platform
import shlex
import shutil
import subprocess
import sys
import threading
import time
import zipfile
from pathlib import Path

from .common import (
    SUPPORTED_GAME_TYPES,
    as_path,
    ensure_directory,
    merge_defaults,
    path_is_relative_to,
    read_json_file,
    save_json_file,
    utc_now_iso,
    write_section,
)
from .database import WorkshopDatabase
from .prompts import prompt_choice, prompt_non_empty, prompt_yes_no
from .tooling import (
    download_file,
    get_steam_app_title,
    get_steam_workshop_item_title,
    install_zip_tool_from_github,
)


class WorkshopAnalysis:
    ANSI_COLORS = {
        "reset": "\033[0m",
        "bold": "\033[1m",
        "muted": "\033[2m",
        "red": "\033[31m",
        "green": "\033[32m",
        "yellow": "\033[33m",
        "blue": "\033[34m",
        "magenta": "\033[35m",
        "cyan": "\033[36m",
        "white": "\033[37m",
    }
    ARCHIVE_EXTENSIONS = {".pak", ".ucas", ".utoc", ".vpk"}
    INTERESTING_METADATA_NAMES = {
        "addoninfo.txt",
        "appmanifest.acf",
        "assetregistry.bin",
        "manifest.vdf",
        "metadata.json",
        "publish_data.txt",
    }
    SUSPICIOUS_EXTENSIONS = {
        ".bat",
        ".cmd",
        ".com",
        ".dll",
        ".exe",
        ".jar",
        ".js",
        ".lnk",
        ".msi",
        ".ps1",
        ".scr",
        ".vbs",
    }

    def __init__(self, state_root, no_tool_bootstrap=False, debug=False):
        self.state_root = Path(state_root)
        self.no_tool_bootstrap = no_tool_bootstrap
        self.debug = debug

    def new_default_config(self):
        tool_root = self.state_root / "tools"
        return {
            "SchemaVersion": 1,
            "CreatedUtc": utc_now_iso(),
            "UpdatedUtc": utc_now_iso(),
            "SteamCmd": {
                "Installed": False,
                "InstallDir": str(tool_root / "steamcmd"),
                "ExePath": None,
            },
            "Defaults": {
                "GameTypeId": None,
                "UseAnonymousSteam": True,
                "WorkshopDownloadRoot": str(self.state_root / "workshop"),
                "ToolRoot": str(tool_root),
            },
            "Tools": {
                "Source2": {
                    "Installed": False,
                    "InstallDir": str(tool_root / "source2viewer"),
                    "CliPath": None,
                },
                "Unreal5": {
                    "Installed": False,
                    "InstallDir": str(tool_root / "unreal5"),
                    "RetocPath": None,
                    "FModelPath": None,
                    "UnrealPakPath": None,
                    "UnrealEngineDir": None,
                },
            },
        }

    def state_paths(self):
        return {
            "ConfigPath": self.state_root / "config.json",
            "DbPath": self.state_root / "workshop_analysis.db",
            "LegacyJsonDbPath": self.state_root / "games.json",
        }

    @staticmethod
    def update_config_timestamp(config):
        config["UpdatedUtc"] = utc_now_iso()

    def load_config(self, config_path):
        default = self.new_default_config()
        config = read_json_file(config_path, default)
        return merge_defaults(default, config)

    @staticmethod
    def describe_game(game):
        count = game.get("WorkshopContentCount")
        count_text = ""
        if count is not None:
            count_text = ", {0} workshop item(s)".format(count)
        game_type = game.get("GameTypeId") or "untyped"
        return "{0} ({1}, {2}{3})".format(
            game.get("Title"),
            game.get("AppId"),
            game_type,
            count_text,
        )

    @staticmethod
    def describe_workshop_content(item):
        downloaded = "downloaded" if item.get("LastDownloadPath") else "not downloaded"
        return "{0} ({1}, {2})".format(
            item.get("Title"),
            item.get("ContentId"),
            downloaded,
        )

    @staticmethod
    def describe_game_with_content(game, item):
        return "{0} / {1}".format(
            game.get("Title"),
            WorkshopAnalysis.describe_workshop_content(item),
        )

    def describe_game_with_content_status(self, config, game, item):
        return "{0} / {1}".format(
            game.get("Title"),
            self.describe_workshop_content_status(config, game, item),
        )

    @staticmethod
    def format_bytes(size):
        size = int(size or 0)
        units = ("B", "KB", "MB", "GB", "TB")
        value = float(size)
        for unit in units:
            if value < 1024 or unit == units[-1]:
                if unit == "B":
                    return "{0} {1}".format(int(value), unit)
                return "{0:.1f} {1}".format(value, unit)
            value /= 1024

    @staticmethod
    def detect_cpu_architecture():
        raw_values = [
            os.environ.get("PROCESSOR_ARCHITEW6432"),
            os.environ.get("PROCESSOR_ARCHITECTURE"),
            platform.machine(),
        ]
        raw = " ".join(value for value in raw_values if value).lower()
        if "arm64" in raw or "aarch64" in raw:
            return "arm64"
        if "amd64" in raw or "x86_64" in raw or "x64" in raw:
            return "x64"
        if "x86" in raw or "i386" in raw or "i686" in raw:
            return "x86"
        return "x64"

    @staticmethod
    def source2_asset_regex_for_arch(architecture):
        if architecture == "arm64":
            return r"(^|[-_])cli-windows-arm64\.zip$|win(dows)?[-_].*arm64.*\.zip$"
        if architecture == "x86":
            return r"(^|[-_])cli-windows-x86\.zip$|win(dows)?[-_].*x86.*\.zip$"
        return r"(^|[-_])cli-windows-x64\.zip$|win(dows)?[-_].*x64.*\.zip$"

    @staticmethod
    def tool_ready_for_game_type(config, game_type_id):
        if game_type_id == "source2":
            cli_path = as_path(config.get("Tools", {}).get("Source2", {}).get("CliPath"))
            return bool(cli_path and cli_path.exists())
        if game_type_id == "unreal5":
            unreal5 = config.get("Tools", {}).get("Unreal5", {})
            for key in ("RetocPath", "FModelPath", "UnrealPakPath"):
                tool_path = as_path(unreal5.get(key))
                if tool_path and tool_path.exists():
                    return True
        return False

    @staticmethod
    def analysis_complete_for_path(content_path):
        content_path = Path(content_path)
        markers = (
            content_path / ".workshop_analysis" / "analysis_complete",
            content_path / ".workshop_analysis" / "analysis_complete.json",
            content_path / "analysis_complete.json",
        )
        return any(marker.exists() for marker in markers)

    def content_has_archive_files(self, content_path, max_files=2000):
        scanned = 0
        try:
            for path in Path(content_path).rglob("*"):
                if path.is_file():
                    scanned += 1
                    if path.suffix.lower() in self.ARCHIVE_EXTENSIONS:
                        return True
                    if scanned >= max_files:
                        return False
        except OSError:
            return False
        return False

    def describe_workshop_content_status(self, config, game, item):
        badges = []
        download_path = as_path(item.get("LastDownloadPath"))
        downloaded = bool(download_path and download_path.exists())
        badges.append("downloaded" if downloaded else "not downloaded")

        if self.tool_ready_for_game_type(config, game.get("GameTypeId")):
            badges.append("tool ready")

        if downloaded and self.content_has_archive_files(download_path):
            badges.append("needs extraction")

        if downloaded and self.analysis_complete_for_path(download_path):
            badges.append("analysis complete")

        last_updated = item.get("LastDownloadUtc")
        if last_updated:
            badges.append("last updated {0}".format(last_updated.replace("T", " ")[:16]))

        return "{0} ({1}) [{2}]".format(
            item.get("Title"),
            item.get("ContentId"),
            "] [".join(badges),
        )

    @staticmethod
    def supported_game_type_ids():
        return {game_type["Id"] for game_type in SUPPORTED_GAME_TYPES}

    @staticmethod
    def workshop_content_paths(config, game, workshop_item):
        paths = []
        last_download_path = as_path(workshop_item.get("LastDownloadPath"))
        if last_download_path:
            paths.append(last_download_path)

        content_parts = [
            "steamapps",
            "workshop",
            "content",
            str(game["AppId"]),
            str(workshop_item["ContentId"]),
        ]

        steam_install_dir = as_path(config.get("SteamCmd", {}).get("InstallDir"))
        if steam_install_dir:
            paths.append(steam_install_dir.joinpath(*content_parts))

        workshop_root = as_path(config.get("Defaults", {}).get("WorkshopDownloadRoot"))
        if workshop_root:
            paths.append(workshop_root.joinpath(*content_parts))

        unique_paths = []
        seen = set()
        for path in paths:
            key = str(path)
            if key not in seen:
                unique_paths.append(path)
                seen.add(key)
        return unique_paths

    @staticmethod
    def allowed_content_roots(config):
        roots = []
        for value in (
            config.get("SteamCmd", {}).get("InstallDir"),
            config.get("Defaults", {}).get("WorkshopDownloadRoot"),
        ):
            root = as_path(value)
            if root:
                roots.append(root)
        return roots

    @staticmethod
    def purge_directory(path, allowed_roots):
        path = Path(path)
        if not path.exists():
            return "missing"

        resolved_path = path.resolve()
        resolved_roots = []
        for root in allowed_roots:
            root = Path(root)
            if root.exists():
                resolved_roots.append(root.resolve())

        if not any(
            resolved_path != root and path_is_relative_to(resolved_path, root)
            for root in resolved_roots
        ):
            raise RuntimeError(
                "Refusing to delete '{0}' because it is not under a configured workshop content root.".format(
                    resolved_path
                )
            )

        if resolved_path.is_dir():
            shutil.rmtree(resolved_path)
            return "removed"

        resolved_path.unlink()
        return "removed"

    def purge_workshop_content(self, config, game, workshop_item):
        allowed_roots = self.allowed_content_roots(config)
        removed = []
        missing = []
        for path in self.workshop_content_paths(config, game, workshop_item):
            result = self.purge_directory(path, allowed_roots)
            if result == "removed":
                removed.append(str(path))
            else:
                missing.append(str(path))
        return removed, missing

    def install_steamcmd(self, config):
        write_section("SteamCMD setup")
        steam_config = config["SteamCmd"]
        install_dir = prompt_non_empty(
            "SteamCMD install directory", steam_config.get("InstallDir")
        )
        install_dir_path = ensure_directory(install_dir)

        zip_path = install_dir_path / "steamcmd.zip"
        exe_path = install_dir_path / "steamcmd.exe"

        if not exe_path.exists():
            print("Downloading SteamCMD...")
            download_file(
                "https://steamcdn-a.akamaihd.net/client/installer/steamcmd.zip",
                zip_path,
            )
            try:
                with zipfile.ZipFile(zip_path) as archive:
                    archive.extractall(install_dir_path)
            finally:
                if zip_path.exists():
                    zip_path.unlink()
        else:
            print("SteamCMD already exists at {0}".format(exe_path))

        steam_config["Installed"] = True
        steam_config["InstallDir"] = str(install_dir_path)
        steam_config["ExePath"] = str(exe_path)

    def ensure_source2_tools(self, config):
        source2 = config["Tools"]["Source2"]
        cli_path = as_path(source2.get("CliPath"))
        if source2.get("Installed") and cli_path and cli_path.exists():
            return

        write_section("Source 2 tool setup")
        if not prompt_yes_no("Install Source 2 Viewer CLI now?", True):
            print("WARNING: Source 2 tools are not installed. Analysis will remain unavailable.")
            return

        install_dir = prompt_non_empty(
            "Source 2 Viewer install directory", source2.get("InstallDir")
        )
        try:
            architecture = self.detect_cpu_architecture()
            asset_regex = self.source2_asset_regex_for_arch(architecture)
            print("Detected CPU architecture: {0}".format(architecture))
            print("Installing Source 2 Viewer CLI asset for {0}.".format(architecture))
            cli_path = install_zip_tool_from_github(
                "ValveResourceFormat/ValveResourceFormat",
                asset_regex,
                install_dir,
                "Source2Viewer-CLI.exe",
            )
            source2["Installed"] = True
            source2["CliPath"] = cli_path
        except Exception as exc:
            print("WARNING: Source 2 Viewer CLI auto-install failed: {0}".format(exc))
            manual_path = input(
                "Optional path to existing Source2Viewer-CLI.exe (blank to skip): "
            ).strip()
            if manual_path and Path(manual_path).exists():
                source2["Installed"] = True
                source2["CliPath"] = manual_path

        source2["InstallDir"] = install_dir

    def ensure_unreal5_tools(self, config):
        unreal5 = config["Tools"]["Unreal5"]
        retoc_path = as_path(unreal5.get("RetocPath"))
        fmodel_path = as_path(unreal5.get("FModelPath"))
        retoc_ok = retoc_path is not None and retoc_path.exists()
        fmodel_ok = fmodel_path is not None and fmodel_path.exists()
        if unreal5.get("Installed") and (retoc_ok or fmodel_ok):
            return

        write_section("Unreal Engine 5 tool setup")
        install_dir = prompt_non_empty("UE5 tool install directory", unreal5.get("InstallDir"))
        ensure_directory(install_dir)

        if not retoc_ok and prompt_yes_no("Install retoc CLI for .utoc/.ucas extraction?", True):
            try:
                retoc_dir = Path(install_dir) / "retoc"
                unreal5["RetocPath"] = install_zip_tool_from_github(
                    "trumank/retoc",
                    r"retoc-x86_64-pc-windows-msvc\.zip$",
                    retoc_dir,
                    "retoc.exe",
                )
            except Exception as exc:
                print("WARNING: retoc auto-install failed: {0}".format(exc))
                manual_path = input("Optional path to existing retoc.exe (blank to skip): ").strip()
                if manual_path and Path(manual_path).exists():
                    unreal5["RetocPath"] = manual_path

        if not fmodel_ok and prompt_yes_no(
            "Install FModel portable build if a release asset is available?", True
        ):
            try:
                fmodel_dir = Path(install_dir) / "FModel"
                unreal5["FModelPath"] = install_zip_tool_from_github(
                    "4sval/FModel",
                    r"FModel.*(win|Windows|x64).*\.zip$|FModel.*\.zip$",
                    fmodel_dir,
                    "FModel.exe",
                )
            except Exception as exc:
                print("WARNING: FModel auto-install failed: {0}".format(exc))
                print("WARNING: You can install FModel manually later and store the path in config.json.")

        engine_dir = input("Optional Unreal Engine install dir for UnrealPak.exe (blank to skip): ").strip()
        if engine_dir:
            unreal_pak = Path(engine_dir) / "Engine" / "Binaries" / "Win64" / "UnrealPak.exe"
            if unreal_pak.exists():
                unreal5["UnrealEngineDir"] = engine_dir
                unreal5["UnrealPakPath"] = str(unreal_pak)
            else:
                print("WARNING: UnrealPak.exe was not found at {0}".format(unreal_pak))

        retoc_path = as_path(unreal5.get("RetocPath"))
        fmodel_path = as_path(unreal5.get("FModelPath"))
        unreal_pak_path = as_path(unreal5.get("UnrealPakPath"))
        unreal5["Installed"] = bool(
            (retoc_path and retoc_path.exists())
            or (fmodel_path and fmodel_path.exists())
            or (unreal_pak_path and unreal_pak_path.exists())
        )
        unreal5["InstallDir"] = install_dir

    def ensure_tools_for_game_type(self, config, game_type_id):
        if self.no_tool_bootstrap:
            return

        if game_type_id == "source2":
            self.ensure_source2_tools(config)
        elif game_type_id == "unreal5":
            self.ensure_unreal5_tools(config)
        else:
            raise RuntimeError("Unsupported game type '{0}'.".format(game_type_id))

    def invoke_bootstrap(self, config, config_path, db_path):
        write_section("Bootstrap")
        ensure_directory(self.state_root)
        ensure_directory(config["Defaults"]["ToolRoot"])
        ensure_directory(config["Defaults"]["WorkshopDownloadRoot"])

        self.install_steamcmd(config)

        game_type = prompt_choice(
            "Default game type analysis",
            SUPPORTED_GAME_TYPES,
            default_id=config["Defaults"].get("GameTypeId"),
        )
        config["Defaults"]["GameTypeId"] = game_type["Id"]

        config["Defaults"]["UseAnonymousSteam"] = prompt_yes_no(
            "Use anonymous SteamCMD login by default?",
            bool(config["Defaults"].get("UseAnonymousSteam", True)),
        )

        self.update_config_timestamp(config)
        save_json_file(config_path, config)

        print()
        print("Bootstrap complete. Config written to {0}".format(config_path))
        print("Game/workshop database initialized at {0}".format(db_path))
        print("Run .\\WorkshopAnalysis to open the command interpreter.")

    def add_game(self, config, database):
        write_section("Add game")
        app_id, title = self.prompt_game_fields()
        game_type = prompt_choice(
            "Game type analysis",
            SUPPORTED_GAME_TYPES,
            default_id=config["Defaults"].get("GameTypeId"),
        )
        game = database.create_game(title, app_id, game_type["Id"])
        print("Added game: {0}".format(self.describe_game(game)))
        return game

    def prompt_game_fields(self, game=None):
        existing_app_id = game.get("AppId") if game else None
        existing_title = game.get("Title") if game else None
        app_id = prompt_non_empty("Steam AppID", existing_app_id)

        title_default = existing_title
        if str(app_id) != str(existing_app_id) or not title_default:
            try:
                resolved_title = get_steam_app_title(app_id)
            except Exception as exc:
                print("WARNING: Could not resolve game title from Steam: {0}".format(exc))
                resolved_title = None

            if resolved_title:
                print("Resolved game title: {0}".format(resolved_title))
                title_default = resolved_title

        title = prompt_non_empty("Game title", title_default)
        return app_id, title

    def prompt_game_type_for_game(self, config, database, game):
        game_type_id = game.get("GameTypeId")
        if game_type_id in self.supported_game_type_ids():
            return game

        write_section("Game type analysis")
        print("Game: {0} ({1})".format(game.get("Title"), game.get("AppId")))
        game_type = prompt_choice(
            "Select analysis type for this game",
            SUPPORTED_GAME_TYPES,
            default_id=config["Defaults"].get("GameTypeId"),
        )
        updated = database.update_game(
            game["Id"],
            game.get("Title"),
            game.get("AppId"),
            game_type["Id"],
        )
        return updated

    def edit_game(self, config, database, game):
        write_section("Edit game")
        app_id, title = self.prompt_game_fields(game)
        game_type = prompt_choice(
            "Game type analysis",
            SUPPORTED_GAME_TYPES,
            default_id=game.get("GameTypeId") or config["Defaults"].get("GameTypeId"),
        )
        updated = database.update_game(game["Id"], title, app_id, game_type["Id"])
        print("Updated game: {0}".format(self.describe_game(updated)))
        return updated

    def delete_game(self, config, database, game):
        items = database.list_workshop_content(game["Id"])
        write_section("Remove game")
        print("Game: {0}".format(self.describe_game(game)))
        print("Associated workshop items: {0}".format(len(items)))
        print("This removes database entries and downloaded workshop content for this game.")
        if not prompt_yes_no("Remove this game?", False):
            print("Game removal cancelled.")
            return False

        for item in items:
            removed, missing = self.purge_workshop_content(config, game, item)
            for path in removed:
                print("Removed installed content: {0}".format(path))
            if not removed and missing:
                print("No installed content found for {0}.".format(item.get("Title")))

        database.delete_game(game["Id"])
        print("Removed game and {0} workshop item database entry/entries.".format(len(items)))
        return True

    def add_workshop_content(self, paths, config, database, game):
        write_section("Add workshop content")
        content_id, title = self.prompt_workshop_content_fields()
        item = database.create_workshop_content(game["Id"], title, content_id)
        print("Added workshop content: {0}".format(self.describe_workshop_content(item)))
        if prompt_yes_no("Download/install this workshop content now?", False):
            game, item, download_path = self.download_and_record_workshop_content(
                paths,
                config,
                database,
                game,
                item,
            )
            self.invoke_analysis_todo(
                config,
                game["GameTypeId"],
                game,
                item,
                download_path,
            )
            self.print_file_inventory(download_path)
            self.offer_analysis_actions(config, game, item, download_path)
            print()
            print("Done.")
        return item

    def prompt_workshop_content_fields(self):
        content_id = prompt_non_empty("Workshop ContentID")
        title_default = None
        try:
            title_default = get_steam_workshop_item_title(content_id)
        except Exception as exc:
            print("WARNING: Could not resolve workshop title from Steam: {0}".format(exc))

        if title_default:
            print("Resolved workshop title: {0}".format(title_default))

        title = prompt_non_empty("Workshop content title", title_default)
        return content_id, title

    def edit_workshop_content(self, database, item):
        write_section("Edit workshop content")
        title = prompt_non_empty("Workshop content title", item.get("Title"))
        content_id = prompt_non_empty("Workshop ContentID", item.get("ContentId"))
        updated = database.update_workshop_content(item["Id"], title, content_id)
        print("Updated workshop content: {0}".format(self.describe_workshop_content(updated)))
        if str(item.get("ContentId")) != str(content_id):
            print("Download metadata was cleared because the ContentID changed.")
        return updated

    def delete_workshop_content(self, config, database, game, item):
        write_section("Remove workshop content")
        print("Workshop content: {0}".format(self.describe_workshop_content(item)))
        print("This removes the database entry and downloaded content for this item.")
        if not prompt_yes_no("Remove this workshop content?", False):
            print("Workshop content removal cancelled.")
            return False

        removed, missing = self.purge_workshop_content(config, game, item)
        for path in removed:
            print("Removed installed content: {0}".format(path))
        if not removed and missing:
            print("No installed content was found for this workshop item.")

        database.delete_workshop_content(item["Id"])
        print("Removed workshop content database entry.")
        return True

    def manage_workshop_content(self, config, database, game, item):
        while True:
            item = database.get_workshop_content(item["Id"])
            if not item:
                print("Workshop content no longer exists.")
                return

            write_section("Manage workshop content")
            print(self.describe_workshop_content_status(config, game, item))
            print("[E] Edit workshop content")
            print("[R] Remove workshop content")
            print("[B] Back")

            answer = input("Select an action: ").strip().lower()
            if answer in ("b", "back", "q", "quit"):
                return
            if answer in ("e", "edit"):
                item = self.edit_workshop_content(database, item)
                continue
            if answer in ("r", "remove", "d", "delete"):
                if self.delete_workshop_content(config, database, game, item):
                    return
                continue
            print("WARNING: Invalid selection.")

    def manage_game(self, paths, config, database, game):
        while True:
            game = database.get_game(game["Id"])
            if not game:
                print("Game no longer exists.")
                return

            items = database.list_workshop_content(game["Id"])
            write_section("Manage game")
            print(self.describe_game(game))
            if items:
                print()
                print("Workshop content:")
                for index, item in enumerate(items, start=1):
                    print(
                        "[{0}] {1}".format(
                            index,
                            self.describe_workshop_content_status(config, game, item),
                        )
                    )
            else:
                print()
                print("No workshop content is associated with this game.")

            print()
            print("[A] Add workshop content")
            print("[E] Edit game")
            print("[R] Remove game")
            print("[B] Back")

            answer = input("Select workshop content or action: ").strip().lower()
            if answer in ("b", "back", "q", "quit"):
                return
            if answer in ("a", "add", "n", "new"):
                self.add_workshop_content(paths, config, database, game)
                continue
            if answer in ("e", "edit"):
                game = self.edit_game(config, database, game)
                continue
            if answer in ("r", "remove", "d", "delete"):
                if self.delete_game(config, database, game):
                    return
                continue

            try:
                index = int(answer)
            except ValueError:
                index = 0
            if 1 <= index <= len(items):
                self.manage_workshop_content(config, database, game, items[index - 1])
                continue
            print("WARNING: Invalid selection.")

    def manage_catalog(self, config, database, paths=None):
        paths = paths or self.state_paths()
        while True:
            games = database.list_games()
            write_section("Game catalog")
            if games:
                for index, game in enumerate(games, start=1):
                    print("[{0}] {1}".format(index, self.describe_game(game)))
            else:
                print("No games are in the catalog.")

            print()
            print("[A] Add game")
            print("[Q] Quit catalog management")

            answer = input("Select a game or action: ").strip().lower()
            if answer in ("q", "quit", "b", "back"):
                return
            if answer in ("a", "add", "n", "new"):
                self.add_game(config, database)
                continue

            try:
                index = int(answer)
            except ValueError:
                index = 0
            if 1 <= index <= len(games):
                self.manage_game(paths, config, database, games[index - 1])
                continue
            print("WARNING: Invalid selection.")

    def select_or_create_game(self, config, database, game_type_id=None):
        write_section("Game entry")
        games = database.list_games()

        if games:
            for index, game in enumerate(games, start=1):
                print("[{0}] {1}".format(index, self.describe_game(game)))
        print("[N] New game entry")
        print("[M] Manage game catalog")

        while True:
            answer = input("Select a game or N: ").strip()
            if not answer and not games:
                answer = "n"
            elif not answer and len(games) == 1:
                answer = "1"
            if answer.lower() in ("n", "new"):
                app_id, title = self.prompt_game_fields()
                game_type = prompt_choice(
                    "Game type analysis",
                    SUPPORTED_GAME_TYPES,
                    default_id=game_type_id or config["Defaults"].get("GameTypeId"),
                )
                return database.create_game(title, app_id, game_type["Id"])
            if answer.lower() in ("m", "manage"):
                self.manage_catalog(config, database)
                return self.select_or_create_game(config, database, game_type_id)

            try:
                index = int(answer)
            except ValueError:
                index = 0
            if 1 <= index <= len(games):
                selected = games[index - 1]
                if game_type_id and not selected.get("GameTypeId"):
                    database.update_game_type(selected["Id"], game_type_id)
                    selected["GameTypeId"] = game_type_id
                    return selected
                return self.prompt_game_type_for_game(config, database, selected)
            print("WARNING: Invalid selection.")

    def select_existing_game(self, database, title="Select game"):
        games = database.list_games()
        write_section(title)
        if not games:
            print("No games are in the catalog.")
            return None

        for index, game in enumerate(games, start=1):
            print("[{0}] {1}".format(index, self.describe_game(game)))
        print("[B] Back")

        while True:
            answer = input("Select a game: ").strip().lower()
            if not answer and len(games) == 1:
                return games[0]
            if answer in ("b", "back", "q", "quit"):
                return None

            try:
                index = int(answer)
            except ValueError:
                index = 0
            if 1 <= index <= len(games):
                return games[index - 1]
            print("WARNING: Invalid selection.")

    def select_or_create_workshop_content(self, config, database, game):
        write_section("Workshop content for {0}".format(game.get("Title")))
        items = database.list_workshop_content(game["Id"])

        if items:
            for index, item in enumerate(items, start=1):
                print(
                    "[{0}] {1}".format(
                        index,
                        self.describe_workshop_content_status(config, game, item),
                    )
                )
        print("[N] New workshop content entry")

        while True:
            answer = input("Select workshop content or N: ").strip()
            if not answer and not items:
                answer = "n"
            elif not answer and len(items) == 1:
                return items[0]
            if answer.lower() in ("n", "new"):
                content_id, title = self.prompt_workshop_content_fields()
                return database.create_workshop_content(game["Id"], title, content_id)

            try:
                index = int(answer)
            except ValueError:
                index = 0
            if 1 <= index <= len(items):
                return items[index - 1]
            print("WARNING: Invalid selection.")

    def invoke_workshop_download(self, config, game, workshop_item):
        steam_config = config["SteamCmd"]
        steamcmd_path = as_path(steam_config.get("ExePath"))
        if not steamcmd_path or not steamcmd_path.exists():
            raise RuntimeError(
                "SteamCMD is not installed or config.json points to a missing steamcmd.exe. "
                "Run bootstrap again."
            )

        workshop_download_root = Path(config["Defaults"]["WorkshopDownloadRoot"])
        ensure_directory(workshop_download_root)

        use_anonymous = config["Defaults"].get("UseAnonymousSteam", True)
        if use_anonymous:
            login_args = ["+login", "anonymous"]
        else:
            print()
            print(
                "SteamCMD will prompt for any required password or Steam Guard code. "
                "Credentials are not stored by this program."
            )
            steam_username = prompt_non_empty("Steam username (not stored)")
            login_args = ["+login", steam_username]

        steamcmd_args = (
            [
                "+force_install_dir",
                str(workshop_download_root),
            ]
            + login_args
            + [
                "+workshop_download_item",
                str(game["AppId"]),
                str(workshop_item["ContentId"]),
                "+quit",
            ]
        )

        write_section("Workshop download")
        print("Game: {0} / AppID {1}".format(game.get("Title"), game.get("AppId")))
        print(
            "Workshop item: {0} / ContentID {1}".format(
                workshop_item.get("Title"), workshop_item.get("ContentId")
            )
        )
        print("SteamCMD: {0}".format(steamcmd_path))

        command = [str(steamcmd_path)] + steamcmd_args
        if use_anonymous:
            result = self.run_steamcmd_with_status(
                command,
                "Downloading workshop content with SteamCMD",
            )
        else:
            result = subprocess.run(command, check=False)

        primary_path = (
            Path(steam_config["InstallDir"])
            / "steamapps"
            / "workshop"
            / "content"
            / str(game["AppId"])
            / str(workshop_item["ContentId"])
        )
        alternate_path = (
            workshop_download_root
            / "steamapps"
            / "workshop"
            / "content"
            / str(game["AppId"])
            / str(workshop_item["ContentId"])
        )

        if primary_path.exists():
            download_path = primary_path
        elif alternate_path.exists():
            download_path = alternate_path
        else:
            if result.returncode != 0:
                raise RuntimeError(
                    "SteamCMD exited with code {0}, and downloaded content was not found under "
                    "'{1}' or '{2}'.".format(result.returncode, primary_path, alternate_path)
                )
            raise RuntimeError(
                "SteamCMD completed, but downloaded content was not found under "
                "'{0}' or '{1}'.".format(primary_path, alternate_path)
            )

        if result.returncode != 0 and result.returncode != 7:
            print(
                "WARNING: SteamCMD exited with code {0}, but downloaded content was found. Continuing.".format(
                    result.returncode
                )
            )

        return download_path

    def run_steamcmd_with_status(self, command, label):
        output_options = {
            "stdout": subprocess.PIPE,
            "stderr": subprocess.STDOUT,
            "text": True,
            "encoding": "utf-8",
            "errors": "replace",
        }

        if self.debug:
            print("{0}...".format(label))
            result = subprocess.run(command, check=False, **output_options)
            self.print_steamcmd_output(getattr(result, "stdout", ""), raw=True)
            return result

        if not sys.stdout.isatty():
            print("{0}...".format(label))
            result = subprocess.run(command, check=False, **output_options)
            print("SteamCMD completed.")
            return result

        result_holder = {}
        error_holder = {}

        def worker():
            try:
                result_holder["result"] = subprocess.run(command, check=False, **output_options)
            except Exception as exc:
                error_holder["error"] = exc

        thread = threading.Thread(target=worker, daemon=True)
        thread.start()
        frames = "|/-\\"
        index = 0
        while thread.is_alive():
            frame = frames[index % len(frames)]
            print(
                "\r{0} {1}".format(self.color_text(frame, "cyan"), label),
                end="",
                flush=True,
            )
            index += 1
            time.sleep(0.12)

        thread.join()
        print("\r{0}\r".format(" " * (len(label) + 4)), end="", flush=True)

        if error_holder:
            raise error_holder["error"]

        result = result_holder["result"]
        if result.returncode == 0 or result.returncode == 7:
            print("{0} {1}".format(self.color_text("OK", "green"), "SteamCMD download finished."))
        else:
            print(
                "{0} SteamCMD finished with exit code {1}.".format(
                    self.color_text("WARNING", "yellow"),
                    result.returncode,
                )
            )
        return result

    @staticmethod
    def print_steamcmd_output(output, raw=False):
        if not output:
            return

        ignored_fragments = (
            "Redirecting stderr to",
            'ILocalize::AddFile() failed to load file "public/steambootstrapper_english.txt"',
            "Failed to clear up temporary update files used for rollback, continuing anyway",
            "Failed to clean up after update, continuing",
            "CWorkThreadPool::~CWorkThreadPool: work processing queue not empty",
        )
        for line in output.splitlines():
            if not raw and any(fragment in line for fragment in ignored_fragments):
                continue
            print(line)

    @staticmethod
    def relative_path_label(base_path, file_path):
        try:
            return str(Path(file_path).relative_to(Path(base_path)))
        except ValueError:
            return str(file_path)

    def build_file_inventory(self, content_path):
        content_path = Path(content_path)
        inventory = {
            "Path": content_path,
            "FileCount": 0,
            "TotalSizeBytes": 0,
            "Extensions": {},
            "ArchiveFiles": [],
            "InterestingMetadata": [],
            "SuspiciousFiles": [],
        }

        if not content_path.exists():
            return inventory

        for path in content_path.rglob("*"):
            if not path.is_file():
                continue

            try:
                size = path.stat().st_size
            except OSError:
                size = 0

            suffix = path.suffix.lower() or "<none>"
            name = path.name.lower()
            label = self.relative_path_label(content_path, path)

            inventory["FileCount"] += 1
            inventory["TotalSizeBytes"] += size
            inventory["Extensions"][suffix] = inventory["Extensions"].get(suffix, 0) + 1

            if suffix in self.ARCHIVE_EXTENSIONS:
                inventory["ArchiveFiles"].append(label)
            if name in self.INTERESTING_METADATA_NAMES:
                inventory["InterestingMetadata"].append(label)
            if suffix in self.SUSPICIOUS_EXTENSIONS:
                inventory["SuspiciousFiles"].append(label)

        return inventory

    @staticmethod
    def print_limited_list(title, values, limit=8):
        if not values:
            return
        print("{0}:".format(title))
        for value in values[:limit]:
            print("  - {0}".format(value))
        remaining = len(values) - limit
        if remaining > 0:
            print("  ... {0} more".format(remaining))

    def print_file_inventory(self, content_path):
        inventory = self.build_file_inventory(content_path)
        write_section("Downloaded file inventory")
        print("Path: {0}".format(inventory["Path"]))
        print("Files: {0}".format(inventory["FileCount"]))
        print("Total size: {0}".format(self.format_bytes(inventory["TotalSizeBytes"])))

        extensions = sorted(
            inventory["Extensions"].items(),
            key=lambda item: (-item[1], item[0]),
        )
        if extensions:
            extension_text = ", ".join(
                "{0}={1}".format(extension, count)
                for extension, count in extensions[:12]
            )
            remaining = len(extensions) - 12
            if remaining > 0:
                extension_text = "{0}, ... {1} more".format(extension_text, remaining)
            print("Extensions: {0}".format(extension_text))
        else:
            print("Extensions: none")

        archive_counts = {}
        for file_name in inventory["ArchiveFiles"]:
            suffix = Path(file_name).suffix.lower()
            archive_counts[suffix] = archive_counts.get(suffix, 0) + 1
        if archive_counts:
            detected = ", ".join(
                "{0}={1}".format(extension, archive_counts[extension])
                for extension in sorted(archive_counts)
            )
            print("Detected package files: {0}".format(detected))
        else:
            print("Detected package files: none")

        self.print_limited_list("Interesting metadata", inventory["InterestingMetadata"])
        self.print_limited_list("Suspicious executable/script-like files", inventory["SuspiciousFiles"])
        return inventory

    def print_content_listing(self, content_path, limit=80):
        write_section("Downloaded content listing")
        content_path = Path(content_path)
        if not content_path.exists():
            print("Content path does not exist: {0}".format(content_path))
            return

        listed = 0
        for path in sorted(content_path.rglob("*")):
            if not path.is_file():
                continue
            print(self.relative_path_label(content_path, path))
            listed += 1
            if listed >= limit:
                break
        if listed == 0:
            print("No files found.")
        elif listed >= limit:
            print("... listing limited to {0} files".format(limit))

    def run_content_scan(self, content_path):
        inventory = self.build_file_inventory(content_path)
        write_section("Content scan")
        if inventory["SuspiciousFiles"]:
            self.print_limited_list(
                "Suspicious executable/script-like files",
                inventory["SuspiciousFiles"],
                limit=20,
            )
        else:
            print("No executable/script-like files were detected by extension.")

        if inventory["InterestingMetadata"]:
            self.print_limited_list("Interesting metadata", inventory["InterestingMetadata"], limit=20)
        else:
            print("No known metadata files were detected.")

        if inventory["ArchiveFiles"]:
            self.print_limited_list("Package files", inventory["ArchiveFiles"], limit=20)
        else:
            print("No VPK/pak/utoc/ucas package files were detected.")

    def run_extract_action(self, config, game, content_path):
        write_section("Extract")
        game_type_id = game.get("GameTypeId")
        if game_type_id == "source2":
            print("Source 2 package extraction should use Source2Viewer-CLI.")
            print("Configured CLI: {0}".format(config["Tools"]["Source2"].get("CliPath") or "not configured"))
        elif game_type_id == "unreal5":
            unreal5 = config["Tools"]["Unreal5"]
            print("UE5 package extraction should use retoc, FModel, or UnrealPak.")
            print("retoc: {0}".format(unreal5.get("RetocPath") or "not configured"))
            print("FModel: {0}".format(unreal5.get("FModelPath") or "not configured"))
            print("UnrealPak: {0}".format(unreal5.get("UnrealPakPath") or "not configured"))
        else:
            print("No extraction action is available for game type '{0}'.".format(game_type_id))
        print("Content path: {0}".format(content_path))

    def run_decompile_action(self, config, game, workshop_item, content_path):
        self.invoke_analysis_todo(
            config,
            game.get("GameTypeId"),
            game,
            workshop_item,
            content_path,
        )

    @staticmethod
    def open_folder(content_path):
        content_path = Path(content_path)
        if not content_path.exists():
            print("Content path does not exist: {0}".format(content_path))
            return
        if sys.platform.startswith("win"):
            subprocess.Popen(["explorer", str(content_path)])
            return
        print("Open this folder manually: {0}".format(content_path))

    def offer_analysis_actions(self, config, game, workshop_item, content_path):
        while True:
            write_section("Analysis actions")
            self.print_menu_item("E", "Extract", "show extraction tooling for this game type", "green")
            self.print_menu_item("L", "List", "print downloaded files", "cyan")
            self.print_menu_item("D", "Decompile/convert", "show conversion/decompile workflow", "magenta")
            self.print_menu_item("R", "Scan", "flag scripts, executables, packages, and metadata", "yellow")
            self.print_menu_item("O", "Open folder", "open downloaded content in Explorer", "blue")
            self.print_menu_item("B", "Back", "return to the previous menu", "white")

            answer = input("Select an action [B]: ").strip().lower()
            if answer in ("", "b", "back", "q", "quit"):
                return
            if answer in ("e", "extract"):
                self.run_extract_action(config, game, content_path)
                continue
            if answer in ("l", "list"):
                self.print_content_listing(content_path)
                continue
            if answer in ("d", "decompile", "convert"):
                self.run_decompile_action(config, game, workshop_item, content_path)
                continue
            if answer in ("r", "scan"):
                self.run_content_scan(content_path)
                continue
            if answer in ("o", "open"):
                self.open_folder(content_path)
                continue
            print("WARNING: Invalid selection.")

    @staticmethod
    def invoke_analysis_todo(config, game_type_id, game, workshop_item, content_path):
        write_section("TODO Analysis")
        print("Content path: {0}".format(content_path))

        if game_type_id == "source2":
            print("Planned Source 2 flow:")
            print("  1. Enumerate VPK/VPK-dir files.")
            print("  2. Use Source2Viewer-CLI to list/export/decompile supported resources.")
            print("  3. Scan extracted output for executable/script red flags.")
            print("  Source2Viewer-CLI: {0}".format(config["Tools"]["Source2"].get("CliPath")))
        elif game_type_id == "unreal5":
            unreal5 = config["Tools"]["Unreal5"]
            print("Planned Unreal Engine 5 flow:")
            print("  1. Enumerate .pak/.utoc/.ucas/AssetRegistry.bin files.")
            print("  2. Use retoc/FModel/UnrealPak where applicable to list/extract cooked assets.")
            print("  3. Scan package listing and extracted output for Binaries/, .dll, .exe, .ps1, .bat, .cmd, .js, etc.")
            print("  retoc: {0}".format(unreal5.get("RetocPath")))
            print("  FModel: {0}".format(unreal5.get("FModelPath")))
            print("  UnrealPak: {0}".format(unreal5.get("UnrealPakPath")))
        else:
            print("No analysis plan exists for '{0}'.".format(game_type_id))

    def load_runtime(self):
        paths = self.state_paths()
        has_config = paths["ConfigPath"].exists()
        config = self.load_config(paths["ConfigPath"])
        database = WorkshopDatabase(paths["DbPath"])
        database.initialize()
        database.migrate_legacy_json(paths["LegacyJsonDbPath"])
        return paths, has_config, config, database

    def download_and_record_workshop_content(
        self,
        paths,
        config,
        database,
        game,
        workshop_item,
        prompt_for_game_type=True,
        ensure_tools=True,
    ):
        if prompt_for_game_type:
            game = self.prompt_game_type_for_game(config, database, game)
        game_type_id = game["GameTypeId"]
        config["Defaults"]["GameTypeId"] = game_type_id
        if ensure_tools:
            self.ensure_tools_for_game_type(config, game_type_id)

        download_path = self.invoke_workshop_download(config, game, workshop_item)
        downloaded_at = utc_now_iso()
        database.update_workshop_download(workshop_item["Id"], downloaded_at, download_path)
        workshop_item["LastDownloadUtc"] = downloaded_at
        workshop_item["LastDownloadPath"] = str(download_path)

        self.update_config_timestamp(config)
        save_json_file(paths["ConfigPath"], config)
        return game, workshop_item, download_path

    @staticmethod
    def find_game_by_app_id(database, app_id):
        for game in database.list_games():
            if str(game.get("AppId")) == str(app_id):
                return game
        return None

    @staticmethod
    def find_workshop_item_by_content_id(database, game_id, content_id):
        for item in database.list_workshop_content(game_id):
            if str(item.get("ContentId")) == str(content_id):
                return item
        return None

    def parse_one_shot_download_args(self, args):
        options = {
            "anonymous": None,
            "ensure_tools": False,
            "game_title": None,
            "game_type_id": None,
            "workshop_title": None,
        }
        positionals = []
        index = 0
        while index < len(args):
            token = args[index]
            if token in ("--anonymous",):
                options["anonymous"] = True
            elif token in ("--no-anonymous",):
                options["anonymous"] = False
            elif token in ("--with-tools", "--tool-bootstrap"):
                options["ensure_tools"] = True
            elif token in ("--no-tool-bootstrap",):
                options["ensure_tools"] = False
            elif token in ("--type", "-t"):
                index += 1
                if index >= len(args):
                    raise ValueError("--type requires a value.")
                options["game_type_id"] = args[index].strip().lower()
            elif token.startswith("--type="):
                options["game_type_id"] = token.split("=", 1)[1].strip().lower()
            elif token in ("--game-title",):
                index += 1
                if index >= len(args):
                    raise ValueError("--game-title requires a value.")
                options["game_title"] = args[index].strip()
            elif token.startswith("--game-title="):
                options["game_title"] = token.split("=", 1)[1].strip()
            elif token in ("--title", "--workshop-title"):
                index += 1
                if index >= len(args):
                    raise ValueError("{0} requires a value.".format(token))
                options["workshop_title"] = args[index].strip()
            elif token.startswith("--title="):
                options["workshop_title"] = token.split("=", 1)[1].strip()
            elif token.startswith("--workshop-title="):
                options["workshop_title"] = token.split("=", 1)[1].strip()
            elif token.startswith("-"):
                raise ValueError("Unknown download option: {0}".format(token))
            else:
                positionals.append(token)
            index += 1

        if len(positionals) != 2:
            raise ValueError(
                "Usage: download <AppID> <WorkshopContentID> "
                "[--type source2|unreal5] [--anonymous]"
            )

        game_type_id = options["game_type_id"]
        if game_type_id and game_type_id not in self.supported_game_type_ids():
            raise ValueError(
                "Unsupported game type '{0}'. Use one of: {1}.".format(
                    game_type_id,
                    ", ".join(sorted(self.supported_game_type_ids())),
                )
            )

        options["app_id"] = positionals[0]
        options["content_id"] = positionals[1]
        return options

    @staticmethod
    def resolve_game_title(app_id, fallback=None):
        if fallback:
            return fallback
        try:
            resolved = get_steam_app_title(app_id)
        except Exception as exc:
            print("WARNING: Could not resolve game title from Steam: {0}".format(exc))
            resolved = None
        return resolved or "Steam App {0}".format(app_id)

    @staticmethod
    def resolve_workshop_title(content_id, fallback=None):
        if fallback:
            return fallback
        try:
            resolved = get_steam_workshop_item_title(content_id)
        except Exception as exc:
            print("WARNING: Could not resolve workshop title from Steam: {0}".format(exc))
            resolved = None
        return resolved or "Workshop item {0}".format(content_id)

    def download_workshop_content_one_shot(self, paths, has_config, config, database, args):
        if not has_config:
            print("No configuration was found. Running bootstrap first.")
            self.invoke_bootstrap(config, paths["ConfigPath"], paths["DbPath"])

        options = self.parse_one_shot_download_args(args)
        if options["anonymous"] is not None:
            config["Defaults"]["UseAnonymousSteam"] = options["anonymous"]

        game = self.find_game_by_app_id(database, options["app_id"])
        game_type_id = (
            options["game_type_id"]
            or (game.get("GameTypeId") if game else None)
            or config["Defaults"].get("GameTypeId")
        )
        if game_type_id not in self.supported_game_type_ids():
            raise ValueError("Game type is not set. Use --type source2 or --type unreal5.")

        game_title = options["game_title"] or (
            game.get("Title") if game else self.resolve_game_title(options["app_id"])
        )
        if game:
            if options["game_title"] or game.get("GameTypeId") != game_type_id:
                game = database.update_game(
                    game["Id"],
                    game_title if options["game_title"] else game.get("Title"),
                    game.get("AppId"),
                    game_type_id,
                )
        else:
            game = database.create_game(game_title, options["app_id"], game_type_id)

        workshop_item = self.find_workshop_item_by_content_id(
            database,
            game["Id"],
            options["content_id"],
        )
        workshop_title = options["workshop_title"] or (
            workshop_item.get("Title")
            if workshop_item
            else self.resolve_workshop_title(options["content_id"])
        )
        if workshop_item:
            if options["workshop_title"]:
                workshop_item = database.update_workshop_content(
                    workshop_item["Id"],
                    workshop_title,
                    workshop_item.get("ContentId"),
                )
        else:
            workshop_item = database.create_workshop_content(
                game["Id"],
                workshop_title,
                options["content_id"],
            )

        game, workshop_item, download_path = self.download_and_record_workshop_content(
            paths,
            config,
            database,
            game,
            workshop_item,
            prompt_for_game_type=False,
            ensure_tools=options["ensure_tools"] and not self.no_tool_bootstrap,
        )
        self.print_file_inventory(download_path)
        print()
        print("Done.")

    def download_workshop_content(self, paths, has_config, config, database):
        if not has_config:
            print("No configuration was found. Running bootstrap first.")
            self.invoke_bootstrap(config, paths["ConfigPath"], paths["DbPath"])
            return

        game = self.select_or_create_game(config, database)
        workshop_item = self.select_or_create_workshop_content(config, database, game)
        game, workshop_item, download_path = self.download_and_record_workshop_content(
            paths,
            config,
            database,
            game,
            workshop_item,
        )

        self.invoke_analysis_todo(
            config,
            game["GameTypeId"],
            game,
            workshop_item,
            download_path,
        )

        self.print_file_inventory(download_path)
        self.offer_analysis_actions(config, game, workshop_item, download_path)

        print()
        print("Done.")

    def prompt_workshop_update_selection(self, candidates):
        if not candidates:
            print("No workshop content is available to update.")
            return []

        write_section("Workshop content update selection")
        for index, candidate in enumerate(candidates, start=1):
            print(
                "[{0}] {1}".format(
                    index,
                    self.describe_game_with_content_status(
                        candidate["Config"],
                        candidate["Game"],
                        candidate["WorkshopItem"],
                    ),
                )
            )
        print("[A] All listed workshop content")
        print("[B] Back")

        while True:
            answer = input("Select items by number, comma-separated list, or A [A]: ").strip().lower()
            if answer in ("b", "back", "q", "quit"):
                return []
            if answer in ("", "a", "all"):
                return candidates

            selected = []
            seen = set()
            parts = answer.replace(",", " ").split()
            for part in parts:
                try:
                    index = int(part)
                except ValueError:
                    index = 0
                if 1 <= index <= len(candidates) and index not in seen:
                    selected.append(candidates[index - 1])
                    seen.add(index)

            if selected:
                return selected
            print("WARNING: Invalid selection.")

    def collect_update_candidates(self, database, game=None, config=None):
        games = [game] if game else database.list_games()
        candidates = []
        for catalog_game in games:
            if not catalog_game:
                continue
            for item in database.list_workshop_content(catalog_game["Id"]):
                candidates.append(
                    {
                        "Config": config or self.new_default_config(),
                        "Game": catalog_game,
                        "WorkshopItem": item,
                    }
                )
        return candidates

    def update_catalog_downloads(self, paths, has_config, config, database):
        if not has_config:
            print("No configuration was found. Running bootstrap first.")
            self.invoke_bootstrap(config, paths["ConfigPath"], paths["DbPath"])
            return

        write_section("Update catalog downloads")
        print("[A] All games")
        print("[G] Select a game")
        print("[B] Back")

        while True:
            answer = input("Select update scope: ").strip().lower()
            if answer in ("b", "back", "q", "quit"):
                return
            if answer in ("", "a", "all"):
                candidates = self.collect_update_candidates(database, config=config)
                break
            if answer in ("g", "game", "select"):
                game = self.select_existing_game(database, "Update downloads for game")
                if not game:
                    return
                candidates = self.collect_update_candidates(database, game, config)
                break
            print("WARNING: Invalid selection.")

        selected = self.prompt_workshop_update_selection(candidates)
        if not selected:
            print("No workshop content selected for update.")
            return

        updated = 0
        for candidate in selected:
            game = candidate["Game"]
            workshop_item = candidate["WorkshopItem"]
            print()
            print(
                "Updating {0}".format(
                    self.describe_game_with_content(game, workshop_item)
                )
            )
            self.download_and_record_workshop_content(
                paths,
                config,
                database,
                game,
                workshop_item,
            )
            updated += 1

        print()
        print("Updated {0} workshop item(s).".format(updated))

    @staticmethod
    def print_command_help():
        print()
        print("WorkshopAnalysis commands:")
        print("  bootstrap      Run first-time setup prompts and write configuration.")
        print("  reconfigure    Re-run bootstrap prompts and update configuration.")
        print("  download       Select a game/workshop item, download it, and show analysis next steps.")
        print("  update         Re-download cataloged workshop content.")
        print("  catalog        Manage games and associated workshop content.")
        print("  status         Show state paths and catalog counts.")
        print("  help           Show this command list.")
        print("  exit           Leave the command interpreter.")
        print()
        print("One-shot download:")
        print("  download <AppID> <WorkshopContentID> --type source2 --anonymous")
        print()
        print("Aliases: run=download, refresh=update, manage=inventory=catalog, quit=exit, ?=help")

    @staticmethod
    def print_status(paths, config, database):
        games = database.list_games()
        workshop_count = sum(int(game.get("WorkshopContentCount") or 0) for game in games)

        write_section("Status")
        print("Config: {0}".format(paths["ConfigPath"]))
        print("Database: {0}".format(paths["DbPath"]))
        print("State root: {0}".format(paths["ConfigPath"].parent))
        print("Default game type: {0}".format(config["Defaults"].get("GameTypeId") or "not set"))
        print("SteamCMD: {0}".format(config["SteamCmd"].get("ExePath") or "not configured"))
        print("Games: {0}".format(len(games)))
        print("Workshop items: {0}".format(workshop_count))

    @staticmethod
    def truncate_text(value, width):
        value = str(value)
        if width <= 0 or len(value) <= width:
            return value
        if width <= 3:
            return value[:width]
        return "{0}...".format(value[: width - 3])

    @staticmethod
    def color_enabled():
        return sys.stdout.isatty() and not os.environ.get("NO_COLOR")

    def color_text(self, value, color_name):
        if not self.color_enabled():
            return str(value)
        color = self.ANSI_COLORS.get(color_name)
        if not color:
            return str(value)
        return "{0}{1}{2}".format(color, value, self.ANSI_COLORS["reset"])

    def accent_key(self, key):
        return self.color_text("[{0}]".format(key), "cyan")

    def status_text(self, value, healthy=True):
        return self.color_text(value, "green" if healthy else "yellow")

    def print_menu_item(self, key, title, detail="", color_name="cyan"):
        key_text = self.color_text("[{0}]".format(key), color_name)
        title_text = self.color_text(title, "bold")
        if detail:
            print("  {0}  {1}  {2}".format(key_text, title_text, self.color_text(detail, "muted")))
        else:
            print("  {0}  {1}".format(key_text, title_text))

    def print_terminal_header(self, title, subtitle=None):
        width = shutil.get_terminal_size((100, 30)).columns
        print(self.color_text(title, "bold"))
        if subtitle:
            print(self.color_text(subtitle, "muted"))
        print(self.color_text("=" * min(width, 80), "blue"))

    @staticmethod
    def enable_terminal_ui():
        if not sys.stdout.isatty() or not sys.platform.startswith("win"):
            return
        try:
            import ctypes

            kernel32 = ctypes.windll.kernel32
            handle = kernel32.GetStdHandle(-11)
            mode = ctypes.c_uint()
            if kernel32.GetConsoleMode(handle, ctypes.byref(mode)):
                kernel32.SetConsoleMode(handle, mode.value | 0x0004)
        except Exception:
            pass

    @staticmethod
    def clear_terminal():
        if sys.stdout.isatty():
            print("\033[2J\033[H", end="")

    @staticmethod
    def read_terminal_action():
        if sys.stdin.isatty() and sys.stdout.isatty() and sys.platform.startswith("win"):
            try:
                import msvcrt

                print("Select action: ", end="", flush=True)
                key = msvcrt.getwch()
                if key in ("\x00", "\xe0"):
                    key = msvcrt.getwch()
                print(key)
                return key.strip()
            except Exception:
                pass
        return input("Select action or command: ").strip()

    @staticmethod
    def pause_terminal_ui():
        if not (sys.stdin.isatty() and sys.stdout.isatty()):
            return
        if sys.platform.startswith("win"):
            try:
                import msvcrt

                print()
                print("Press any key to return to WorkshopAnalysis...", end="", flush=True)
                msvcrt.getwch()
                print()
                return
            except Exception:
                pass
        input("\nPress Enter to return to WorkshopAnalysis...")

    def render_terminal_ui_home(self, paths, config, database):
        self.clear_terminal()
        games = database.list_games()
        workshop_count = sum(int(game.get("WorkshopContentCount") or 0) for game in games)
        width = shutil.get_terminal_size((100, 30)).columns

        steamcmd_path = as_path(config.get("SteamCmd", {}).get("ExePath"))
        steamcmd_status = "ready" if steamcmd_path and steamcmd_path.exists() else "not configured"
        default_type = config.get("Defaults", {}).get("GameTypeId") or "not set"

        self.print_terminal_header("WorkshopAnalysis", "Steam Workshop catalog and inspection workspace")
        print(
            "{0} {1}   {2} {3}   {4} {5}   {6} {7}".format(
                self.color_text("Games:", "muted"),
                len(games),
                self.color_text("Workshop items:", "muted"),
                workshop_count,
                self.color_text("Default type:", "muted"),
                default_type,
                self.color_text("SteamCMD:", "muted"),
                self.status_text(steamcmd_status, steamcmd_status == "ready"),
            )
        )
        print("{0} {1}".format(self.color_text("State:", "muted"), paths["ConfigPath"].parent))
        print()
        print(self.color_text("Primary Actions", "bold"))
        self.print_menu_item("D", "Download", "choose game/workshop content and install it", "green")
        self.print_menu_item("U", "Update", "refresh all or selected catalog downloads", "yellow")
        self.print_menu_item("C", "Catalog", "add, edit, remove, or install content", "cyan")
        print()
        print(self.color_text("Utilities", "bold"))
        self.print_menu_item("S", "Status", "show config, database, and counts", "blue")
        self.print_menu_item("B", "Bootstrap", "run setup or reconfigure defaults", "magenta")
        self.print_menu_item("H", "Help", "show command reference", "white")
        self.print_menu_item(":", "Command", "type a one-off command", "white")
        self.print_menu_item("Q", "Quit", "leave WorkshopAnalysis", "red")

        rows = []
        for game in games:
            for item in database.list_workshop_content(game["Id"]):
                rows.append((item.get("LastDownloadUtc") or "", game, item))
        rows.sort(key=lambda row: row[0], reverse=True)

        if rows:
            print()
            print(self.color_text("Recent Catalog Items", "bold"))
            for _timestamp, game, item in rows[:8]:
                status = self.describe_game_with_content_status(config, game, item)
                print("  - {0}".format(self.truncate_text(status, max(40, width - 6))))
        else:
            print()
            print(self.color_text("Recent Catalog Items", "bold"))
            print("  {0}".format(self.color_text("No workshop content is cataloged yet.", "muted")))

        print()
        print(self.color_text("Tip: .\\WorkshopAnalysis --raw keeps the line-oriented interpreter.", "muted"))

    def tokens_for_terminal_action(self, action, has_config):
        normalized = action.strip()
        lowered = normalized.lower()
        if not normalized:
            return []
        if lowered in ("q", "quit", "exit"):
            return ["exit"]
        if lowered in ("d", "1"):
            return ["download"]
        if lowered in ("u", "2"):
            return ["update"]
        if lowered in ("c", "3"):
            return ["catalog"]
        if lowered in ("s", "4"):
            return ["status"]
        if lowered in ("b", "5"):
            return ["reconfigure" if has_config else "bootstrap"]
        if lowered in ("h", "?", "6"):
            return ["help"]
        if lowered in (":", ";"):
            command = input("Command: ").strip()
            return shlex.split(command) if command else []
        return shlex.split(normalized)

    def execute_command(self, tokens):
        if isinstance(tokens, str):
            tokens = shlex.split(tokens)
        tokens = list(tokens)
        if not tokens:
            return True

        command = tokens[0].strip().lower()
        if command in ("exit", "quit"):
            return False

        if command in ("help", "?", "commands"):
            self.print_command_help()
            return True

        paths, has_config, config, database = self.load_runtime()

        if command in ("bootstrap", "reconfigure"):
            self.invoke_bootstrap(config, paths["ConfigPath"], paths["DbPath"])
            return True

        if command in ("download", "run"):
            if len(tokens) > 1:
                self.download_workshop_content_one_shot(
                    paths,
                    has_config,
                    config,
                    database,
                    tokens[1:],
                )
            else:
                self.download_workshop_content(paths, has_config, config, database)
            return True

        if command in ("update", "refresh"):
            self.update_catalog_downloads(paths, has_config, config, database)
            return True

        if command in ("catalog", "manage", "inventory"):
            self.manage_catalog(config, database, paths)
            return True

        if command == "status":
            self.print_status(paths, config, database)
            return True

        print("Unknown command: {0}".format(tokens[0]))
        print("Run 'help' to list available commands.")
        return True

    def run_shell(self):
        print("WorkshopAnalysis command interpreter")
        print("Type 'help' for commands or 'exit' to quit.")

        paths, has_config, config, database = self.load_runtime()
        if not has_config:
            print()
            print("No configuration was found. Starting initial setup.")
            self.invoke_bootstrap(config, paths["ConfigPath"], paths["DbPath"])

        while True:
            try:
                line = input("WorkshopAnalysis> ").strip()
            except EOFError:
                print()
                return

            try:
                should_continue = self.execute_command(line)
            except ValueError as exc:
                print("ERROR: {0}".format(exc), file=sys.stderr)
                continue
            except Exception as exc:
                print("ERROR: {0}".format(exc), file=sys.stderr)
                continue

            if not should_continue:
                return

    def run_terminal_ui(self):
        self.enable_terminal_ui()
        paths, has_config, config, database = self.load_runtime()
        if not has_config:
            self.clear_terminal()
            print("No configuration was found. Starting initial setup.")
            self.invoke_bootstrap(config, paths["ConfigPath"], paths["DbPath"])
            self.pause_terminal_ui()

        while True:
            paths, has_config, config, database = self.load_runtime()
            self.render_terminal_ui_home(paths, config, database)
            try:
                action = self.read_terminal_action()
                tokens = self.tokens_for_terminal_action(action, has_config)
            except EOFError:
                print()
                return
            except ValueError as exc:
                self.clear_terminal()
                print("ERROR: {0}".format(exc), file=sys.stderr)
                self.pause_terminal_ui()
                continue

            if not tokens:
                continue

            if tokens[0].strip().lower() in ("exit", "quit"):
                return

            self.clear_terminal()
            try:
                should_continue = self.execute_command(tokens)
            except ValueError as exc:
                print("ERROR: {0}".format(exc), file=sys.stderr)
                should_continue = True
            except Exception as exc:
                print("ERROR: {0}".format(exc), file=sys.stderr)
                should_continue = True

            if not should_continue:
                return

            self.pause_terminal_ui()

    def run(self, commands=None, bootstrap=False, reconfigure=False, manage_catalog=False, raw_mode=False):
        if bootstrap:
            commands = ["bootstrap"]
        elif reconfigure:
            commands = ["reconfigure"]
        elif manage_catalog:
            commands = ["catalog"]

        if commands:
            self.execute_command(commands)
            return

        if raw_mode:
            self.run_shell()
        else:
            self.run_terminal_ui()
