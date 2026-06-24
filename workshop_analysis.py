#!/usr/bin/env python3
"""
Downloads Steam Workshop content and prepares game-type-specific inspection tools.

State is stored under ./state by default:
  - config.json: install paths, selected game type, anonymous login preference.
  - workshop_analysis.db: reusable game entries and their workshop content entries.

The program never stores Steam usernames or passwords. SteamCMD handles password
and Steam Guard prompts when anonymous login is disabled.
"""

import argparse
from contextlib import contextmanager
import copy
import json
import shutil
import sqlite3
import subprocess
import sys
import urllib.request
import uuid
import zipfile
from datetime import datetime, timezone
from pathlib import Path


MIN_PYTHON = (3, 9)
SCRIPT_ROOT = Path(__file__).resolve().parent
USER_AGENT = "WorkshopAnalysis"

SUPPORTED_GAME_TYPES = [
    {
        "Id": "source2",
        "Name": "Source 2",
        "Description": "Source 2 / VPK analysis with Source 2 Viewer CLI (ValveResourceFormat).",
    },
    {
        "Id": "unreal5",
        "Name": "Unreal Engine 5",
        "Description": "UE5 pak/utoc/ucas analysis with retoc/FModel-oriented tooling.",
    },
]


def utc_now_iso():
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


def write_section(text):
    print()
    print("== {0} ==".format(text))


def ensure_directory(path):
    path = Path(path)
    path.mkdir(parents=True, exist_ok=True)
    return path


def read_json_file(path, default):
    path = Path(path)
    if not path.exists():
        return copy.deepcopy(default)

    raw = path.read_text(encoding="utf-8")
    if not raw.strip():
        return copy.deepcopy(default)

    return json.loads(raw)


def save_json_file(path, value):
    path = Path(path)
    ensure_directory(path.parent)
    path.write_text(json.dumps(value, indent=2) + "\n", encoding="utf-8")


def merge_defaults(default, value):
    if not isinstance(default, dict) or not isinstance(value, dict):
        return value

    merged = copy.deepcopy(default)
    for key, item in value.items():
        if key in merged and isinstance(merged[key], dict) and isinstance(item, dict):
            merged[key] = merge_defaults(merged[key], item)
        else:
            merged[key] = item
    return merged


def as_path(value):
    if value is None:
        return None
    text = str(value).strip()
    if not text:
        return None
    return Path(text).expanduser()


def download_file(uri, out_file):
    out_file = Path(out_file)
    request = urllib.request.Request(uri, headers={"User-Agent": USER_AGENT})
    with urllib.request.urlopen(request) as response:
        with out_file.open("wb") as target:
            shutil.copyfileobj(response, target)


def get_github_latest_release_asset(repository, asset_name_regex):
    import re

    uri = "https://api.github.com/repos/{0}/releases/latest".format(repository)
    request = urllib.request.Request(uri, headers={"User-Agent": USER_AGENT})
    with urllib.request.urlopen(request) as response:
        release = json.loads(response.read().decode("utf-8"))

    pattern = re.compile(asset_name_regex)
    for asset in release.get("assets", []):
        if pattern.search(asset.get("name", "")):
            return asset

    raise RuntimeError(
        "Could not find a release asset matching '{0}' in {1} latest release.".format(
            asset_name_regex, repository
        )
    )


def install_zip_tool_from_github(repository, asset_name_regex, install_dir, expected_exe_name):
    install_dir = ensure_directory(install_dir)
    asset = get_github_latest_release_asset(repository, asset_name_regex)
    zip_path = install_dir / asset["name"]

    print("Downloading {0} from {1}...".format(asset["name"], repository))
    download_file(asset["browser_download_url"], zip_path)
    try:
        with zipfile.ZipFile(zip_path) as archive:
            archive.extractall(install_dir)
    finally:
        if zip_path.exists():
            zip_path.unlink()

    expected_name = expected_exe_name.lower()
    for path in install_dir.rglob("*"):
        if path.is_file() and path.name.lower() == expected_name:
            return str(path)

    raise RuntimeError(
        "Installed {0}, but could not find {1} under {2}.".format(
            repository, expected_exe_name, install_dir
        )
    )


