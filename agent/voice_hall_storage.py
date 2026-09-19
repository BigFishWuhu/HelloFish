import json
import sqlite3
from contextlib import closing
from pathlib import Path
from typing import Any, Iterable

from charm_levels import CHARM_LEVEL_MIN_VALUES
from wealth_levels import WEALTH_LEVEL_MIN_CONTRIBUTIONS


CONTRIBUTION_COLUMNS = (
    "room_id",
    "room_name",
    "rank",
    "contribution_gap",
    "estimated_contribution_value",
    "user_id",
    "username",
    "gender",
    "gender_source",
    "ip",
    "close_friend_count",
    "wealth_level",
    "charm_level",
    "level_sample_path",
    "scanned_at",
)


class VoiceHallDatabase:
    def __init__(self, path: Path | str) -> None:
        self.path = Path(path)

    def _connect(self) -> sqlite3.Connection:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        connection = sqlite3.connect(self.path, timeout=10)
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA busy_timeout = 10000")
        connection.execute("PRAGMA journal_mode = WAL")
        return connection

    def initialize(self) -> None:
        with closing(self._connect()) as connection, connection:
            connection.executescript(
                """
                CREATE TABLE IF NOT EXISTS wealth_level_thresholds (
                    level INTEGER PRIMARY KEY,
                    min_contribution INTEGER NOT NULL UNIQUE
                );

                CREATE TABLE IF NOT EXISTS charm_level_thresholds (
                    level INTEGER PRIMARY KEY,
                    min_charm_value INTEGER NOT NULL UNIQUE
                );

                CREATE TABLE IF NOT EXISTS contributions (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    room_id TEXT NOT NULL,
                    room_name TEXT,
                    rank INTEGER,
                    contribution_gap INTEGER,
                    estimated_contribution_value INTEGER,
                    user_id TEXT NOT NULL,
                    username TEXT,
                    gender TEXT,
                    gender_source TEXT,
                    ip TEXT,
                    close_friend_count INTEGER,
                    wealth_level INTEGER,
                    charm_level INTEGER,
                    level_sample_path TEXT,
                    scanned_at TEXT NOT NULL,
                    scan_date TEXT NOT NULL,
                    UNIQUE (room_id, user_id, scan_date)
                );

                CREATE INDEX IF NOT EXISTS idx_contributions_date_room_rank
                    ON contributions (scan_date DESC, room_id, rank);
                CREATE INDEX IF NOT EXISTS idx_contributions_user
                    ON contributions (user_id, scanned_at DESC);

                CREATE TABLE IF NOT EXISTS level_samples (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    room_id TEXT NOT NULL,
                    room_name TEXT,
                    rank INTEGER,
                    user_id TEXT NOT NULL,
                    username TEXT,
                    wealth_level INTEGER,
                    charm_level INTEGER,
                    missing_fields TEXT NOT NULL,
                    screenshot TEXT NOT NULL,
                    scanned_at TEXT NOT NULL,
                    scan_date TEXT NOT NULL,
                    UNIQUE (room_id, user_id, scan_date)
                );

                DROP VIEW IF EXISTS contribution_records_enriched;
                CREATE VIEW contribution_records_enriched AS
                SELECT
                    c.*,
                    current_level.min_contribution AS wealth_min_contribution,
                    next_level.level AS next_wealth_level,
                    next_level.min_contribution AS next_wealth_min_contribution,
                    current_charm.min_charm_value AS charm_min_value,
                    next_charm.level AS next_charm_level,
                    next_charm.min_charm_value AS next_charm_min_value
                FROM contributions AS c
                LEFT JOIN wealth_level_thresholds AS current_level
                    ON current_level.level = c.wealth_level
                LEFT JOIN wealth_level_thresholds AS next_level
                    ON next_level.level = c.wealth_level + 1
                LEFT JOIN charm_level_thresholds AS current_charm
                    ON current_charm.level = c.charm_level
                LEFT JOIN charm_level_thresholds AS next_charm
                    ON next_charm.level = c.charm_level + 1;
                """
            )
            existing_columns = {
                str(row[1])
                for row in connection.execute("PRAGMA table_info(contributions)")
            }
            for column in ("contribution_gap", "estimated_contribution_value"):
                if column not in existing_columns:
                    connection.execute(
                        f"ALTER TABLE contributions ADD COLUMN {column} INTEGER"
                    )
            connection.executemany(
                """
                INSERT INTO wealth_level_thresholds (level, min_contribution)
                VALUES (?, ?)
                ON CONFLICT(level) DO UPDATE SET
                    min_contribution = excluded.min_contribution
                """,
                enumerate(WEALTH_LEVEL_MIN_CONTRIBUTIONS),
            )
            connection.executemany(
                """
                INSERT INTO charm_level_thresholds (level, min_charm_value)
                VALUES (?, ?)
                ON CONFLICT(level) DO UPDATE SET
                    min_charm_value = excluded.min_charm_value
                """,
                enumerate(CHARM_LEVEL_MIN_VALUES),
            )

    @staticmethod
    def _scan_date(record: dict[str, Any]) -> str:
        value = str(record.get("scanned_at", ""))[:10]
        if not value:
            raise ValueError("记录缺少 scanned_at")
        return value

    @staticmethod
    def _upsert_contribution(
        connection: sqlite3.Connection,
        record: dict[str, Any],
    ) -> bool:
        scan_date = VoiceHallDatabase._scan_date(record)
        key = (str(record.get("room_id", "")), str(record.get("user_id", "")), scan_date)
        existed = connection.execute(
            """
            SELECT 1 FROM contributions
            WHERE room_id = ? AND user_id = ? AND scan_date = ?
            """,
            key,
        ).fetchone() is not None
        values = [record.get(column) for column in CONTRIBUTION_COLUMNS]
        connection.execute(
            f"""
            INSERT INTO contributions ({', '.join(CONTRIBUTION_COLUMNS)}, scan_date)
            VALUES ({', '.join('?' for _ in CONTRIBUTION_COLUMNS)}, ?)
            ON CONFLICT(room_id, user_id, scan_date) DO UPDATE SET
                room_name = excluded.room_name,
                rank = excluded.rank,
                contribution_gap = excluded.contribution_gap,
                estimated_contribution_value = excluded.estimated_contribution_value,
                username = excluded.username,
                gender = excluded.gender,
                gender_source = excluded.gender_source,
                ip = excluded.ip,
                close_friend_count = excluded.close_friend_count,
                wealth_level = excluded.wealth_level,
                charm_level = excluded.charm_level,
                level_sample_path = excluded.level_sample_path,
                scanned_at = excluded.scanned_at
            """,
            (*values, scan_date),
        )
        return existed

    def upsert_contribution(self, record: dict[str, Any]) -> bool:
        self.initialize()
        with closing(self._connect()) as connection, connection:
            return self._upsert_contribution(connection, record)

    def load_contributions(self) -> list[dict[str, Any]]:
        self.initialize()
        with closing(self._connect()) as connection:
            rows = connection.execute(
                f"""
                SELECT {', '.join(CONTRIBUTION_COLUMNS)}
                FROM contributions
                ORDER BY scanned_at, id
                """
            ).fetchall()
        return [dict(row) for row in rows]

    def upsert_level_sample(self, record: dict[str, Any]) -> None:
        self.initialize()
        scan_date = self._scan_date(record)
        with closing(self._connect()) as connection, connection:
            connection.execute(
                """
                INSERT INTO level_samples (
                    room_id, room_name, rank, user_id, username,
                    wealth_level, charm_level, missing_fields, screenshot,
                    scanned_at, scan_date
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(room_id, user_id, scan_date) DO UPDATE SET
                    room_name = excluded.room_name,
                    rank = excluded.rank,
                    username = excluded.username,
                    wealth_level = excluded.wealth_level,
                    charm_level = excluded.charm_level,
                    missing_fields = excluded.missing_fields,
                    screenshot = excluded.screenshot,
                    scanned_at = excluded.scanned_at
                """,
                (
                    str(record.get("room_id", "")),
                    record.get("room_name"),
                    record.get("rank"),
                    str(record.get("user_id", "")),
                    record.get("username"),
                    record.get("wealth_level"),
                    record.get("charm_level"),
                    json.dumps(record.get("missing_fields", []), ensure_ascii=False),
                    str(record.get("screenshot", "")),
                    str(record.get("scanned_at", "")),
                    scan_date,
                ),
            )

    def fetch_all(self, sql: str, parameters: Iterable[Any] = ()) -> list[dict[str, Any]]:
        self.initialize()
        with closing(self._connect()) as connection:
            return [dict(row) for row in connection.execute(sql, tuple(parameters)).fetchall()]
