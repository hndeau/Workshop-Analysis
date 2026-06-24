import io
import json
import shutil
import subprocess
import sys
import tempfile
import unittest
import zipfile
from contextlib import redirect_stderr, redirect_stdout
from pathlib import Path
from types import SimpleNamespace
from unittest import mock


PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

import workshop_analysis as wa


CS2_APP_ID = "730"
CS2_GAME_TITLE = "CS2"
CS2_WORKSHOP_ITEMS = [
    {
        "Size": "light",
        "ContentId": "3735111145",
        "Title": "Dual Berettas :: RGB-A (Cache Collection)",
        "Url": "https://steamcommunity.com/sharedfiles/filedetails/?id=3735111145",
    },
    {
        "Size": "medium",
        "ContentId": "3437809122",
        "Title": "Cache",
        "Url": "https://steamcommunity.com/sharedfiles/filedetails/?id=3437809122",
    },
    {
        "Size": "heavy",
        "ContentId": "3691046714",
        "Title": "Splinter",
        "Url": "https://steamcommunity.com/sharedfiles/filedetails/?id=3691046714",
    },
]


class WorkshopAnalysisTestCase(unittest.TestCase):
    def setUp(self):
        self.temp_dir = Path(tempfile.mkdtemp(prefix="workshop-analysis-test-"))
        self.addCleanup(lambda: shutil.rmtree(self.temp_dir, ignore_errors=True))

    def app(self, no_tool_bootstrap=False):
        return wa.WorkshopAnalysis(self.temp_dir / "state", no_tool_bootstrap=no_tool_bootstrap)

    def database(self):
        db = wa.WorkshopDatabase(self.temp_dir / "state" / "workshop_analysis.db")
        db.initialize()
        return db

    def config(self, app=None):
        app = app or self.app()
        config = app.new_default_config()
        config["SteamCmd"]["InstallDir"] = str(self.temp_dir / "steamcmd")
        config["SteamCmd"]["ExePath"] = str(self.temp_dir / "steamcmd" / "steamcmd.exe")
        config["Defaults"]["WorkshopDownloadRoot"] = str(self.temp_dir / "workshop")
        config["Defaults"]["ToolRoot"] = str(self.temp_dir / "tools")
        return config

    def run_quietly(self, callable_, inputs=()):
        with mock.patch("builtins.input", side_effect=list(inputs)):
            with redirect_stdout(io.StringIO()), redirect_stderr(io.StringIO()):
                return callable_()

    def create_cs2_catalog(self, db):
        game = db.create_game(CS2_GAME_TITLE, CS2_APP_ID, "source2")
        items = [
            db.create_workshop_content(game["Id"], item["Title"], item["ContentId"])
            for item in CS2_WORKSHOP_ITEMS
        ]
        return game, items