def prompt_choice(title, choices, label_property="Name", default_id=None, id_property="Id"):
    write_section(title)
    for index, choice in enumerate(choices, start=1):
        label = str(choice.get(label_property, ""))
        choice_id = str(choice.get(id_property, ""))
        suffix = " [default]" if default_id and choice_id == default_id else ""
        print("[{0}] {1}{2}".format(index, label, suffix))
        description = choice.get("Description")
        if description:
            print("    {0}".format(description))

    while True:
        answer = input("Select a number: ").strip()
        if not answer and default_id:
            for choice in choices:
                if str(choice.get(id_property, "")) == default_id:
                    return choice
        try:
            index = int(answer)
        except ValueError:
            index = 0
        if 1 <= index <= len(choices):
            return choices[index - 1]
        print("WARNING: Invalid selection.")


def prompt_yes_no(question, default=True):
    suffix = "[Y/n]" if default else "[y/N]"
    while True:
        answer = input("{0} {1}: ".format(question, suffix)).strip().lower()
        if not answer:
            return default
        if answer in ("y", "yes"):
            return True
        if answer in ("n", "no"):
            return False
        print("WARNING: Please answer yes or no.")


def prompt_non_empty(prompt, default=None):
    while True:
        suffix = " [{0}]".format(default) if default else ""
        value = input("{0}{1}: ".format(prompt, suffix))
        if not value.strip() and default:
            return str(default)
        if value.strip():
            return value.strip()
        print("WARNING: A value is required.")


def path_is_relative_to(path, parent):
    try:
        path.relative_to(parent)
        return True
    except ValueError:
        return False


