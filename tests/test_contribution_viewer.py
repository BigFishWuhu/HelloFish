import csv
import io
import json
import sqlite3
import sys
import tempfile
import unittest
from contextlib import closing
from datetime import date
from pathlib import Path


sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "agent"))

from contribution_viewer import (  # noqa: E402
    RecordQuery,
    export_records_csv,
    load_settings,
    parse_export_columns,
    query_records,
    save_settings,
)
from voice_hall_storage import VoiceHallDatabase  # noqa: E402


class ContributionViewerTest(unittest.TestCase):
    def setUp(self) -> None:
        self.temp_directory = tempfile.TemporaryDirectory()
        self.root = Path(self.temp_directory.name)
        self.database_path = self.root / "voice_hall.sqlite3"
        self.settings_path = self.root / "viewer.json"
        database = VoiceHallDatabase(self.database_path)
        database.initialize()
        records = [
            {
                "room_id": "100",
                "room_name": "海风厅",
                "rank": 1,
                "user_id": "u1",
                "username": "甲",
                "gender": "男",
                "gender_source": "icon",
                "ip": "上海",
                "close_friend_count": 2,
                "wealth_level": 20,
                "charm_level": 3,
                "level_sample_path": None,
                "scanned_at": "2026-09-14T10:00:00+08:00",
            },
            {
                "room_id": "200",
                "room_name": "星光厅",
                "rank": 2,
                "user_id": "u2",
                "username": "乙",
                "gender": "女",
                "gender_source": "icon",
                "ip": "江苏",
                "close_friend_count": 0,
                "wealth_level": None,
                "charm_level": None,
                "level_sample_path": None,
                "scanned_at": "2026-09-13T10:00:00+08:00",
            },
            {
                "room_id": "300",
                "room_name": "旧厅",
                "rank": 3,
                "user_id": "u3",
                "username": "丙",
                "gender": None,
                "gender_source": "icon",
                "ip": "浙江",
                "close_friend_count": 1,
                "wealth_level": 100,
                "charm_level": 8,
                "level_sample_path": None,
                "scanned_at": "2026-09-01T10:00:00+08:00",
            },
        ]
        with closing(sqlite3.connect(self.database_path)) as connection, connection:
            for record in records:
                VoiceHallDatabase._upsert_contribution(connection, record)

    def tearDown(self) -> None:
        self.temp_directory.cleanup()

    def test_settings_are_normalized_and_persisted(self) -> None:
        saved = save_settings(
            self.settings_path,
            {
                "hidden_room_ids": [" 200 ", "100", "200", ""],
                "hidden_user_ids": ["u2", "u2"],
            },
        )
        self.assertEqual(saved["hidden_room_ids"], ["100", "200"])
        self.assertEqual(load_settings(self.settings_path), saved)
        self.assertEqual(json.loads(self.settings_path.read_text(encoding="utf-8")), saved)

    def test_today_is_the_default_date_filter(self) -> None:
        query = RecordQuery.from_query({}, today=date(2026, 9, 14))
        result = query_records(self.database_path, self.settings_path, query)
        self.assertEqual(result["total"], 1)
        self.assertEqual(result["records"][0]["user_id"], "u1")
        self.assertIsNotNone(result["records"][0]["wealth_min_contribution"])

    def test_recent_days_minimum_unknown_and_hidden_ids(self) -> None:
        save_settings(
            self.settings_path,
            {"hidden_room_ids": [], "hidden_user_ids": ["u1"]},
        )
        query = RecordQuery.from_query(
            {
                "date_mode": ["recent"],
                "days": ["2"],
                "min_wealth_level": ["10"],
                "include_unknown": ["true"],
            },
            today=date(2026, 9, 14),
        )
        result = query_records(self.database_path, self.settings_path, query)
        self.assertEqual(result["total"], 1)
        self.assertEqual(result["records"][0]["user_id"], "u2")

    def test_unknown_wealth_can_be_excluded(self) -> None:
        query = RecordQuery.from_query(
            {
                "date_mode": ["recent"],
                "days": ["2"],
                "include_unknown": ["false"],
            },
            today=date(2026, 9, 14),
        )
        result = query_records(self.database_path, self.settings_path, query)
        self.assertEqual([record["user_id"] for record in result["records"]], ["u1"])

    def test_gender_friend_count_and_identity_filters_are_combined(self) -> None:
        query = RecordQuery.from_query(
            {
                "gender": ["male"],
                "min_close_friend_count": ["2"],
                "room_id": ["100"],
                "user_id": ["u1"],
                "username": ["甲"],
            },
            today=date(2026, 9, 14),
        )
        result = query_records(self.database_path, self.settings_path, query)
        self.assertEqual(result["total"], 1)
        self.assertEqual(result["records"][0]["user_id"], "u1")

        no_partial_id_match = RecordQuery.from_query(
            {"user_id": ["u"]}, today=date(2026, 9, 14)
        )
        self.assertEqual(
            query_records(self.database_path, self.settings_path, no_partial_id_match)["total"],
            0,
        )

    def test_unknown_gender_filter(self) -> None:
        query = RecordQuery.from_query(
            {
                "date_mode": ["custom"],
                "start_date": ["2026-09-01"],
                "end_date": ["2026-09-01"],
                "gender": ["unknown"],
            },
            today=date(2026, 9, 14),
        )
        result = query_records(self.database_path, self.settings_path, query)
        self.assertEqual([record["user_id"] for record in result["records"]], ["u3"])

    def test_csv_export_uses_filters_and_selected_columns(self) -> None:
        query = RecordQuery.from_query(
            {
                "gender": ["male"],
                "min_close_friend_count": ["2"],
            },
            today=date(2026, 9, 14),
        )
        payload = export_records_csv(
            self.database_path,
            self.settings_path,
            query,
            ["username", "user_id", "wealth_min_yuan"],
        )
        rows = list(csv.reader(io.StringIO(payload.decode("utf-8-sig"))))
        self.assertEqual(rows[0], ["用户名", "用户 ID", "等级最低金额（元）"])
        self.assertEqual(len(rows), 2)
        self.assertEqual(rows[1][:2], ["甲", "u1"])
        self.assertNotEqual(rows[1][2], "")

    def test_export_columns_are_allowlisted_and_deduplicated(self) -> None:
        columns = parse_export_columns(
            {"columns": ["user_id,username,user_id,not-a-column"]}
        )
        self.assertEqual(columns, ["user_id", "username"])
        with self.assertRaisesRegex(ValueError, "至少选择"):
            parse_export_columns({"columns": ["not-a-column"]})


if __name__ == "__main__":
    unittest.main()
