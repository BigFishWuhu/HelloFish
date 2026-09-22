import csv
import io
import json
import os
import sqlite3
import sys
import tempfile
import threading
import unittest
from contextlib import closing
from datetime import date, datetime, timezone
from http import HTTPStatus
from pathlib import Path
from unittest.mock import patch
from urllib.request import urlopen


sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "agent"))

import contribution_viewer  # noqa: E402
from contribution_viewer import (  # noqa: E402
    RecordQuery,
    ContributionViewerServer,
    HEALTH_RESPONSE,
    export_records_csv,
    export_records_html,
    is_server_running,
    load_settings,
    parse_export_columns,
    query_records,
    _account_assessment,
    _format_yuan_amount,
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
                "contribution_gap": 250,
                "estimated_contribution_value": 1_500,
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
        self.assertEqual(result["records"][0]["contribution_gap"], 250)
        self.assertEqual(result["records"][0]["estimated_contribution_value"], 1_500)
        self.assertIsNotNone(result["records"][0]["wealth_min_contribution"])
        self.assertIsNotNone(result["records"][0]["charm_min_value"])
        self.assertEqual(result["records"][0]["account_assessment"], "高概率真实玩家")

    def test_account_assessment_uses_fifty_percent_wealth_boundary(self) -> None:
        self.assertEqual(_account_assessment(150, 100), "高概率真实玩家")
        self.assertEqual(_account_assessment(149, 100), "疑似排挡账号")
        self.assertEqual(_account_assessment(100, 150), "疑似排挡账号")
        self.assertEqual(_account_assessment(100, 0), "高概率真实玩家")
        self.assertIsNone(_account_assessment(0, 0))
        self.assertIsNone(_account_assessment(None, 100))

    def test_cloud_page_uses_china_timezone_for_default_date(self) -> None:
        app_path = (
            Path(__file__).resolve().parents[1]
            / "cloud"
            / "web"
            / "contributions"
            / "app.js"
        )
        app = app_path.read_text(encoding="utf-8")
        self.assertIn('timeZone: "Asia/Shanghai"', app)
        self.assertIn("start_time: controls.startDate.value", app)
        self.assertIn("end_time: controls.endDate.value", app)

    def test_default_today_uses_china_time_when_host_is_still_in_utc_yesterday(self) -> None:
        with patch.object(
            contribution_viewer,
            "datetime",
            wraps=datetime,
        ) as mocked_datetime:
            mocked_datetime.now.return_value = datetime(
                2026, 9, 13, 16, 30, tzinfo=timezone.utc
            )
            query = RecordQuery.from_query({})

        self.assertEqual(query.start_date, date(2026, 9, 14))
        self.assertEqual(query.end_date, date(2026, 9, 14))

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

    def test_custom_time_range_filters_to_inclusive_minutes(self) -> None:
        records = [
            ("u-before", "2026-09-14T09:59:59+08:00"),
            ("u-in-minute", "2026-09-14T10:00:59+08:00"),
            ("u-after", "2026-09-14T10:01:00+08:00"),
        ]
        with closing(sqlite3.connect(self.database_path)) as connection, connection:
            for index, (user_id, scanned_at) in enumerate(records, start=1):
                VoiceHallDatabase._upsert_contribution(
                    connection,
                    {
                        "room_id": f"minute-{index}",
                        "rank": index,
                        "user_id": user_id,
                        "username": user_id,
                        "scanned_at": scanned_at,
                    },
                )

        query = RecordQuery.from_query(
            {
                "date_mode": ["custom"],
                "start_time": ["2026-09-14T10:00"],
                "end_time": ["2026-09-14T10:00"],
            },
            today=date(2026, 9, 14),
        )
        result = query_records(self.database_path, self.settings_path, query)

        self.assertEqual(
            [record["user_id"] for record in result["records"]],
            ["u-in-minute", "u1"],
        )
        self.assertEqual(result["start_time"], "2026-09-14T10:00")
        self.assertEqual(result["end_time"], "2026-09-14T10:00")

        payload = export_records_csv(
            self.database_path,
            self.settings_path,
            query,
            ["user_id"],
        )
        rows = list(csv.reader(io.StringIO(payload.decode("utf-8-sig"))))
        self.assertEqual(rows, [["用户 ID"], ["u-in-minute"], ["u1"]])

        document = export_records_html(
            self.database_path,
            self.settings_path,
            query,
        ).decode("utf-8")
        self.assertIn("u-in-minute", document)
        self.assertNotIn("u-before", document)
        self.assertNotIn("u-after", document)

    def test_legacy_custom_dates_still_cover_the_full_end_day(self) -> None:
        query = RecordQuery.from_query(
            {
                "date_mode": ["custom"],
                "start_date": ["2026-09-13"],
                "end_date": ["2026-09-14"],
            },
            today=date(2026, 9, 14),
        )
        result = query_records(self.database_path, self.settings_path, query)

        self.assertEqual(result["total"], 2)
        self.assertEqual(result["start_time"], "2026-09-13T00:00")
        self.assertEqual(result["end_time"], "2026-09-14T23:59")

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

    def test_csv_export_includes_relative_and_estimated_contributions(self) -> None:
        query = RecordQuery.from_query({}, today=date(2026, 9, 14))
        payload = export_records_csv(
            self.database_path,
            self.settings_path,
            query,
            ["contribution_gap", "estimated_contribution_value"],
        )
        rows = list(csv.reader(io.StringIO(payload.decode("utf-8-sig"))))
        self.assertEqual(rows[0], ["距前一名", "推测贡献值"])
        self.assertEqual(rows[1], ["250", "1500"])

    def test_csv_export_includes_charm_amount_and_account_assessment(self) -> None:
        query = RecordQuery.from_query({}, today=date(2026, 9, 14))
        payload = export_records_csv(
            self.database_path,
            self.settings_path,
            query,
            ["charm_level", "charm_min_value", "charm_min_yuan", "account_assessment"],
        )
        rows = list(csv.reader(io.StringIO(payload.decode("utf-8-sig"))))
        self.assertEqual(
            rows[0],
            ["魅力等级", "等级最低魅力值", "魅力等级最低金额（元）", "账号判断"],
        )
        self.assertEqual(rows[1], ["3", "30", "3元", "高概率真实玩家"])

    def test_csv_export_formats_yuan_amounts_by_wan(self) -> None:
        self.assertEqual(_format_yuan_amount(9999), "9999元")
        self.assertEqual(_format_yuan_amount(10000), "1万元")
        self.assertEqual(_format_yuan_amount(12345), "1.23万元")
        record = {
            "room_id": "400",
            "room_name": "金额厅",
            "rank": 1,
            "user_id": "u4",
            "username": "丁",
            "gender": "男",
            "gender_source": "icon",
            "ip": "北京",
            "close_friend_count": 0,
            "wealth_level": 200,
            "charm_level": 1,
            "level_sample_path": None,
            "scanned_at": "2026-09-14T10:00:00+08:00",
        }
        with closing(sqlite3.connect(self.database_path)) as connection, connection:
            VoiceHallDatabase._upsert_contribution(connection, record)
        query = RecordQuery.from_query({}, today=date(2026, 9, 14))
        payload = export_records_csv(
            self.database_path,
            self.settings_path,
            query,
            ["wealth_min_yuan"],
        )
        rows = list(csv.reader(io.StringIO(payload.decode("utf-8-sig"))))
        self.assertEqual(rows[0], ["等级最低金额（元）"])
        self.assertTrue(rows[1][0].endswith(("元", "万元")))

    def test_export_columns_are_allowlisted_and_deduplicated(self) -> None:
        columns = parse_export_columns(
            {"columns": ["user_id,username,user_id,not-a-column"]}
        )
        self.assertEqual(columns, ["user_id", "username"])
        with self.assertRaisesRegex(ValueError, "至少选择"):
            parse_export_columns({"columns": ["not-a-column"]})

    def test_html_export_is_unpaginated_uses_yuan_and_escapes_data(self) -> None:
        with closing(sqlite3.connect(self.database_path)) as connection, connection:
            connection.execute(
                "UPDATE contributions SET username = ? WHERE user_id = ?",
                ("<script>alert(1)</script>", "u1"),
            )
        query = RecordQuery.from_query(
            {
                "date_mode": ["recent"],
                "days": ["2"],
                "page": ["2"],
                "page_size": ["10"],
            },
            today=date(2026, 9, 14),
        )

        document = export_records_html(
            self.database_path,
            self.settings_path,
            query,
        ).decode("utf-8")

        self.assertIn('<strong id="record-total">2</strong>', document)
        self.assertIn("距前一名金额", document)
        self.assertIn("推测金额", document)
        self.assertIn("25 元", document)
        self.assertIn("150 元", document)
        self.assertIn('<td class="wealth">20<small>（120 元）</small></td>', document)
        self.assertIn('<td class="charm">3<small>（3 元）</small></td>', document)
        self.assertIn("高概率真实玩家", document)
        self.assertNotIn("<th>等级最低金额</th>", document)
        self.assertIn("ID u2", document)
        self.assertIn('data-user-id="u2"', document)
        self.assertIn('<tbody id="records-body">', document)
        self.assertIn('id="static-filters"', document)
        self.assertIn('name="start_time" type="datetime-local"', document)
        self.assertIn('name="end_time" type="datetime-local"', document)
        self.assertIn('data-scan-time="2026-09-14T10:00"', document)
        self.assertIn('name="gender"', document)
        self.assertIn('data-gender="male"', document)
        self.assertIn('data-gender="female"', document)
        self.assertIn('data-room-id="100"', document)
        self.assertIn("function applyFilters()", document)
        self.assertIn("wealth === null && !filterForm.elements.include_unknown.checked", document)
        self.assertIn('filterForm.addEventListener("input", applyFilters)', document)
        self.assertIn('filterForm.addEventListener("change", applyFilters)', document)
        self.assertIn("navigator.clipboard.writeText(userId)", document)
        self.assertIn("hellofish-copied-user-ids-v1", document)
        self.assertIn('content: "已复制"', document)
        self.assertIn("position: absolute", document)
        self.assertIn("&lt;script&gt;alert(1)&lt;/script&gt;", document)
        self.assertNotIn("<script>alert(1)</script>", document)
        self.assertNotIn("上一页", document)

    def test_html_export_uses_selected_columns(self) -> None:
        query = RecordQuery.from_query({}, today=date(2026, 9, 14))

        document = export_records_html(
            self.database_path,
            self.settings_path,
            query,
            ["username", "wealth_min_yuan"],
        ).decode("utf-8")

        self.assertIn("<th>用户名</th><th>等级最低金额（元）</th>", document)
        self.assertNotIn("<th>日期 / 时间</th>", document)
        self.assertNotIn("<th>财富等级</th>", document)
        self.assertIn('data-user-id="u1"', document)
        self.assertIn("120 元", document)

    def test_html_export_endpoint_downloads_a_standalone_file(self) -> None:
        web_root = self.root / "html-export-web"
        web_root.mkdir()
        web_root.joinpath("index.html").write_text("viewer", encoding="utf-8")
        server = ContributionViewerServer(
            ("127.0.0.1", 0),
            self.database_path,
            self.settings_path,
            web_root,
        )
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        url = (
            f"http://127.0.0.1:{server.server_port}/api/export.html"
            "?date_mode=custom&start_date=2026-09-14&end_date=2026-09-14"
        )
        try:
            with urlopen(url, timeout=2) as response:  # noqa: S310
                self.assertEqual(response.status, HTTPStatus.OK)
                self.assertEqual(response.headers.get_content_type(), "text/html")
                self.assertIn(".html", response.headers["Content-Disposition"])
                self.assertIn(
                    "script-src 'unsafe-inline'",
                    response.headers["Content-Security-Policy"],
                )
                self.assertIn("贡献记录", response.read().decode("utf-8"))
        finally:
            server.shutdown()
            server.server_close()
            thread.join(timeout=2)

    def test_html_export_endpoint_accepts_selected_columns(self) -> None:
        web_root = self.root / "html-export-selected-web"
        web_root.mkdir()
        web_root.joinpath("index.html").write_text("viewer", encoding="utf-8")
        server = ContributionViewerServer(
            ("127.0.0.1", 0),
            self.database_path,
            self.settings_path,
            web_root,
        )
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        url = (
            f"http://127.0.0.1:{server.server_port}/api/export.html"
            "?date_mode=custom&start_date=2026-09-14&end_date=2026-09-14"
            "&columns=username,room_id"
        )
        try:
            with urlopen(url, timeout=2) as response:  # noqa: S310
                document = response.read().decode("utf-8")
                self.assertIn("<th>用户名</th><th>厅 ID</th>", document)
                self.assertNotIn("<th>日期 / 时间</th>", document)
        finally:
            server.shutdown()
            server.server_close()
            thread.join(timeout=2)

    def test_health_endpoint_identifies_the_viewer(self) -> None:
        web_root = self.root / "web"
        web_root.mkdir()
        web_root.joinpath("index.html").write_text("viewer", encoding="utf-8")
        server = ContributionViewerServer(
            ("127.0.0.1", 0),
            self.database_path,
            self.settings_path,
            web_root,
        )
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        health_url = f"http://127.0.0.1:{server.server_port}/api/health"
        try:
            self.assertTrue(is_server_running(health_url))
            with urlopen(health_url, timeout=1) as response:  # noqa: S310
                self.assertEqual(response.status, HTTPStatus.OK)
                self.assertEqual(json.load(response), HEALTH_RESPONSE)
        finally:
            server.shutdown()
            server.server_close()
            thread.join(timeout=2)

    def test_runtime_endpoint_is_published_and_cleared(self) -> None:
        web_root = self.root / "web"
        web_root.mkdir()
        web_root.joinpath("index.html").write_text("viewer", encoding="utf-8")
        runtime_path = self.root / "contribution_viewer_port.json"
        server = ContributionViewerServer(
            ("127.0.0.1", 0),
            self.database_path,
            self.settings_path,
            web_root,
        )
        actual_port = server.server_port
        contribution_viewer._write_runtime_endpoint(
            "127.0.0.1", actual_port, runtime_path
        )
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        try:
            self.assertEqual(
                contribution_viewer.get_server_url(runtime_path),
                f"http://127.0.0.1:{actual_port}/",
            )
            self.assertTrue(
                contribution_viewer.is_server_running(runtime_path=runtime_path)
            )
        finally:
            server.shutdown()
            server.server_close()
            thread.join(timeout=2)
            contribution_viewer._clear_runtime_endpoint(
                "127.0.0.1", actual_port, runtime_path
            )
        self.assertFalse(runtime_path.exists())

    @patch("contribution_viewer.subprocess.Popen")
    @patch("contribution_viewer.is_server_running", side_effect=[False, True])
    def test_background_server_is_launched_with_the_mxu_owner_pid(
        self,
        server_running,
        popen,
    ) -> None:
        log_path = self.root / "viewer.log"
        self.assertTrue(
            contribution_viewer.start_background_server(
                owner_pid=12345,
                startup_timeout=1,
                log_path=log_path,
            )
        )
        command = popen.call_args.args[0]
        self.assertEqual(command[-2:], ["--owner-pid", "12345"])
        self.assertIn("--serve", command)
        self.assertEqual(server_running.call_count, 2)
        self.assertTrue(log_path.is_file())

    def test_process_probe_recognizes_the_current_process(self) -> None:
        self.assertTrue(contribution_viewer._process_exists(os.getpid()))
        self.assertFalse(contribution_viewer._process_exists(-1))


if __name__ == "__main__":
    unittest.main()