class WorkshopDatabase:
    def __init__(self, db_path):
        self.db_path = Path(db_path)

    @contextmanager
    def connect(self):
        ensure_directory(self.db_path.parent)
        connection = sqlite3.connect(self.db_path)
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA foreign_keys = ON")
        try:
            yield connection
        finally:
            connection.close()

    def initialize(self):
        with self.connect() as connection:
            with connection:
                connection.executescript(
                    """
                    CREATE TABLE IF NOT EXISTS metadata (
                        key TEXT PRIMARY KEY,
                        value TEXT NOT NULL
                    );

                    CREATE TABLE IF NOT EXISTS games (
                        id TEXT PRIMARY KEY,
                        title TEXT NOT NULL,
                        app_id TEXT NOT NULL,
                        game_type_id TEXT,
                        created_utc TEXT NOT NULL
                    );

                    CREATE TABLE IF NOT EXISTS workshop_content (
                        id TEXT PRIMARY KEY,
                        game_id TEXT NOT NULL,
                        title TEXT NOT NULL,
                        content_id TEXT NOT NULL,
                        created_utc TEXT NOT NULL,
                        last_download_utc TEXT,
                        last_download_path TEXT,
                        FOREIGN KEY (game_id) REFERENCES games(id) ON DELETE CASCADE
                    );

                    CREATE INDEX IF NOT EXISTS idx_games_title ON games(title);
                    CREATE INDEX IF NOT EXISTS idx_workshop_content_game_id
                        ON workshop_content(game_id);
                    CREATE INDEX IF NOT EXISTS idx_workshop_content_content_id
                        ON workshop_content(content_id);
                    """
                )
                connection.execute(
                    """
                    INSERT INTO metadata (key, value)
                    VALUES ('schema_version', '1')
                    ON CONFLICT(key) DO UPDATE SET value = excluded.value
                    """
                )

    def migrate_legacy_json(self, legacy_json_path):
        legacy_json_path = Path(legacy_json_path)
        if not legacy_json_path.exists():
            return

        with self.connect() as connection:
            existing_count = connection.execute("SELECT COUNT(*) FROM games").fetchone()[0]
            if existing_count:
                return

        legacy = read_json_file(legacy_json_path, {"Games": []})
        games = legacy.get("Games", [])
        if not isinstance(games, list) or not games:
            return

        migrated_games = 0
        migrated_items = 0
        with self.connect() as connection:
            with connection:
                for game in games:
                    if not isinstance(game, dict):
                        continue
                    game_id = str(game.get("Id") or uuid.uuid4())
                    connection.execute(
                        """
                        INSERT OR IGNORE INTO games
                            (id, title, app_id, game_type_id, created_utc)
                        VALUES (?, ?, ?, ?, ?)
                        """,
                        (
                            game_id,
                            str(game.get("Title") or "Untitled game"),
                            str(game.get("AppId") or ""),
                            game.get("GameTypeId"),
                            game.get("CreatedUtc") or utc_now_iso(),
                        ),
                    )
                    migrated_games += 1

                    workshop_items = game.get("WorkshopContent", [])
                    if not isinstance(workshop_items, list):
                        continue
                    for item in workshop_items:
                        if not isinstance(item, dict):
                            continue
                        connection.execute(
                            """
                            INSERT OR IGNORE INTO workshop_content
                                (
                                    id,
                                    game_id,
                                    title,
                                    content_id,
                                    created_utc,
                                    last_download_utc,
                                    last_download_path
                                )
                            VALUES (?, ?, ?, ?, ?, ?, ?)
                            """,
                            (
                                str(item.get("Id") or uuid.uuid4()),
                                game_id,
                                str(item.get("Title") or "Untitled workshop content"),
                                str(item.get("ContentId") or ""),
                                item.get("CreatedUtc") or utc_now_iso(),
                                item.get("LastDownloadUtc"),
                                item.get("LastDownloadPath"),
                            ),
                        )
                        migrated_items += 1

        print(
            "Migrated {0} game(s) and {1} workshop item(s) from {2} to {3}.".format(
                migrated_games,
                migrated_items,
                legacy_json_path,
                self.db_path,
            )
        )

    def list_games(self):
        with self.connect() as connection:
            rows = connection.execute(
                """
                SELECT
                    games.id AS Id,
                    games.title AS Title,
                    games.app_id AS AppId,
                    games.game_type_id AS GameTypeId,
                    games.created_utc AS CreatedUtc,
                    COUNT(workshop_content.id) AS WorkshopContentCount
                FROM games
                LEFT JOIN workshop_content ON workshop_content.game_id = games.id
                GROUP BY games.id
                ORDER BY title COLLATE NOCASE, app_id
                """
            ).fetchall()
        return [dict(row) for row in rows]

    def get_game(self, game_id):
        with self.connect() as connection:
            row = connection.execute(
                """
                SELECT
                    id AS Id,
                    title AS Title,
                    app_id AS AppId,
                    game_type_id AS GameTypeId,
                    created_utc AS CreatedUtc
                FROM games
                WHERE id = ?
                """,
                (game_id,),
            ).fetchone()
        return dict(row) if row else None

    def create_game(self, title, app_id, game_type_id):
        game = {
            "Id": str(uuid.uuid4()),
            "Title": title,
            "AppId": app_id,
            "GameTypeId": game_type_id,
            "CreatedUtc": utc_now_iso(),
        }
        with self.connect() as connection:
            with connection:
                connection.execute(
                    """
                    INSERT INTO games (id, title, app_id, game_type_id, created_utc)
                    VALUES (?, ?, ?, ?, ?)
                    """,
                    (
                        game["Id"],
                        game["Title"],
                        game["AppId"],
                        game["GameTypeId"],
                        game["CreatedUtc"],
                    ),
                )
        return game

    def update_game(self, game_id, title, app_id, game_type_id):
        with self.connect() as connection:
            with connection:
                connection.execute(
                    """
                    UPDATE games
                    SET title = ?, app_id = ?, game_type_id = ?
                    WHERE id = ?
                    """,
                    (title, app_id, game_type_id, game_id),
                )
        return self.get_game(game_id)

    def update_game_type(self, game_id, game_type_id):
        with self.connect() as connection:
            with connection:
                connection.execute(
                    "UPDATE games SET game_type_id = ? WHERE id = ?",
                    (game_type_id, game_id),
                )

    def delete_game(self, game_id):
        with self.connect() as connection:
            with connection:
                connection.execute("DELETE FROM games WHERE id = ?", (game_id,))

    def list_workshop_content(self, game_id):
        with self.connect() as connection:
            rows = connection.execute(
                """
                SELECT
                    id AS Id,
                    game_id AS GameId,
                    title AS Title,
                    content_id AS ContentId,
                    created_utc AS CreatedUtc,
                    last_download_utc AS LastDownloadUtc,
                    last_download_path AS LastDownloadPath
                FROM workshop_content
                WHERE game_id = ?
                ORDER BY title COLLATE NOCASE, content_id
                """,
                (game_id,),
            ).fetchall()
        return [dict(row) for row in rows]

    def get_workshop_content(self, workshop_item_id):
        with self.connect() as connection:
            row = connection.execute(
                """
                SELECT
                    id AS Id,
                    game_id AS GameId,
                    title AS Title,
                    content_id AS ContentId,
                    created_utc AS CreatedUtc,
                    last_download_utc AS LastDownloadUtc,
                    last_download_path AS LastDownloadPath
                FROM workshop_content
                WHERE id = ?
                """,
                (workshop_item_id,),
            ).fetchone()
        return dict(row) if row else None

    def create_workshop_content(self, game_id, title, content_id):
        item = {
            "Id": str(uuid.uuid4()),
            "GameId": game_id,
            "Title": title,
            "ContentId": content_id,
            "CreatedUtc": utc_now_iso(),
            "LastDownloadUtc": None,
            "LastDownloadPath": None,
        }
        with self.connect() as connection:
            with connection:
                connection.execute(
                    """
                    INSERT INTO workshop_content
                        (id, game_id, title, content_id, created_utc)
                    VALUES (?, ?, ?, ?, ?)
                    """,
                    (
                        item["Id"],
                        item["GameId"],
                        item["Title"],
                        item["ContentId"],
                        item["CreatedUtc"],
                    ),
                )
        return item

    def update_workshop_content(self, workshop_item_id, title, content_id):
        current = self.get_workshop_content(workshop_item_id)
        if not current:
            return None

        content_changed = str(current["ContentId"]) != str(content_id)
        with self.connect() as connection:
            with connection:
                if content_changed:
                    connection.execute(
                        """
                        UPDATE workshop_content
                        SET title = ?,
                            content_id = ?,
                            last_download_utc = NULL,
                            last_download_path = NULL
                        WHERE id = ?
                        """,
                        (title, content_id, workshop_item_id),
                    )
                else:
                    connection.execute(
                        """
                        UPDATE workshop_content
                        SET title = ?, content_id = ?
                        WHERE id = ?
                        """,
                        (title, content_id, workshop_item_id),
                    )
        return self.get_workshop_content(workshop_item_id)

    def delete_workshop_content(self, workshop_item_id):
        with self.connect() as connection:
            with connection:
                connection.execute(
                    "DELETE FROM workshop_content WHERE id = ?",
                    (workshop_item_id,),
                )

    def update_workshop_download(self, workshop_item_id, downloaded_at, download_path):
        with self.connect() as connection:
            with connection:
                connection.execute(
                    """
                    UPDATE workshop_content
                    SET last_download_utc = ?, last_download_path = ?
                    WHERE id = ?
                    """,
                    (downloaded_at, str(download_path), workshop_item_id),
                )


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
        print("Run .\\WorkshopAnalysis without -Bootstrap to select a game and workshop item.")

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

    def run(self, bootstrap=False, reconfigure=False, manage_catalog=False):
        paths = self.state_paths()
        has_config = paths["ConfigPath"].exists()
        config = self.load_config(paths["ConfigPath"])
        database = WorkshopDatabase(paths["DbPath"])
        database.initialize()
        database.migrate_legacy_json(paths["LegacyJsonDbPath"])

        if manage_catalog:
            self.manage_catalog(config, database)
            return

        if bootstrap or reconfigure or not has_config:
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