class JsonAndDatabaseTests(WorkshopAnalysisTestCase):
    def test_read_json_uses_default_for_missing_or_blank_file(self):
        default = {"value": []}
        missing = self.temp_dir / "missing.json"
        self.assertEqual(wa.read_json_file(missing, default), default)

        blank = self.temp_dir / "blank.json"
        blank.write_text("  \n", encoding="utf-8")
        self.assertEqual(wa.read_json_file(blank, default), default)

        loaded = wa.read_json_file(blank, default)
        loaded["value"].append("changed")
        self.assertEqual(default, {"value": []})

    def test_merge_defaults_preserves_existing_values_and_fills_missing_keys(self):
        merged = wa.merge_defaults(
            {"outer": {"a": 1, "b": 2}, "keep": True},
            {"outer": {"b": 3}},
        )
        self.assertEqual(merged, {"outer": {"a": 1, "b": 3}, "keep": True})

    def test_legacy_json_migration_populates_sqlite_once(self):
        legacy_path = self.temp_dir / "state" / "games.json"
        legacy_path.parent.mkdir(parents=True)
        legacy_path.write_text(
            json.dumps(
                {
                    "Games": [
                        {
                            "Id": "game-1",
                            "Title": "Legacy Game",
                            "AppId": "111",
                            "GameTypeId": "source2",
                            "CreatedUtc": "2026-06-24T00:00:00Z",
                            "WorkshopContent": [
                                {
                                    "Id": "item-1",
                                    "Title": "Legacy Item",
                                    "ContentId": "222",
                                    "CreatedUtc": "2026-06-24T00:00:01Z",
                                    "LastDownloadUtc": "2026-06-24T00:00:02Z",
                                    "LastDownloadPath": "C:/content",
                                }
                            ],
                        }
                    ]
                }
            ),
            encoding="utf-8",
        )

        db = self.database()
        self.run_quietly(lambda: db.migrate_legacy_json(legacy_path))
        self.run_quietly(lambda: db.migrate_legacy_json(legacy_path))

        games = db.list_games()
        self.assertEqual(len(games), 1)
        self.assertEqual(games[0]["Id"], "game-1")
        self.assertEqual(games[0]["WorkshopContentCount"], 1)
        items = db.list_workshop_content("game-1")
        self.assertEqual(len(items), 1)
        self.assertEqual(items[0]["LastDownloadPath"], "C:/content")

    def test_database_crud_and_download_metadata_reset_on_content_id_change(self):
        db = self.database()
        game = db.create_game("Game", "100", "source2")
        item = db.create_workshop_content(game["Id"], "Item", "200")
        db.update_workshop_download(item["Id"], "2026-06-24T00:00:00Z", "C:/download")

        renamed = db.update_game(game["Id"], "Renamed Game", "101", "unreal5")
        self.assertEqual(renamed["Title"], "Renamed Game")
        self.assertEqual(renamed["GameTypeId"], "unreal5")

        same_content_id = db.update_workshop_content(item["Id"], "Renamed Item", "200")
        self.assertEqual(same_content_id["LastDownloadPath"], "C:/download")

        changed_content_id = db.update_workshop_content(item["Id"], "New Item", "201")
        self.assertEqual(changed_content_id["ContentId"], "201")
        self.assertIsNone(changed_content_id["LastDownloadPath"])
        self.assertIsNone(changed_content_id["LastDownloadUtc"])

    def test_deleting_game_cascades_workshop_rows(self):
        db = self.database()
        game = db.create_game("Game", "100", "source2")
        db.create_workshop_content(game["Id"], "Item", "200")

        db.delete_game(game["Id"])

        self.assertEqual(db.list_games(), [])
        self.assertEqual(db.list_workshop_content(game["Id"]), [])


class BootstrapAndRunTests(WorkshopAnalysisTestCase):
    def test_bootstrap_writes_config_and_initializes_database(self):
        app = self.app()

        def fake_install_steamcmd(config):
            steamcmd = self.temp_dir / "steamcmd" / "steamcmd.exe"
            steamcmd.parent.mkdir(parents=True)
            steamcmd.write_text("", encoding="utf-8")
            config["SteamCmd"]["Installed"] = True
            config["SteamCmd"]["InstallDir"] = str(steamcmd.parent)
            config["SteamCmd"]["ExePath"] = str(steamcmd)

        with mock.patch.object(app, "install_steamcmd", side_effect=fake_install_steamcmd):
            self.run_quietly(lambda: app.run(commands=["bootstrap"]), inputs=["2", "n"])

        paths = app.state_paths()
        config = json.loads(paths["ConfigPath"].read_text(encoding="utf-8"))
        self.assertEqual(config["Defaults"]["GameTypeId"], "unreal5")
        self.assertFalse(config["Defaults"]["UseAnonymousSteam"])
        self.assertTrue(config["SteamCmd"]["Installed"])
        self.assertTrue(paths["DbPath"].exists())

        db = wa.WorkshopDatabase(paths["DbPath"])
        with db.connect() as connection:
            schema_version = connection.execute(
                "SELECT value FROM metadata WHERE key = 'schema_version'"
            ).fetchone()[0]
        self.assertEqual(schema_version, "1")

    def test_run_download_flow_creates_catalog_entries_and_records_download(self):
        app = self.app(no_tool_bootstrap=True)
        paths = app.state_paths()
        config = self.config(app)
        config["Defaults"]["GameTypeId"] = "source2"
        Path(config["SteamCmd"]["ExePath"]).parent.mkdir(parents=True)
        Path(config["SteamCmd"]["ExePath"]).write_text("", encoding="utf-8")
        wa.save_json_file(paths["ConfigPath"], config)

        def fake_steamcmd(command, check=False, **kwargs):
            self.assertIn("+workshop_download_item", command)
            self.assertLess(command.index("+force_install_dir"), command.index("+login"))
            content_dir = (
                Path(config["Defaults"]["WorkshopDownloadRoot"])
                / "steamapps"
                / "workshop"
                / "content"
                / "123"
                / "456"
            )
            content_dir.mkdir(parents=True, exist_ok=True)
            return SimpleNamespace(returncode=0)

        with mock.patch(
            "workshop_analysis_app.app.get_steam_app_title",
            return_value="Test Game",
        ), mock.patch(
            "workshop_analysis_app.app.get_steam_workshop_item_title",
            return_value="Test Item",
        ), mock.patch("subprocess.run", side_effect=fake_steamcmd):
            self.run_quietly(
                lambda: app.run(commands=["download"]),
                inputs=["n", "123", "", "1", "n", "456", ""],
            )

        db = wa.WorkshopDatabase(paths["DbPath"])
        games = db.list_games()
        self.assertEqual(len(games), 1)
        self.assertEqual(games[0]["Title"], "Test Game")
        items = db.list_workshop_content(games[0]["Id"])
        self.assertEqual(len(items), 1)
        self.assertEqual(items[0]["Title"], "Test Item")
        self.assertTrue(Path(items[0]["LastDownloadPath"]).exists())

    def test_main_returns_error_for_application_exception(self):
        with mock.patch.object(wa.WorkshopAnalysis, "run", side_effect=RuntimeError("boom")):
            with redirect_stdout(io.StringIO()), redirect_stderr(io.StringIO()):
                exit_code = wa.main(["--state-root", str(self.temp_dir / "state")])

        self.assertEqual(exit_code, 1)


