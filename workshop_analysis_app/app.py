"""Application workflows and command interpreter for WorkshopAnalysis."""

import shlex
import shutil
import subprocess
import sys
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
from .tooling import download_file, install_zip_tool_from_github


class WorkshopAnalysis:
    def __init__(self, state_root, no_tool_bootstrap=False):
        self.state_root = Path(state_root)
        self.no_tool_bootstrap = no_tool_bootstrap

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
            cli_path = install_zip_tool_from_github(
                "ValveResourceFormat/ValveResourceFormat",
                r"win.*(x64|64).*\.zip$|Source2Viewer.*Windows.*\.zip$|S2V.*Windows.*\.zip$",
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
        title = prompt_non_empty("Game title")
        app_id = prompt_non_empty("Steam AppID")
        game_type = prompt_choice(
            "Game type analysis",
            SUPPORTED_GAME_TYPES,
            default_id=config["Defaults"].get("GameTypeId"),
        )
        game = database.create_game(title, app_id, game_type["Id"])
        print("Added game: {0}".format(self.describe_game(game)))
        return game

    def edit_game(self, config, database, game):
        write_section("Edit game")
        title = prompt_non_empty("Game title", game.get("Title"))
        app_id = prompt_non_empty("Steam AppID", game.get("AppId"))
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

    def add_workshop_content(self, database, game):
        write_section("Add workshop content")
        title = prompt_non_empty("Workshop content title")
        content_id = prompt_non_empty("Workshop ContentID")
        item = database.create_workshop_content(game["Id"], title, content_id)
        print("Added workshop content: {0}".format(self.describe_workshop_content(item)))
        return item

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
            print(self.describe_workshop_content(item))
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

    def manage_game(self, config, database, game):
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
                    print("[{0}] {1}".format(index, self.describe_workshop_content(item)))
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
                self.add_workshop_content(database, game)
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

    def manage_catalog(self, config, database):
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
                self.manage_game(config, database, games[index - 1])
                continue
            print("WARNING: Invalid selection.")

    def select_or_create_game(self, config, database, game_type_id):
        write_section("Game entry")
        games = database.list_games()

        if games:
            for index, game in enumerate(games, start=1):
                print("[{0}] {1} ({2})".format(index, game.get("Title"), game.get("AppId")))
        print("[N] New game entry")
        print("[M] Manage game catalog")

        while True:
            answer = input("Select a game or N: ").strip()
            if answer.lower() in ("n", "new"):
                title = prompt_non_empty("Game title")
                app_id = prompt_non_empty("Steam AppID")
                return database.create_game(title, app_id, game_type_id)
            if answer.lower() in ("m", "manage"):
                self.manage_catalog(config, database)
                return self.select_or_create_game(config, database, game_type_id)

            try:
                index = int(answer)
            except ValueError:
                index = 0
            if 1 <= index <= len(games):
                selected = games[index - 1]
                if not selected.get("GameTypeId"):
                    database.update_game_type(selected["Id"], game_type_id)
                    selected["GameTypeId"] = game_type_id
                return selected
            print("WARNING: Invalid selection.")

    def select_or_create_workshop_content(self, database, game):
        write_section("Workshop content for {0}".format(game.get("Title")))
        items = database.list_workshop_content(game["Id"])

        if items:
            for index, item in enumerate(items, start=1):
                print("[{0}] {1} ({2})".format(index, item.get("Title"), item.get("ContentId")))
        print("[N] New workshop content entry")

        while True:
            answer = input("Select workshop content or N: ").strip()
            if answer.lower() in ("n", "new"):
                title = prompt_non_empty("Workshop content title")
                content_id = prompt_non_empty("Workshop ContentID")
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

        if config["Defaults"].get("UseAnonymousSteam", True):
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
            login_args
            + [
                "+force_install_dir",
                str(workshop_download_root),
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

        result = subprocess.run([str(steamcmd_path)] + steamcmd_args, check=False)
        if result.returncode != 0:
            raise RuntimeError("SteamCMD exited with code {0}.".format(result.returncode))

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
            raise RuntimeError(
                "SteamCMD completed, but downloaded content was not found under "
                "'{0}' or '{1}'.".format(primary_path, alternate_path)
            )

        return download_path

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

    def download_workshop_content(self, paths, has_config, config, database):
        if not has_config:
            print("No configuration was found. Running bootstrap first.")
            self.invoke_bootstrap(config, paths["ConfigPath"], paths["DbPath"])
            return

        default_game_type_id = config["Defaults"].get("GameTypeId")
        game_type = prompt_choice(
            "Game type analysis",
            SUPPORTED_GAME_TYPES,
            default_id=default_game_type_id,
        )
        config["Defaults"]["GameTypeId"] = game_type["Id"]
        self.ensure_tools_for_game_type(config, game_type["Id"])

        game = self.select_or_create_game(config, database, game_type["Id"])
        workshop_item = self.select_or_create_workshop_content(database, game)
        download_path = self.invoke_workshop_download(config, game, workshop_item)
        downloaded_at = utc_now_iso()
        database.update_workshop_download(workshop_item["Id"], downloaded_at, download_path)
        workshop_item["LastDownloadUtc"] = downloaded_at
        workshop_item["LastDownloadPath"] = str(download_path)

        self.update_config_timestamp(config)
        save_json_file(paths["ConfigPath"], config)

        self.invoke_analysis_todo(
            config,
            game_type["Id"],
            game,
            workshop_item,
            download_path,
        )

        print()
        print("Done.")

    @staticmethod
    def print_command_help():
        print()
        print("WorkshopAnalysis commands:")
        print("  bootstrap      Run first-time setup prompts and write configuration.")
        print("  reconfigure    Re-run bootstrap prompts and update configuration.")
        print("  download       Select a game/workshop item, download it, and show analysis next steps.")
        print("  catalog        Manage games and associated workshop content.")
        print("  status         Show state paths and catalog counts.")
        print("  help           Show this command list.")
        print("  exit           Leave the command interpreter.")
        print()
        print("Aliases: run=download, manage=inventory=catalog, quit=exit, ?=help")

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
            self.download_workshop_content(paths, has_config, config, database)
            return True

        if command in ("catalog", "manage", "inventory"):
            self.manage_catalog(config, database)
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

    def run(self, commands=None, bootstrap=False, reconfigure=False, manage_catalog=False):
        if bootstrap:
            commands = ["bootstrap"]
        elif reconfigure:
            commands = ["reconfigure"]
        elif manage_catalog:
            commands = ["catalog"]

        if commands:
            self.execute_command(commands)
            return

        self.run_shell()