def build_parser():
    parser = argparse.ArgumentParser(
        prog="WorkshopAnalysis",
        description="Download Steam Workshop content and prepare analysis tooling.",
    )
    parser.add_argument(
        "-StateRoot",
        "--state-root",
        dest="state_root",
        default=str(SCRIPT_ROOT / "state"),
        help="Directory for config, database, downloaded workshop content, and tools.",
    )
    parser.add_argument(
        "-Bootstrap",
        "--bootstrap",
        action="store_true",
        help="Run first-time bootstrap prompts and write reusable configuration.",
    )
    parser.add_argument(
        "-Reconfigure",
        "--reconfigure",
        action="store_true",
        help="Re-run bootstrap prompts and update reusable configuration.",
    )
    parser.add_argument(
        "-NoToolBootstrap",
        "--no-tool-bootstrap",
        action="store_true",
        help="Skip Source 2 / UE5 tool installation checks for this run.",
    )
    parser.add_argument(
        "-ManageCatalog",
        "--manage-catalog",
        action="store_true",
        help="Open catalog management for games and associated workshop content.",
    )
    return parser


def main(argv=None):
    if sys.version_info < MIN_PYTHON:
        print(
            "ERROR: Python {0}.{1}+ is required. Run .\\setup.ps1 to install it.".format(
                MIN_PYTHON[0], MIN_PYTHON[1]
            ),
            file=sys.stderr,
        )
        return 1

    parser = build_parser()
    args = parser.parse_args(argv)
    app = WorkshopAnalysis(args.state_root, no_tool_bootstrap=args.no_tool_bootstrap)
    try:
        app.run(
            bootstrap=args.bootstrap,
            reconfigure=args.reconfigure,
            manage_catalog=args.manage_catalog,
        )
    except KeyboardInterrupt:
        print()
        print("Interrupted.", file=sys.stderr)
        return 130
    except Exception as exc:
        print("ERROR: {0}".format(exc), file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