class CatalogManagementTests(WorkshopAnalysisTestCase):
    def test_manage_catalog_can_add_edit_game_and_workshop_content(self):
        app = self.app()
        db = self.database()
        config = self.config(app)

        with mock.patch("workshop_analysis_app.app.get_steam_app_title", return_value=None), mock.patch(
            "workshop_analysis_app.app.get_steam_workshop_item_title",
            return_value=None,
        ):
            self.run_quietly(
                lambda: app.manage_catalog(config, db),
                inputs=[
                    "a",
                    "111",
                    "Game A",
                    "1",
                    "1",
                    "a",
                    "222",
                    "Item A",
                    "1",
                    "e",
                    "Item B",
                    "333",
                    "b",
                    "e",
                    "444",
                    "Game B",
                    "2",
                    "b",
                    "q",
                ],
            )

        games = db.list_games()
        self.assertEqual(len(games), 1)
        self.assertEqual(games[0]["Title"], "Game B")
        self.assertEqual(games[0]["AppId"], "444")
        self.assertEqual(games[0]["GameTypeId"], "unreal5")
        items = db.list_workshop_content(games[0]["Id"])
        self.assertEqual(len(items), 1)
        self.assertEqual(items[0]["Title"], "Item B")
        self.assertEqual(items[0]["ContentId"], "333")

    def test_delete_workshop_content_purges_installed_directory_and_database_row(self):
        app = self.app()
        db = self.database()
        config = self.config(app)
        game = db.create_game("Game", "100", "source2")
        item = db.create_workshop_content(game["Id"], "Item", "200")
        content_dir = (
            Path(config["Defaults"]["WorkshopDownloadRoot"])
            / "steamapps"
            / "workshop"
            / "content"
            / "100"
            / "200"
        )
        content_dir.mkdir(parents=True)
        (content_dir / "payload.txt").write_text("payload", encoding="utf-8")
        db.update_workshop_download(item["Id"], "2026-06-24T00:00:00Z", content_dir)
        item = db.get_workshop_content(item["Id"])

        removed = self.run_quietly(
            lambda: app.delete_workshop_content(config, db, game, item),
            inputs=["y"],
        )

        self.assertTrue(removed)
        self.assertFalse(content_dir.exists())
        self.assertIsNone(db.get_workshop_content(item["Id"]))

    def test_delete_game_purges_all_workshop_content_and_database_rows(self):
        app = self.app()
        db = self.database()
        config = self.config(app)
        game = db.create_game("Game", "100", "source2")
        item_a = db.create_workshop_content(game["Id"], "Item A", "200")
        item_b = db.create_workshop_content(game["Id"], "Item B", "201")
        content_a = (
            Path(config["Defaults"]["WorkshopDownloadRoot"])
            / "steamapps"
            / "workshop"
            / "content"
            / "100"
            / "200"
        )
        content_b = (
            Path(config["SteamCmd"]["InstallDir"])
            / "steamapps"
            / "workshop"
            / "content"
            / "100"
            / "201"
        )
        content_a.mkdir(parents=True)
        content_b.mkdir(parents=True)
        (content_a / "payload.txt").write_text("payload", encoding="utf-8")
        (content_b / "payload.txt").write_text("payload", encoding="utf-8")
        db.update_workshop_download(item_a["Id"], "2026-06-24T00:00:00Z", content_a)
        db.update_workshop_download(item_b["Id"], "2026-06-24T00:00:01Z", content_b)

        removed = self.run_quietly(lambda: app.delete_game(config, db, game), inputs=["y"])

        self.assertTrue(removed)
        self.assertFalse(content_a.exists())
        self.assertFalse(content_b.exists())
        self.assertIsNone(db.get_game(game["Id"]))
        self.assertEqual(db.list_workshop_content(game["Id"]), [])

    def test_delete_prompts_can_cancel_without_mutating_state(self):
        app = self.app()
        db = self.database()
        config = self.config(app)
        game = db.create_game("Game", "100", "source2")
        item = db.create_workshop_content(game["Id"], "Item", "200")

        item_removed = self.run_quietly(
            lambda: app.delete_workshop_content(config, db, game, item),
            inputs=["n"],
        )
        game_removed = self.run_quietly(lambda: app.delete_game(config, db, game), inputs=["n"])

        self.assertFalse(item_removed)
        self.assertFalse(game_removed)
        self.assertIsNotNone(db.get_game(game["Id"]))
        self.assertIsNotNone(db.get_workshop_content(item["Id"]))

    def test_purge_rejects_paths_outside_configured_roots(self):
        app = self.app()
        config = self.config(app)
        game = {"AppId": "100"}
        outside = self.temp_dir / "outside" / "content"
        outside.mkdir(parents=True)
        (outside / "payload.txt").write_text("payload", encoding="utf-8")
        item = {
            "ContentId": "200",
            "LastDownloadPath": str(outside),
        }

        with self.assertRaises(RuntimeError):
            app.purge_workshop_content(config, game, item)

        self.assertTrue(outside.exists())


