"""SQLite persistence for games and workshop content."""

from contextlib import contextmanager
import sqlite3
import uuid
from pathlib import Path

from .common import ensure_directory, read_json_file, utc_now_iso


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
