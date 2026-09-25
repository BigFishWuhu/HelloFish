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
    parse_summary_column_order,
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

    def test_local_and_cloud_pages_support_estimated_minimum_and_column_order(self) -> None:
        project_root = Path(__file__).resolve().parents[1]
        for web_root in (
            project_root / "web" / "contributions",
            project_root / "cloud" / "web" / "contributions",
        ):
            page = (web_root / "index.html").read_text(encoding="utf-8")
            script = (web_root / "app.js").read_text(encoding="utf-8")
            self.assertIn('id="min-estimated"', page)
            self.assertIn('id="column-order-dialog"', page)
            self.assertIn("min_estimated_contribution_total", script)
            self.assertIn("formatEstimatedValue", script)
            self.assertIn('params.set("column_order"', script)
            self.assertIn("reorderExportColumns", script)

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

    def test_same_user_in_multiple_rooms_is_grouped_with_appearance_details(self) -> None:
        with closing(sqlite3.connect(self.database_path)) as connection, connection:
            VoiceHallDatabase._upsert_contribution(
                connection,
                {
                    "room_id": "200",
                    "room_name": "星光厅",
                    "rank": 4,
                    "contribution_gap": 80,
                    "estimated_contribution_value": 900,
                    "user_id": "u1",
                    "username": "甲",
                    "gender": "男",
                    "ip": "上海",
                    "close_friend_count": 2,
                    "wealth_level": 20,
                    "charm_level": 3,
                    "scanned_at": "2026-09-14T11:00:00+08:00",
                },
            )

        query = RecordQuery.from_query(
            {"user_id": ["u1"]}, today=date(2026, 9, 14)
        )
        result = query_records(self.database_path, self.settings_path, query)

        self.assertEqual(result["total"], 1)
        self.assertEqual(result["records"][0]["room_count"], 2)
        self.assertEqual(result["records"][0]["appearance_count"], 2)
        self.assertEqual(
            result["records"][0]["estimated_contribution_total"],
            2_400,
        )
        self.assertEqual(
            [item["room_id"] for item in result["records"][0]["appearances"]],
            ["200", "100"],
        )

        payload = export_records_csv(
            self.database_path,
            self.settings_path,
            query,
            ["username", "room_name", "rank"],
        )
        rows = list(csv.reader(io.StringIO(payload.decode("utf-8-sig"))))
        self.assertEqual(len(rows), 2)
        self.assertEqual(rows[1][0], "甲（ID u1）")
        self.assertIn("星光厅（ID 200）\n海风厅（ID 100）", rows[1][1])
        self.assertEqual(rows[1][2], "4\n1")

        document = export_records_html(
            self.database_path,
            self.settings_path,
            query,
            ["username", "room_name"],
        ).decode("utf-8")
        self.assertIn('<strong id="record-total">1</strong>', document)
        self.assertIn("2 个厅 · 2 条记录", document)
        self.assertIn("星光厅", document)
        self.assertIn("海风厅", document)
        self.assertIn('class="user-detail-row hidden"', document)

    def test_minimum_estimated_total_filters_after_user_rooms_are_merged(self) -> None:
        with closing(sqlite3.connect(self.database_path)) as connection, connection:
            VoiceHallDatabase._upsert_contribution(
                connection,
                {
                    "room_id": "200",
                    "room_name": "星光厅",
                    "rank": 4,
                    "estimated_contribution_value": 900,
                    "user_id": "u1",
                    "username": "甲",
                    "scanned_at": "2026-09-14T11:00:00+08:00",
                },
            )

        included = RecordQuery.from_query(
            {"min_estimated_contribution_total": ["2400"]},
            today=date(2026, 9, 14),
        )
        excluded = RecordQuery.from_query(
            {"min_estimated_contribution_total": ["2401"]},
            today=date(2026, 9, 14),
        )

        self.assertEqual(query_records(
            self.database_path, self.settings_path, included
        )["total"], 1)
        self.assertEqual(query_records(
            self.database_path, self.settings_path, excluded
        )["total"], 0)

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
            ["username"],
        )
        rows = list(csv.reader(io.StringIO(payload.decode("utf-8-sig"))))
        self.assertEqual(rows, [["用户名 / ID"], ["u-in-minute（ID u-in-minute）"], ["甲（ID u1）"]])

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
            ["username", "wealth_level"],
        )
        rows = list(csv.reader(io.StringIO(payload.decode("utf-8-sig"))))
        self.assertEqual(rows[0], ["用户名 / ID", "财富等级"])
        self.assertEqual(len(rows), 2)
        self.assertEqual(rows[1], ["甲（ID u1）", "20（120元）"])

    def test_csv_export_includes_relative_and_estimated_contributions(self) -> None:
        query = RecordQuery.from_query({}, today=date(2026, 9, 14))
        payload = export_records_csv(
            self.database_path,
            self.settings_path,
            query,
            ["contribution_gap", "estimated_contribution_value"],
        )
        rows = list(csv.reader(io.StringIO(payload.decode("utf-8-sig"))))
        self.assertEqual(rows[0], ["距前一名", "推测贡献值下限"])
        self.assertEqual(rows[1], ["250", "≥1500"])

    def test_csv_export_includes_charm_amount_and_account_assessment(self) -> None:
        query = RecordQuery.from_query({}, today=date(2026, 9, 14))
        payload = export_records_csv(
            self.database_path,
            self.settings_path,
            query,
            ["charm_level", "charm_min_value", "account_assessment"],
        )
        rows = list(csv.reader(io.StringIO(payload.decode("utf-8-sig"))))
        self.assertEqual(
            rows[0],
            ["魅力等级", "等级最低魅力值", "账号判断"],
        )
        self.assertEqual(rows[1], ["3（3元）", "30", "高概率真实玩家"])

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
            ["wealth_level"],
        )
        rows = list(csv.reader(io.StringIO(payload.decode("utf-8-sig"))))
        self.assertEqual(rows[0], ["财富等级"])
        amount_level = next(row[0] for row in rows[1:] if row[0].startswith("200（"))
        self.assertTrue(amount_level.endswith(("元）", "万元）")))

    def test_export_columns_are_allowlisted_and_deduplicated(self) -> None:
        columns = parse_export_columns(
            {"columns": ["room_name,username,room_name,not-a-column"]}
        )
        self.assertEqual(columns, ["room_name", "username"])
        with self.assertRaisesRegex(ValueError, "至少选择"):
            parse_export_columns({"columns": ["not-a-column"]})
        with self.assertRaisesRegex(ValueError, "至少选择"):
            parse_export_columns({"columns": ["wealth_min_yuan"]})
        with self.assertRaisesRegex(ValueError, "至少选择"):
            parse_export_columns({"columns": ["user_id,room_id"]})

    def test_summary_column_order_is_allowlisted_completed_and_exported(self) -> None:
        order = parse_summary_column_order(
            {"column_order": ["estimated_contribution_total,username,bad,username"]}
        )
        self.assertEqual(order[:2], ["estimated_contribution_total", "username"])
        self.assertEqual(len(order), 10)

        query = RecordQuery.from_query({}, today=date(2026, 9, 14))
        document = export_records_html(
            self.database_path,
            self.settings_path,
            query,
            column_order=order,
        ).decode("utf-8")
        self.assertIn(
            "<thead><tr><th>合计推测金额下限</th><th>用户名</th>",
            document,
        )

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
        self.assertIn("copied-user-ids-v1", document)
        self.assertNotIn("hellofish", document.lower())
        self.assertIn('content: "已复制"', document)
        self.assertIn("position: absolute", document)
        self.assertNotIn("background-image:", document)
        self.assertNotIn("财神爷", document)
        self.assertNotIn('content: "福"', document)
        self.assertIn("&lt;script&gt;alert(1)&lt;/script&gt;", document)
        self.assertNotIn("<script>alert(1)</script>", document)
        self.assertNotIn("上一页", document)

    def test_html_export_matches_the_user_summary_view(self) -> None:
        query = RecordQuery.from_query({}, today=date(2026, 9, 14))

        document = export_records_html(
            self.database_path,
            self.settings_path,
            query,
            ["username", "wealth_level"],
        ).decode("utf-8")

        self.assertIn(
            "<th>用户名</th><th>出现厅</th><th>最近记录</th><th>性别</th>",
            document,
        )
        self.assertIn("请使用浏览器打开本文件，点击用户名即可复制用户 ID", document)
        self.assertIn("不推荐直接在微信中查看，微信内无法复制用户 ID", document)
        self.assertIn("<th>合计推测金额下限</th>", document)
        self.assertIn('<td class="estimated-total">≥ 150 元</td>', document)
        self.assertNotIn("hellofish", document.lower())
        self.assertIn("<th>明细时间</th><th>所在厅 / ID</th>", document)
        self.assertIn('class="detail-toggle"', document)
        self.assertIn('data-user-id="u1"', document)
        self.assertIn("120 元", document)

    def test_html_export_endpoint_downloads_a_standalone_file(self) -> None:
        web_root = self.root / "html-export-web"
        web_root.mkdir()
        web_root.joinpath("index.html").write_text("viewer", encoding="utf-8")
        with patch.object(
            VoiceHallDatabase,
            "purge_old_data",
            return_value={"contributions": 0, "level_samples": 0},
        ):
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

    def test_html_export_endpoint_keeps_view_layout_with_legacy_columns(self) -> None:
        web_root = self.root / "html-export-selected-web"
        web_root.mkdir()
        web_root.joinpath("index.html").write_text("viewer", encoding="utf-8")
        with patch.object(
            VoiceHallDatabase,
            "purge_old_data",
            return_value={"contributions": 0, "level_samples": 0},
        ):
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
            "&columns=username,room_name"
        )
        try:
            with urlopen(url, timeout=2) as response:  # noqa: S310
                document = response.read().decode("utf-8")
                self.assertIn(
                    "<th>用户名</th><th>出现厅</th><th>最近记录</th>", document
                )
                self.assertIn("<th>明细时间</th><th>所在厅 / ID</th>", document)
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

    def test_mxu_startup_log_cleanup_only_removes_log_files(self) -> None:
        paths = [
            self.root / "maafw.log",
            self.root / "data" / "voice_hall_agent.log",
            self.root / "debug" / "maafw.bak.1.log",
            self.root / "assets" / "debug" / "nested" / "worker.log.1",
        ]
        for path in paths:
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text("old log", encoding="utf-8")
        database = self.root / "data" / "voice_hall.sqlite3"
        database.write_text("keep", encoding="utf-8")

        cleared, failures = contribution_viewer.clear_runtime_logs(self.root)

        self.assertEqual(cleared, len(paths))
        self.assertEqual(failures, [])
        self.assertTrue(all(not path.exists() for path in paths))
        self.assertEqual(database.read_text(encoding="utf-8"), "keep")

    def test_process_probe_recognizes_the_current_process(self) -> None:
        self.assertTrue(contribution_viewer._process_exists(os.getpid()))
        self.assertFalse(contribution_viewer._process_exists(-1))


if __name__ == "__main__":
    unittest.main()