class PromptAndSelectionTests(WorkshopAnalysisTestCase):
    def test_select_or_create_game_can_open_management_then_select_existing_game(self):
        app = self.app()
        db = self.database()
        config = self.config(app)
        game = db.create_game("Managed Game", "100", None)

        selected = self.run_quietly(
            lambda: app.select_or_create_game(config, db, "source2"),
            inputs=["m", "q", "1"],
        )

        self.assertEqual(selected["Id"], game["Id"])
        self.assertEqual(db.get_game(game["Id"])["GameTypeId"], "source2")

    def test_prompt_helpers_retry_invalid_answers(self):
        yes_no = self.run_quietly(
            lambda: wa.prompt_yes_no("Question?", default=False),
            inputs=["maybe", "y"],
        )
        choice = self.run_quietly(
            lambda: wa.prompt_choice("Choose", [{"Id": "a", "Name": "A"}]),
            inputs=["x", "1"],
        )
        non_empty = self.run_quietly(lambda: wa.prompt_non_empty("Value"), inputs=["", "ok"])

        self.assertTrue(yes_no)
        self.assertEqual(choice["Id"], "a")
        self.assertEqual(non_empty, "ok")


class DownloadAndToolingTests(WorkshopAnalysisTestCase):
    class BytesResponse(io.BytesIO):
        def __enter__(self):
            return self

        def __exit__(self, exc_type, exc, tb):
            return False

    def test_download_file_writes_response_body(self):
        target = self.temp_dir / "download.bin"
        with mock.patch(
            "urllib.request.urlopen",
            return_value=self.BytesResponse(b"payload"),
        ):
            wa.download_file("https://example.test/file", target)

        self.assertEqual(target.read_bytes(), b"payload")

    def test_steam_workshop_title_lookup_uses_published_file_details(self):
        payload = {
            "response": {
                "publishedfiledetails": [
                    {
                        "publishedfileid": "3070193546",
                        "result": 1,
                        "title": "crashz' Crosshair Generator v4",
                    }
                ]
            }
        }

        def fake_urlopen(request, timeout=None):
            self.assertIn("GetPublishedFileDetails", request.full_url)
            self.assertEqual(timeout, 10)
            body = request.data.decode("utf-8")
            self.assertIn("itemcount=1", body)
            self.assertIn("publishedfileids%5B0%5D=3070193546", body)
            return self.BytesResponse(json.dumps(payload).encode("utf-8"))

        with mock.patch("urllib.request.urlopen", side_effect=fake_urlopen):
            title = wa.get_steam_workshop_item_title("3070193546")

        self.assertEqual(title, "crashz' Crosshair Generator v4")

    def test_steam_workshop_title_lookup_returns_none_without_title(self):
        payload = {"response": {"publishedfiledetails": [{"publishedfileid": "1"}]}}
        with mock.patch(
            "urllib.request.urlopen",
            return_value=self.BytesResponse(json.dumps(payload).encode("utf-8")),
        ):
            self.assertIsNone(wa.get_steam_workshop_item_title("1"))

    def test_steam_app_title_lookup_uses_appdetails(self):
        payload = {
            "730": {
                "success": True,
                "data": {
                    "name": "Counter-Strike 2",
                },
            }
        }

        def fake_urlopen(request, timeout=None):
            self.assertIn("store.steampowered.com/api/appdetails", request.full_url)
            self.assertIn("appids=730", request.full_url)
            self.assertEqual(timeout, 10)
            return self.BytesResponse(json.dumps(payload).encode("utf-8"))

        with mock.patch("urllib.request.urlopen", side_effect=fake_urlopen):
            title = wa.get_steam_app_title("730")

        self.assertEqual(title, "Counter-Strike 2")

    def test_steam_app_title_lookup_returns_none_without_successful_title(self):
        payload = {"730": {"success": False}}
        with mock.patch(
            "urllib.request.urlopen",
            return_value=self.BytesResponse(json.dumps(payload).encode("utf-8")),
        ):
            self.assertIsNone(wa.get_steam_app_title("730"))

    def test_github_release_asset_selection_and_missing_asset_error(self):
        release = {
            "assets": [
                {"name": "tool-linux.zip", "browser_download_url": "https://example.test/linux"},
                {"name": "tool-windows.zip", "browser_download_url": "https://example.test/windows"},
            ]
        }
        with mock.patch(
            "urllib.request.urlopen",
            return_value=self.BytesResponse(json.dumps(release).encode("utf-8")),
        ):
            asset = wa.get_github_latest_release_asset("owner/repo", r"windows")

        self.assertEqual(asset["browser_download_url"], "https://example.test/windows")

        with mock.patch(
            "urllib.request.urlopen",
            return_value=self.BytesResponse(json.dumps(release).encode("utf-8")),
        ):
            with self.assertRaises(RuntimeError):
                wa.get_github_latest_release_asset("owner/repo", r"macos")

    def test_install_zip_tool_from_github_extracts_expected_executable(self):
        install_dir = self.temp_dir / "tool"

        def fake_download(uri, out_file):
            with zipfile.ZipFile(out_file, "w") as archive:
                archive.writestr("nested/tool.exe", "exe")

        with mock.patch(
            "workshop_analysis_app.tooling.get_github_latest_release_asset",
            return_value={"name": "tool.zip", "browser_download_url": "https://example.test/tool.zip"},
        ):
            with mock.patch("workshop_analysis_app.tooling.download_file", side_effect=fake_download):
                with redirect_stdout(io.StringIO()):
                    exe_path = wa.install_zip_tool_from_github(
                        "owner/repo",
                        r"tool\.zip",
                        install_dir,
                        "tool.exe",
                    )

        self.assertTrue(Path(exe_path).exists())
        self.assertFalse((install_dir / "tool.zip").exists())

    def test_install_zip_tool_from_github_errors_when_executable_is_missing(self):
        def fake_download(uri, out_file):
            with zipfile.ZipFile(out_file, "w") as archive:
                archive.writestr("readme.txt", "missing")

        with mock.patch(
            "workshop_analysis_app.tooling.get_github_latest_release_asset",
            return_value={"name": "tool.zip", "browser_download_url": "https://example.test/tool.zip"},
        ):
            with mock.patch("workshop_analysis_app.tooling.download_file", side_effect=fake_download):
                with redirect_stdout(io.StringIO()):
                    with self.assertRaises(RuntimeError):
                        wa.install_zip_tool_from_github(
                            "owner/repo",
                            r"tool\.zip",
                            self.temp_dir / "tool",
                            "tool.exe",
                        )

    def test_install_steamcmd_downloads_and_records_executable(self):
        app = self.app()
        config = self.config(app)

        def fake_download(uri, out_file):
            with zipfile.ZipFile(out_file, "w") as archive:
                archive.writestr("steamcmd.exe", "exe")

        with mock.patch("workshop_analysis_app.app.download_file", side_effect=fake_download):
            self.run_quietly(lambda: app.install_steamcmd(config), inputs=[""])

        self.assertTrue(Path(config["SteamCmd"]["ExePath"]).exists())
        self.assertTrue(config["SteamCmd"]["Installed"])

    def test_install_steamcmd_uses_existing_executable(self):
        app = self.app()
        config = self.config(app)
        exe = Path(config["SteamCmd"]["ExePath"])
        exe.parent.mkdir(parents=True)
        exe.write_text("existing", encoding="utf-8")

        self.run_quietly(lambda: app.install_steamcmd(config), inputs=[""])

        self.assertEqual(Path(config["SteamCmd"]["ExePath"]), exe)
        self.assertEqual(exe.read_text(encoding="utf-8"), "existing")

    def test_source2_tool_setup_decline_success_and_manual_fallback(self):
        app = self.app()

        decline_config = self.config(app)
        self.run_quietly(lambda: app.ensure_source2_tools(decline_config), inputs=["n"])
        self.assertFalse(decline_config["Tools"]["Source2"]["Installed"])

        success_config = self.config(app)
        cli_path = self.temp_dir / "source2" / "Source2Viewer-CLI.exe"
        cli_path.parent.mkdir(parents=True)
        cli_path.write_text("exe", encoding="utf-8")
        with mock.patch("workshop_analysis_app.app.install_zip_tool_from_github", return_value=str(cli_path)):
            self.run_quietly(lambda: app.ensure_source2_tools(success_config), inputs=["y", str(cli_path.parent)])
        self.assertTrue(success_config["Tools"]["Source2"]["Installed"])
        self.assertEqual(success_config["Tools"]["Source2"]["CliPath"], str(cli_path))

        fallback_config = self.config(app)
        manual_path = self.temp_dir / "manual" / "Source2Viewer-CLI.exe"
        manual_path.parent.mkdir(parents=True)
        manual_path.write_text("exe", encoding="utf-8")
        with mock.patch("workshop_analysis_app.app.install_zip_tool_from_github", side_effect=RuntimeError("no asset")):
            self.run_quietly(
                lambda: app.ensure_source2_tools(fallback_config),
                inputs=["y", str(self.temp_dir / "auto-source2"), str(manual_path)],
            )
        self.assertTrue(fallback_config["Tools"]["Source2"]["Installed"])
        self.assertEqual(fallback_config["Tools"]["Source2"]["CliPath"], str(manual_path))

    def test_unreal_tool_setup_manual_retoc_and_unrealpak(self):
        app = self.app()
        config = self.config(app)
        install_dir = self.temp_dir / "unreal-tools"
        retoc = self.temp_dir / "manual" / "retoc.exe"
        retoc.parent.mkdir(parents=True)
        retoc.write_text("exe", encoding="utf-8")
        engine_dir = self.temp_dir / "UE"
        unrealpak = engine_dir / "Engine" / "Binaries" / "Win64" / "UnrealPak.exe"
        unrealpak.parent.mkdir(parents=True)
        unrealpak.write_text("exe", encoding="utf-8")

        with mock.patch("workshop_analysis_app.app.install_zip_tool_from_github", side_effect=RuntimeError("no asset")):
            self.run_quietly(
                lambda: app.ensure_unreal5_tools(config),
                inputs=[str(install_dir), "y", str(retoc), "n", str(engine_dir)],
            )

        self.assertTrue(config["Tools"]["Unreal5"]["Installed"])
        self.assertEqual(config["Tools"]["Unreal5"]["RetocPath"], str(retoc))
        self.assertEqual(config["Tools"]["Unreal5"]["UnrealPakPath"], str(unrealpak))

    def test_ensure_tools_for_game_type_respects_skip_and_rejects_unknown_type(self):
        skipped = self.app(no_tool_bootstrap=True)
        skipped.ensure_tools_for_game_type(self.config(skipped), "unknown")

        app = self.app()
        with self.assertRaises(RuntimeError):
            app.ensure_tools_for_game_type(self.config(app), "unknown")

    def test_invoke_workshop_download_auth_error_primary_path_and_missing_path(self):
        app = self.app()
        config = self.config(app)
        exe = Path(config["SteamCmd"]["ExePath"])
        exe.parent.mkdir(parents=True)
        exe.write_text("exe", encoding="utf-8")
        game = {"Title": "Game", "AppId": "100"}
        item = {"Title": "Item", "ContentId": "200"}

        config["Defaults"]["UseAnonymousSteam"] = False
        with mock.patch("subprocess.run", return_value=SimpleNamespace(returncode=5)):
            with self.assertRaises(RuntimeError):
                self.run_quietly(
                    lambda: app.invoke_workshop_download(config, game, item),
                    inputs=["steam-user"],
                )

        config["Defaults"]["UseAnonymousSteam"] = True
        primary_path = (
            Path(config["SteamCmd"]["InstallDir"])
            / "steamapps"
            / "workshop"
            / "content"
            / "100"
            / "200"
        )
        primary_path.mkdir(parents=True)
        with mock.patch("subprocess.run", return_value=SimpleNamespace(returncode=7)) as run:
            resolved = self.run_quietly(lambda: app.invoke_workshop_download(config, game, item))
        self.assertEqual(resolved, primary_path)
        command = run.call_args.args[0]
        self.assertLess(command.index("+force_install_dir"), command.index("+login"))

        shutil.rmtree(primary_path)
        with mock.patch("subprocess.run", return_value=SimpleNamespace(returncode=0)):
            with self.assertRaises(RuntimeError):
                self.run_quietly(lambda: app.invoke_workshop_download(config, game, item))

        exe.unlink()
        with self.assertRaises(RuntimeError):
            app.invoke_workshop_download(config, game, item)

    def test_update_catalog_downloads_all_and_selected_workshop_items(self):
        app = self.app(no_tool_bootstrap=True)
        paths = app.state_paths()
        config = self.config(app)
        Path(config["SteamCmd"]["ExePath"]).parent.mkdir(parents=True)
        Path(config["SteamCmd"]["ExePath"]).write_text("", encoding="utf-8")
        wa.save_json_file(paths["ConfigPath"], config)

        db = self.database()
        game_a = db.create_game("Game A", "100", "source2")
        game_b = db.create_game("Game B", "101", "source2")
        item_a = db.create_workshop_content(game_a["Id"], "Item A", "200")
        item_b = db.create_workshop_content(game_b["Id"], "Item B", "201")

        downloaded = []

        def fake_steamcmd(command, check=False, **kwargs):
            app_id = command[command.index("+workshop_download_item") + 1]
            content_id = command[command.index("+workshop_download_item") + 2]
            content_dir = (
                Path(config["Defaults"]["WorkshopDownloadRoot"])
                / "steamapps"
                / "workshop"
                / "content"
                / app_id
                / content_id
            )
            content_dir.mkdir(parents=True, exist_ok=True)
            downloaded.append((app_id, content_id))
            return SimpleNamespace(returncode=0)

        with mock.patch("subprocess.run", side_effect=fake_steamcmd):
            self.run_quietly(
                lambda: app.update_catalog_downloads(paths, True, config, db),
                inputs=["a", "a"],
            )

        self.assertEqual(downloaded, [("100", "200"), ("101", "201")])
        self.assertTrue(Path(db.get_workshop_content(item_a["Id"])["LastDownloadPath"]).exists())
        self.assertTrue(Path(db.get_workshop_content(item_b["Id"])["LastDownloadPath"]).exists())

        downloaded.clear()
        with mock.patch("subprocess.run", side_effect=fake_steamcmd):
            self.run_quietly(
                lambda: app.update_catalog_downloads(paths, True, config, db),
                inputs=["g", "2", "1"],
            )

        self.assertEqual(downloaded, [("101", "201")])

    def test_cs2_workshop_fixture_catalog_contains_known_anonymous_items(self):
        app = self.app(no_tool_bootstrap=True)
        db = self.database()
        game, items = self.create_cs2_catalog(db)

        self.assertEqual(game["AppId"], CS2_APP_ID)
        self.assertEqual(game["GameTypeId"], "source2")
        self.assertEqual(
            [item["ContentId"] for item in items],
            [item["ContentId"] for item in CS2_WORKSHOP_ITEMS],
        )
        self.assertEqual(
            [item["Title"] for item in items],
            [item["Title"] for item in CS2_WORKSHOP_ITEMS],
        )
        for item in CS2_WORKSHOP_ITEMS:
            self.assertTrue(item["Url"].endswith("id={0}".format(item["ContentId"])))

        candidates = app.collect_update_candidates(db)
        self.assertEqual(len(candidates), 3)
        self.assertEqual(
            {
                (
                    candidate["Game"]["AppId"],
                    candidate["WorkshopItem"]["ContentId"],
                    candidate["WorkshopItem"]["Title"],
                )
                for candidate in candidates
            },
            {
                (CS2_APP_ID, item["ContentId"], item["Title"])
                for item in CS2_WORKSHOP_ITEMS
            },
        )

    def test_cs2_workshop_fixture_updates_with_anonymous_steamcmd(self):
        app = self.app(no_tool_bootstrap=True)
        paths = app.state_paths()
        config = self.config(app)
        config["Defaults"]["UseAnonymousSteam"] = True
        Path(config["SteamCmd"]["ExePath"]).parent.mkdir(parents=True)
        Path(config["SteamCmd"]["ExePath"]).write_text("", encoding="utf-8")
        wa.save_json_file(paths["ConfigPath"], config)

        db = self.database()
        self.create_cs2_catalog(db)

        commands = []

        def fake_steamcmd(command, check=False, **kwargs):
            commands.append(command)
            self.assertIn("+login", command)
            self.assertEqual(command[command.index("+login") + 1], "anonymous")
            self.assertLess(command.index("+force_install_dir"), command.index("+login"))
            self.assertEqual(command[command.index("+workshop_download_item") + 1], CS2_APP_ID)
            content_id = command[command.index("+workshop_download_item") + 2]
            content_dir = (
                Path(config["Defaults"]["WorkshopDownloadRoot"])
                / "steamapps"
                / "workshop"
                / "content"
                / CS2_APP_ID
                / content_id
            )
            content_dir.mkdir(parents=True, exist_ok=True)
            return SimpleNamespace(
                returncode=0,
                stdout="Success. Downloaded item {0}".format(content_id),
            )

        with mock.patch("subprocess.run", side_effect=fake_steamcmd):
            self.run_quietly(
                lambda: app.update_catalog_downloads(paths, True, config, db),
                inputs=["a", "a"],
            )

        self.assertEqual(len(commands), len(CS2_WORKSHOP_ITEMS))
        self.assertEqual(
            {
                command[command.index("+workshop_download_item") + 2]
                for command in commands
            },
            {item["ContentId"] for item in CS2_WORKSHOP_ITEMS},
        )

    def test_analysis_todo_outputs_all_game_type_branches(self):
        app = self.app()
        config = self.config(app)
        for game_type_id in ("source2", "unreal5", "unknown"):
            self.run_quietly(
                lambda game_type_id=game_type_id: app.invoke_analysis_todo(
                    config,
                    game_type_id,
                    {"Title": "Game"},
                    {"Title": "Item"},
                    self.temp_dir / "content",
                )
            )

    def test_main_success_keyboard_interrupt_and_minimum_python_check(self):
        with mock.patch.object(wa.WorkshopAnalysis, "run", return_value=None) as run:
            with redirect_stdout(io.StringIO()), redirect_stderr(io.StringIO()):
                self.assertEqual(wa.main(["--state-root", str(self.temp_dir / "state"), "catalog"]), 0)
        self.assertEqual(run.call_args.kwargs["commands"], ["catalog"])

        with mock.patch.object(wa.WorkshopAnalysis, "run", side_effect=KeyboardInterrupt):
            with redirect_stdout(io.StringIO()), redirect_stderr(io.StringIO()):
                self.assertEqual(wa.main(["--state-root", str(self.temp_dir / "state")]), 130)

        with mock.patch.object(sys, "version_info", (3, 8, 0)):
            with redirect_stdout(io.StringIO()), redirect_stderr(io.StringIO()):
                self.assertEqual(wa.main([]), 1)

    def test_shell_help_and_exit(self):
        app = self.app()
        wa.save_json_file(app.state_paths()["ConfigPath"], self.config(app))
        self.run_quietly(app.run_shell, inputs=["help", "exit"])

    def test_shell_runs_initial_bootstrap_when_config_is_missing(self):
        app = self.app()

        def fake_install_steamcmd(config):
            steamcmd = self.temp_dir / "steamcmd" / "steamcmd.exe"
            steamcmd.parent.mkdir(parents=True)
            steamcmd.write_text("", encoding="utf-8")
            config["SteamCmd"]["Installed"] = True
            config["SteamCmd"]["InstallDir"] = str(steamcmd.parent)
            config["SteamCmd"]["ExePath"] = str(steamcmd)

        with mock.patch.object(app, "install_steamcmd", side_effect=fake_install_steamcmd):
            self.run_quietly(app.run_shell, inputs=["1", "y", "exit"])

        config = json.loads(app.state_paths()["ConfigPath"].read_text(encoding="utf-8"))
        self.assertEqual(config["Defaults"]["GameTypeId"], "source2")
        self.assertTrue(config["Defaults"]["UseAnonymousSteam"])

    def test_execute_unknown_command_and_status(self):
        app = self.app()
        self.run_quietly(lambda: app.execute_command(["status"]))
        self.run_quietly(lambda: app.execute_command(["bogus"]))
        self.assertFalse(app.execute_command(["exit"]))


if __name__ == "__main__":
    unittest.main()
