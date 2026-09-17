import json
import re
import sys
import tempfile
import unittest
from ctypes import c_char_p, c_int64, c_void_p
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

import numpy as np

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "agent"))

from voice_hall import (  # noqa: E402
    ContributionScanner,
    UI_DUMP_COMMAND,
    UI_DUMP_CONTROLLER_TIMEOUT_MS,
    _ScanStopped,
    _build_ui_dump_command,
    _ensure_shell_api_types,
    _estimate_contribution_values,
    _extract_hall_id,
    _find_contribution_row_data,
    _find_contribution_targets,
    _find_profile_copy_target,
    _normalize_gender_selection,
    _normalize_setting_list,
    _selected_record_genders,
    _should_record_gender,
    _is_contribution_hierarchy,
    _should_save_level_samples,
    _was_scanned_on,
)
from voice_hall_storage import VoiceHallDatabase  # noqa: E402
from wealth_levels import (  # noqa: E402
    WEALTH_LEVEL_MIN_CONTRIBUTIONS,
    minimum_contribution_for_wealth_level,
    wealth_level_for_contribution,
)


def ocr(text: str, box: tuple[int, int, int, int]) -> SimpleNamespace:
    return SimpleNamespace(text=text, box=box)


class FakeJob:
    def __init__(self, value=None) -> None:
        self.value = value

    def get(self, wait: bool = False):
        return self.value

    def wait(self):
        return self


class FakeController:
    hierarchy = """<?xml version='1.0'?>
    <hierarchy>
      <node resource-id="app:id/tv_nice_num" text="71110"
            bounds="[68,624][120,642]" />
      <node resource-id="app:id/iv_copy" text=""
            bounds="[120,621][158,659]" />
      <node resource-id="app:id/iv_gender" text=""
            bounds="[168,629][241,662]" />
      <node resource-id="app:id/tv_location" text="IP:天津"
            bounds="[538,615][644,653]" />
    </hierarchy>"""

    def __init__(self) -> None:
        self.clicks = []
        self.shell_calls = []
        self.swipes = []

    def post_shell(self, command: str, timeout: int) -> FakeJob:
        self.shell_calls.append((command, timeout))
        marker_match = re.search(r"(__MAA_UI_DUMP_\d+__)", command)
        marker = marker_match.group(1) if marker_match else "__missing_marker__"
        return FakeJob(f"{marker}:0\n{self.hierarchy}")

    def post_click(self, x: int, y: int) -> FakeJob:
        self.clicks.append((x, y))
        return FakeJob()

    def post_click_key(self, key: int) -> FakeJob:
        self.clicks.append(("key", key))
        return FakeJob()

    def post_swipe(
        self,
        x1: int,
        y1: int,
        x2: int,
        y2: int,
        duration: int,
    ) -> FakeJob:
        self.swipes.append((x1, y1, x2, y2, duration))
        return FakeJob()


class FakeApiFunction:
    restype = None
    argtypes = None


class FakeFramework:
    MaaControllerPostShell = FakeApiFunction()
    MaaControllerGetShellOutput = FakeApiFunction()


class HallListRecognitionTest(unittest.TestCase):
    def setUp(self) -> None:
        self.scanner = ContributionScanner()

    def test_interruptible_sleep_stops_custom_action_promptly(self) -> None:
        class StoppingTasker:
            checks = 0

            @property
            def stopping(self) -> bool:
                self.checks += 1
                return self.checks >= 3

        tasker = StoppingTasker()
        context = SimpleNamespace(tasker=tasker)
        with patch("voice_hall.time.sleep") as sleep:
            with self.assertRaises(_ScanStopped):
                self.scanner._sleep(context, 5.0)

        self.assertEqual(tasker.checks, 3)
        self.assertEqual(sleep.call_count, 2)

    def test_run_handles_stop_without_unhandled_error(self) -> None:
        with (
            patch.object(self.scanner, "_run", side_effect=_ScanStopped),
            patch.object(self.scanner, "_log") as log,
        ):
            result = self.scanner.run(SimpleNamespace(), SimpleNamespace())

        self.assertFalse(result)
        log.assert_called_once_with("收到停止请求，扫描已停止")

    def test_extracts_hall_id_when_icon_is_joined_to_text(self) -> None:
        self.assertEqual(_extract_hall_id("▥ 120323"), "120323")
        self.assertEqual(_extract_hall_id("'４８９９８'"), "48998")

    def test_recognizes_current_hall_list_layout(self) -> None:
        items = [
            ocr("女神", (48, 61, 64, 38)),
            ocr("男神", (162, 61, 64, 38)),
            ocr("点唱", (276, 61, 64, 38)),
            ocr("天命女友", (230, 187, 150, 38)),
            ocr("严查黑麦/24h聘d", (230, 233, 220, 30)),
            ocr("▥ 120323", (231, 283, 115, 30)),
            ocr("799漫画少女", (230, 397, 220, 38)),
            ocr("48998", (261, 490, 86, 30)),
        ]

        self.assertTrue(self.scanner._is_hall_list(items))
        candidates = self.scanner._find_hall_candidates(items)
        self.assertEqual([item["hall_id"] for item in candidates], ["120323", "48998"])
        self.assertEqual(candidates[0]["name"], "天命女友")

    def test_one_visible_card_is_enough_with_category_anchor(self) -> None:
        items = [
            ocr("女神", (48, 61, 64, 38)),
            ocr("120323", (261, 1080, 90, 30)),
        ]
        self.assertTrue(self.scanner._is_hall_list(items))

    def test_number_outside_card_column_is_not_a_hall(self) -> None:
        items = [
            ocr("120323", (20, 283, 115, 30)),
            ocr("消息", (430, 1220, 64, 38)),
        ]
        self.assertFalse(self.scanner._is_hall_list(items))

    def test_room_page_numbers_are_not_mistaken_for_hall_cards(self) -> None:
        items = [
            ocr("女神", (131, 88, 46, 25)),
            ocr("公告", (48, 141, 55, 25)),
            ocr("房间榜894.45w", (140, 145, 120, 19)),
            ocr("聊聊天", (33, 1201, 79, 33)),
            ocr("99999", (222, 805, 65, 19)),
            ocr("(22212931)", (235, 912, 111, 24)),
        ]

        self.assertFalse(self.scanner._is_hall_list(items))

    def test_contribution_panel_ids_are_not_mistaken_for_halls(self) -> None:
        items = [
            ocr("房间贡献榜", (330, 55, 150, 35)),
            ocr("99999", (222, 805, 65, 19)),
            ocr("22212931", (235, 912, 111, 24)),
        ]

        self.assertFalse(self.scanner._is_hall_list(items))

    def test_contribution_panel_title_must_be_in_header(self) -> None:
        items = [ocr("房间贡献榜", (330, 700, 150, 35))]
        self.assertFalse(self.scanner._is_contribution_panel(items))

    def test_contribution_targets_come_from_ui_hierarchy(self) -> None:
        hierarchy = """<?xml version='1.0'?>
        <hierarchy>
          <node text="房间贡献榜" resource-id="" selected="true"
                bounds="[333,55][463,95]" />
          <node text="" resource-id="app:id/iv_avatar_rank_2"
                bounds="[83,290][179,386]" />
          <node text="" resource-id="app:id/iv_avatar_rank_1"
                bounds="[301,267][420,386]" />
          <node text="" resource-id="app:id/iv_avatar_rank_3"
                bounds="[543,290][639,386]" />
          <node text="" resource-id="app:id/rv_rank_list"
                bounds="[0,547][720,1126]" />
          <node text="4" resource-id="app:id/tv_rank"
                bounds="[13,595][94,627]" />
          <node text="" resource-id="app:id/iv_avatar"
                bounds="[94,574][167,647]" />
          <node text="99+" resource-id="app:id/tv_rank_me"
                bounds="[40,1184][99,1231]" />
          <node text="" resource-id="app:id/iv_avatar"
                bounds="[120,1170][210,1260]" />
        </hierarchy>"""

        self.assertTrue(_is_contribution_hierarchy(hierarchy))
        self.assertEqual(
            _find_contribution_targets(hierarchy),
            (
                [(1, (360, 326)), (2, (131, 338)), (3, (591, 338))],
                [(4, 610)],
            ),
        )

    def test_contribution_hierarchy_requires_selected_contribution_tab(self) -> None:
        hierarchy = """<?xml version='1.0'?>
        <hierarchy>
          <node text="房间守护榜" resource-id="" selected="true"
                bounds="[175,55][305,95]" />
          <node text="" resource-id="app:id/rv_rank_list"
                bounds="[0,547][720,1126]" />
        </hierarchy>"""
        self.assertFalse(_is_contribution_hierarchy(hierarchy))

    def test_contribution_rows_read_ids_and_relative_gaps_from_hierarchy(self) -> None:
        hierarchy = """<?xml version='1.0'?>
        <hierarchy>
          <node text="" resource-id="app:id/iv_avatar_rank_2"
                bounds="[83,299][179,395]" />
          <node text="" resource-id="app:id/iv_avatar_rank_1"
                bounds="[301,276][420,395]" />
          <node text="" resource-id="app:id/iv_avatar_rank_3"
                bounds="[543,299][639,395]" />
          <node text="66666" resource-id="app:id/tv_nice_num"
                bounds="[121,467][173,485]" />
          <node text="140000" resource-id="app:id/tv_nice_num"
                bounds="[347,467][407,485]" />
          <node text="98000" resource-id="app:id/tv_nice_num"
                bounds="[581,467][633,485]" />
          <node text="4" resource-id="app:id/tv_rank"
                bounds="[13,604][94,636]" />
          <node text="距前一名79786" resource-id="app:id/tv_value"
                bounds="[528,604][685,636]" />
          <node text="ID:23512216" resource-id="app:id/tv_user_code"
                bounds="[194,629][325,661]" />
        </hierarchy>"""

        rows = _find_contribution_row_data(hierarchy)

        self.assertEqual(rows[1]["user_id"], "140000")
        self.assertEqual(rows[2]["user_id"], "66666")
        self.assertEqual(rows[3]["user_id"], "98000")
        self.assertEqual(rows[4]["user_id"], "23512216")
        self.assertEqual(rows[4]["contribution_gap"], 79_786)

    def test_estimated_contribution_starts_last_rank_at_one(self) -> None:
        rows = {
            1: {"contribution_gap": None},
            2: {"contribution_gap": None},
            3: {"contribution_gap": None},
            4: {"contribution_gap": 10},
            5: {"contribution_gap": 5},
            6: {"contribution_gap": 3},
        }

        _estimate_contribution_values(rows)

        self.assertEqual(rows[1]["estimated_contribution_value"], 19)
        self.assertEqual(rows[2]["estimated_contribution_value"], 19)
        self.assertEqual(rows[3]["estimated_contribution_value"], 19)
        self.assertEqual(rows[4]["estimated_contribution_value"], 9)
        self.assertEqual(rows[5]["estimated_contribution_value"], 4)
        self.assertEqual(rows[6]["estimated_contribution_value"], 1)

    def test_estimated_contribution_fills_missing_rank_from_known_gaps_behind(self) -> None:
        rows = {
            1: {"contribution_gap": None},
            2: {"contribution_gap": None},
            3: {"contribution_gap": None},
            4: {"contribution_gap": 10},
            6: {"contribution_gap": 3},
            7: {"contribution_gap": 7},
        }

        _estimate_contribution_values(rows)

        self.assertEqual(rows[7]["estimated_contribution_value"], 1)
        self.assertEqual(rows[6]["estimated_contribution_value"], 8)
        self.assertEqual(rows[4]["estimated_contribution_value"], 16)
        self.assertEqual(rows[3]["estimated_contribution_value"], 26)
        self.assertEqual(rows[2]["estimated_contribution_value"], 26)
        self.assertEqual(rows[1]["estimated_contribution_value"], 26)

    def test_profile_id_comes_from_copy_control_not_ocr(self) -> None:
        hierarchy = """<?xml version='1.0' encoding='UTF-8'?>
        <hierarchy>
          <node resource-id="com.sybl.voiceroom:id/tv_nice_num"
                text="71110" bounds="[68,624][120,642]" />
          <node resource-id="com.sybl.voiceroom:id/iv_copy"
                text="" bounds="[120,621][158,659]" />
        </hierarchy>"""

        self.assertEqual(
            _find_profile_copy_target(hierarchy),
            ("71110", (139, 640)),
        )

    def test_ordinary_profile_id_uses_user_code_copy_control(self) -> None:
        hierarchy = """<?xml version='1.0' encoding='UTF-8'?>
        <hierarchy>
          <node resource-id="com.sybl.voiceroom:id/ll_copy"
                text="" bounds="[44,628][203,655]" />
          <node resource-id="com.sybl.voiceroom:id/tv_user_code"
                text="23342974" bounds="[77,628][165,655]" />
        </hierarchy>"""

        self.assertEqual(
            _find_profile_copy_target(hierarchy),
            ("23342974", (123, 641)),
        )

    def test_profile_id_does_not_mix_vanity_and_ordinary_controls(self) -> None:
        hierarchy = """<?xml version='1.0'?>
        <hierarchy>
          <node resource-id="app:id/tv_nice_num" text="71110"
                bounds="[68,624][120,642]" />
          <node resource-id="app:id/ll_copy" text=""
                bounds="[44,628][203,655]" />
        </hierarchy>"""

        self.assertIsNone(_find_profile_copy_target(hierarchy))

    def test_profile_copy_target_rejects_invalid_hierarchy(self) -> None:
        self.assertIsNone(_find_profile_copy_target("not xml"))
        self.assertIsNone(
            _find_profile_copy_target(
                "<?xml version='1.0'?><hierarchy>"
                '<node resource-id="app:id/tv_nice_num" text="abc" '
                'bounds="[1,1][2,2]" />'
                "</hierarchy>"
            )
        )

    def test_copy_profile_id_clicks_the_discovered_copy_button(self) -> None:
        controller = FakeController()
        self.scanner.controller = controller

        self.assertEqual(self.scanner._copy_profile_id(), "71110")
        self.assertEqual(controller.clicks, [(139, 640)])
        self.assertEqual(len(controller.shell_calls), 1)

    def test_ui_dump_has_device_and_controller_timeouts(self) -> None:
        self.assertIn("timeout -k 1 3 uiautomator dump", UI_DUMP_COMMAND)
        self.assertEqual(UI_DUMP_CONTROLLER_TIMEOUT_MS, 5000)

    def test_ui_dump_command_uses_unique_path_and_completion_marker(self) -> None:
        command, marker = _build_ui_dump_command("12345")

        self.assertEqual(marker, "__MAA_UI_DUMP_12345__")
        self.assertIn("/sdcard/maa_voice_hall_12345.xml", command)
        self.assertIn(f'echo "{marker}:$dump_status"', command)
        self.assertIn("rm -f /sdcard/maa_voice_hall_12345.xml", command)

    def test_ui_dump_rejects_stale_shell_output_without_current_marker(self) -> None:
        controller = FakeController()
        controller.post_shell = lambda command, timeout: FakeJob(  # type: ignore[method-assign]
            "__MAA_UI_DUMP_older__:0\n" + controller.hierarchy
        )
        self.scanner.controller = controller

        self.assertEqual(self.scanner._dump_ui_hierarchy(), "")

    def test_copy_profile_id_supports_ordinary_id(self) -> None:
        controller = FakeController()
        controller.hierarchy = """<?xml version='1.0'?>
        <hierarchy>
          <node resource-id="app:id/ll_copy" text=""
                bounds="[44,628][203,655]" />
          <node resource-id="app:id/tv_user_code" text="23342974"
                bounds="[77,628][165,655]" />
        </hierarchy>"""
        self.scanner.controller = controller

        self.assertEqual(self.scanner._copy_profile_id(), "23342974")
        self.assertEqual(controller.clicks, [(123, 641)])

    def test_registers_missing_shell_api_types(self) -> None:
        framework = FakeFramework()
        with patch("voice_hall.Library.framework", return_value=framework):
            _ensure_shell_api_types()

        self.assertEqual(
            framework.MaaControllerPostShell.argtypes,
            [c_void_p, c_char_p, c_int64],
        )
        self.assertEqual(
            framework.MaaControllerGetShellOutput.argtypes,
            [c_void_p, c_void_p],
        )

    def test_profile_id_ocr_fallback_accepts_explicit_id(self) -> None:
        items = [ocr("ID:23391589", (180, 92, 150, 24))]
        self.assertEqual(self.scanner._extract_profile_id(items), "23391589")

    def test_profile_id_ocr_fallback_accepts_number_in_id_area(self) -> None:
        items = [
            ocr("30", (201, 634, 36, 27)),
            ocr("66666", (68, 624, 52, 18)),
            ocr("300", (55, 700, 60, 20)),
        ]
        self.assertEqual(self.scanner._extract_profile_id(items), "66666")

    def test_profile_id_ocr_fallback_removes_joined_badge_zero(self) -> None:
        items = [ocr("066666", (43, 618, 70, 24))]
        self.assertEqual(self.scanner._extract_profile_id(items), "66666")

    def test_profile_id_ocr_fallback_rejects_number_outside_id_area(self) -> None:
        items = [ocr("66666", (300, 900, 80, 25))]
        self.assertIsNone(self.scanner._extract_profile_id(items))

    def test_record_user_uses_ocr_when_copy_retrieval_fails(self) -> None:
        controller = FakeController()
        self.scanner.controller = controller
        items = [
            ocr("66666", (68, 624, 52, 18)),
            ocr("女神", (180, 700, 50, 24)),
            ocr("IP:浙江", (554, 617, 72, 28)),
            ocr("127", (45, 680, 76, 32)),
            ocr("O101", (131, 681, 80, 33)),
        ]
        records = []
        processed_users = set()

        with (
            patch.object(self.scanner, "_copy_profile_id", return_value=None),
            patch.object(self.scanner, "_dump_ui_hierarchy", return_value=""),
            patch.object(self.scanner, "_capture_ocr", return_value=(None, items)),
            patch(
                "voice_hall.VoiceHallDatabase.upsert_contribution",
                return_value=False,
            ) as upsert_database,
            patch.object(self.scanner, "_log"),
            patch("voice_hall.time.sleep"),
        ):
            self.scanner._record_user(
                context=SimpleNamespace(),
                room_id="120323",
                room_name="测试厅",
                rank=1,
                click_point=(362, 335),
                output_path=Path("result.sqlite3"),
                records=records,
                processed_users=processed_users,
                delay=0.1,
                unknown_gender_as_male=True,
            )

        self.assertEqual(records[0]["user_id"], "66666")
        self.assertEqual(records[0]["gender"], "男")
        self.assertEqual(records[0]["gender_source"], "inferred:未识别到性别图标")
        self.assertEqual(records[0]["ip"], "浙江")
        self.assertEqual(records[0]["wealth_level"], 127)
        self.assertEqual(records[0]["charm_level"], 101)
        self.assertIn(("120323", "66666"), processed_users)
        self.assertEqual(controller.clicks, [(362, 335), ("key", 4)])
        upsert_database.assert_called_once()

    def test_record_user_returns_immediately_when_profile_gender_is_filtered(self) -> None:
        controller = FakeController()
        self.scanner.controller = controller

        with (
            patch.object(self.scanner, "_wait_for_profile_page", return_value=(None, [])),
            patch.object(self.scanner, "_dump_ui_hierarchy", return_value=""),
            patch.object(self.scanner, "_extract_gender", return_value=("女", "icon:♀")),
            patch.object(self.scanner, "_copy_profile_id") as copy_profile_id,
            patch.object(self.scanner, "_read_profile_details_with_retry") as read_details,
            patch.object(self.scanner, "_sleep"),
            patch.object(self.scanner, "_log"),
        ):
            result = self.scanner._record_user(
                context=SimpleNamespace(),
                room_id="120323",
                room_name="测试厅",
                rank=5,
                click_point=(130, 610),
                output_path=Path("result.sqlite3"),
                records=[],
                processed_users=set(),
                delay=0.1,
                unknown_gender_as_male=False,
                record_genders={"男"},
                from_leaderboard=True,
            )

        self.assertTrue(result)
        self.assertEqual(controller.clicks, [(130, 610), ("key", 4)])
        copy_profile_id.assert_not_called()
        read_details.assert_not_called()

    def test_record_user_recovers_from_duplicate_stale_hierarchy_id(self) -> None:
        controller = FakeController()
        self.scanner.controller = controller
        records = [{"room_id": "120323", "user_id": "71110"}]
        processed_users = {("120323", "71110")}
        items = [ocr("ID:23342974", (46, 630, 152, 28))]

        with (
            patch.object(self.scanner, "_copy_profile_id", return_value="71110"),
            patch.object(self.scanner, "_capture_ocr", return_value=(None, items)),
            patch(
                "voice_hall.VoiceHallDatabase.upsert_contribution",
                return_value=False,
            ) as upsert_database,
            patch.object(self.scanner, "_log"),
            patch("voice_hall.time.sleep"),
        ):
            self.scanner._record_user(
                context=SimpleNamespace(),
                room_id="120323",
                room_name="测试厅",
                rank=5,
                click_point=(130, 610),
                output_path=Path("result.sqlite3"),
                records=records,
                processed_users=processed_users,
                delay=0.1,
                unknown_gender_as_male=True,
            )

        self.assertEqual(records[-1]["user_id"], "23342974")
        self.assertIn(("120323", "23342974"), processed_users)
        upsert_database.assert_called_once()

    def test_record_user_overwrites_same_day_instead_of_skipping(self) -> None:
        controller = FakeController()
        self.scanner.controller = controller
        records = [
            {
                "room_id": "120323",
                "user_id": "71110",
                "username": "旧名字",
                "scanned_at": "2026-09-14T08:00:00+08:00",
            }
        ]
        processed_users = {("120323", "71110")}
        items = [ocr("ID:71110", (46, 630, 152, 28))]

        with (
            patch.object(self.scanner, "_copy_profile_id", return_value="71110"),
            patch.object(self.scanner, "_capture_ocr", return_value=(None, items)),
            patch.object(self.scanner, "_extract_profile_name", return_value="新名字"),
            patch(
                "voice_hall.VoiceHallDatabase.upsert_contribution",
                return_value=True,
            ) as upsert_database,
            patch.object(
                self.scanner,
                "_now",
                return_value="2026-09-14T12:00:00+08:00",
            ),
            patch.object(self.scanner, "_log"),
            patch("voice_hall.time.sleep"),
        ):
            self.scanner._record_user(
                context=SimpleNamespace(),
                room_id="120323",
                room_name="测试厅",
                rank=5,
                click_point=(130, 610),
                output_path=Path("result.sqlite3"),
                records=records,
                processed_users=processed_users,
                delay=0.1,
                unknown_gender_as_male=False,
            )

        self.assertEqual(len(records), 1)
        self.assertEqual(records[0]["username"], "新名字")
        upsert_database.assert_called_once()

    def test_record_user_persists_complete_record_to_sqlite(self) -> None:
        controller = FakeController()
        self.scanner.controller = controller
        records = []
        processed_users = set()
        items = [ocr("ID:23342974", (46, 630, 152, 28))]

        with (
            patch.object(self.scanner, "_copy_profile_id", return_value="23342974"),
            patch.object(self.scanner, "_capture_ocr", return_value=(None, items)),
            patch(
                "voice_hall.VoiceHallDatabase.upsert_contribution",
                return_value=False,
            ) as upsert_database,
            patch.object(self.scanner, "_log"),
            patch("voice_hall.time.sleep"),
        ):
            self.scanner._record_user(
                context=SimpleNamespace(),
                room_id="120323",
                room_name="测试厅",
                rank=6,
                click_point=(130, 700),
                output_path=Path("result.sqlite3"),
                records=records,
                processed_users=processed_users,
                delay=0.1,
                unknown_gender_as_male=True,
                contribution_gap=3_426,
                estimated_contribution_value=20_000,
        )

        self.assertEqual(records[-1]["user_id"], "23342974")
        persisted = upsert_database.call_args.args[0]
        self.assertEqual(persisted["room_id"], "120323")
        self.assertEqual(persisted["rank"], 6)
        self.assertEqual(persisted["contribution_gap"], 3_426)
        self.assertEqual(persisted["estimated_contribution_value"], 20_000)

    def test_gender_uses_icon_color_on_copy_button_row(self) -> None:
        hierarchy = """<?xml version='1.0'?>
        <hierarchy><node resource-id="app:id/iv_gender" text=""
        bounds="[168,629][241,662]" /></hierarchy>"""
        male_image = np.zeros((1280, 720, 3), dtype=np.uint8)
        female_image = np.zeros((1280, 720, 3), dtype=np.uint8)
        male_image[629:662, 168:201] = (235, 238, 190)
        female_image[629:662, 168:201] = (246, 205, 230)

        self.assertEqual(
            self.scanner._extract_gender(male_image, [], hierarchy, True),
            ("男", "icon:♂"),
        )
        self.assertEqual(
            self.scanner._extract_gender(female_image, [], hierarchy, True),
            ("女", "icon:♀"),
        )

    def test_gender_box_is_inferred_when_gender_resource_is_missing(self) -> None:
        hierarchy = """<?xml version='1.0'?>
        <hierarchy>
          <node resource-id="app:id/ll_copy" text=""
                bounds="[44,628][203,655]" />
          <node resource-id="app:id/tv_user_code" text="22833546"
                bounds="[77,628][165,655]" />
          <node resource-id="app:id/tv_age" text="27"
                bounds="[246,630][282,657]" />
        </hierarchy>"""
        female_image = np.zeros((1280, 720, 3), dtype=np.uint8)
        female_image[625:661, 211:245] = (246, 205, 230)

        self.assertEqual(
            self.scanner._extract_gender(female_image, [], hierarchy, True),
            ("女", "icon:♀"),
        )

    def test_gender_color_uses_multiple_points_instead_of_one_noisy_pixel(self) -> None:
        hierarchy = """<?xml version='1.0'?>
        <hierarchy><node resource-id="app:id/iv_gender" text=""
        bounds="[168,629][241,662]" /></hierarchy>"""
        image = np.zeros((1280, 720, 3), dtype=np.uint8)
        image[629:662, 168:201] = (246, 205, 230)
        # A cyan compression/noise pixel must not overturn the pink majority.
        image[629, 168] = (235, 238, 190)

        self.assertEqual(
            self.scanner._extract_gender(image, [], hierarchy, True),
            ("女", "icon:♀"),
        )

    def test_gender_color_requires_a_clear_multi_point_majority(self) -> None:
        hierarchy = """<?xml version='1.0'?>
        <hierarchy><node resource-id="app:id/iv_gender" text=""
        bounds="[168,629][241,662]" /></hierarchy>"""
        image = np.zeros((1280, 720, 3), dtype=np.uint8)
        image[629:640, 168:179] = (235, 238, 190)
        image[651:662, 190:201] = (246, 205, 230)

        self.assertEqual(
            self.scanner._extract_gender(image, [], hierarchy, False),
            ("未知", "unknown"),
        )

    def test_gender_does_not_use_goddess_or_god_badge_text(self) -> None:
        items = [ocr("女神", (48, 61, 64, 38))]
        self.assertEqual(
            self.scanner._extract_gender(None, items, "", False),
            ("未知", "unknown"),
        )

    def test_extracts_ip_below_avatar_from_hierarchy(self) -> None:
        hierarchy = """<?xml version='1.0'?>
        <hierarchy><node resource-id="app:id/tv_location" text="IP:天津"
        bounds="[538,615][644,653]" /></hierarchy>"""
        self.assertEqual(self.scanner._extract_profile_ip([], hierarchy), "天津")

    def test_extracts_username_from_profile_hierarchy(self) -> None:
        hierarchy = """<?xml version='1.0'?>
        <hierarchy><node resource-id="app:id/tv_nickname" text="iuok"
        bounds="[56,565][122,613]" /></hierarchy>"""

        self.assertEqual(
            self.scanner._extract_profile_name([], hierarchy),
            "iuok",
        )

    def test_username_ocr_fallback_uses_text_above_id_row(self) -> None:
        items = [
            ocr("iuok", (56, 565, 66, 48)),
            ocr("ID 22532364", (46, 628, 159, 27)),
        ]

        self.assertEqual(self.scanner._extract_profile_name(items, ""), "iuok")

    def test_counts_occupied_close_friend_slots_from_hierarchy(self) -> None:
        hierarchy = """<?xml version='1.0'?>
        <hierarchy>
          <node text="挚友" resource-id="" selected="true"
                bounds="[19,378][99,445]" />
          <node resource-id="app:id/recyclerView" text=""
                bounds="[0,445][720,1280]">
            <node bounds="[0,445][720,846]">
              <node resource-id="app:id/tv_header_cp_day" text="一起 627 天"
                    bounds="[325,737][396,776]" />
              <node resource-id="app:id/tv_header_right_name" text="好友甲"
                    bounds="[482,764][601,800]" />
            </node>
            <node bounds="[17,868][228,1118]">
              <node resource-id="app:id/tvCPDay" text="一起 12 天"
                    bounds="[92,1071][153,1094]" />
              <node resource-id="app:id/tvCPName" text="好友乙"
                    bounds="[17,1027][228,1063]" />
            </node>
          </node>
        </hierarchy>"""

        self.assertEqual(
            self.scanner._extract_close_friend_count([], hierarchy),
            2,
        )

    def test_empty_close_friend_list_is_zero(self) -> None:
        hierarchy = """<?xml version='1.0'?>
        <hierarchy>
          <node text="挚友" resource-id="" selected="true"
                bounds="[19,378][99,445]" />
          <node resource-id="app:id/recyclerView" text=""
                bounds="[0,445][720,1280]">
            <node bounds="[0,445][720,846]">
              <node resource-id="app:id/tv_header_cp_day" text="暂无挚友"
                    bounds="[322,737][398,780]" />
              <node resource-id="app:id/tv_header_left_name" text="江南雨"
                    bounds="[142,790][216,826]" />
              <node resource-id="app:id/tv_header_right_name" text="虚位以待"
                    bounds="[493,790][590,826]" />
            </node>
          </node>
        </hierarchy>"""

        self.assertEqual(
            self.scanner._extract_close_friend_count([], hierarchy),
            0,
        )

    def test_friend_container_without_visible_cards_is_unknown(self) -> None:
        hierarchy = """<?xml version='1.0'?>
        <hierarchy>
          <node text="挚友" resource-id="" selected="true"
                bounds="[19,985][99,1052]" />
          <node resource-id="app:id/recyclerView" text=""
                bounds="[0,1052][720,1280]" />
        </hierarchy>"""

        self.assertIsNone(
            self.scanner._extract_close_friend_count([], hierarchy)
        )

    def test_scans_and_merges_multi_page_close_friend_list(self) -> None:
        controller = FakeController()
        self.scanner.controller = controller
        top = """<?xml version='1.0'?><hierarchy>
        <node text="挚友" bounds="[19,985][99,1052]" />
        <node resource-id="app:id/recyclerView" bounds="[0,1052][720,1280]" />
        </hierarchy>"""

        def page(entries: list[tuple[str, int]]) -> str:
            cards = []
            for index, (name, days) in enumerate(entries):
                top_y = 180 + index * 270
                cards.append(
                    f'<node bounds="[17,{top_y}][228,{top_y + 250}]">'
                    f'<node resource-id="app:id/tvCPName" text="{name}" '
                    f'bounds="[17,{top_y + 150}][228,{top_y + 186}]" />'
                    f'<node resource-id="app:id/tvCPDay" text="一起 {days} 天" '
                    f'bounds="[92,{top_y + 194}][153,{top_y + 217}]" />'
                    "</node>"
                )
            return (
                "<?xml version='1.0'?><hierarchy>"
                '<node text="挚友" bounds="[19,380][99,447]" />'
                '<node resource-id="app:id/recyclerView" bounds="[0,447][720,1280]">'
                + "".join(cards)
                + "</node></hierarchy>"
            )

        page1 = page([("甲", 10), ("乙", 20), ("丙", 30)])
        page2 = page([("乙", 20), ("丙", 30), ("丁", 40), ("戊", 50)])
        page3 = page([("丁", 40), ("戊", 50), ("己", 60)])

        with (
            patch.object(
                self.scanner,
                "_dump_ui_hierarchy",
                side_effect=[page1, page2, page3, page3],
            ),
            patch("voice_hall.time.sleep"),
        ):
            count = self.scanner._scan_close_friend_count(
                SimpleNamespace(), [], top, delay=0.1
            )

        self.assertEqual(count, 6)
        self.assertEqual(len(controller.swipes), 4)

    def test_daily_record_upsert_replaces_same_room_user_day(self) -> None:
        records = [
            {
                "room_id": "38405",
                "user_id": "22532364",
                "username": "旧名字",
                "scanned_at": "2026-09-14T08:00:00+08:00",
            },
            {
                "room_id": "38405",
                "user_id": "22532364",
                "username": "重复旧记录",
                "scanned_at": "2026-09-14T09:00:00+08:00",
            },
        ]
        updated_record = {
            "room_id": "38405",
            "user_id": "22532364",
            "username": "新名字",
            "scanned_at": "2026-09-14T12:00:00+08:00",
        }

        self.assertTrue(
            self.scanner._upsert_daily_record(records, updated_record)
        )
        self.assertEqual(records, [updated_record])

    def test_daily_record_upsert_keeps_a_new_day(self) -> None:
        old_record = {
            "room_id": "38405",
            "user_id": "22532364",
            "scanned_at": "2026-09-13T23:59:59+08:00",
        }
        new_record = {
            "room_id": "38405",
            "user_id": "22532364",
            "scanned_at": "2026-09-14T00:00:01+08:00",
        }
        records = [old_record]

        self.assertFalse(self.scanner._upsert_daily_record(records, new_record))
        self.assertEqual(records, [old_record, new_record])

    def test_ip_ocr_fallback_is_limited_to_avatar_area(self) -> None:
        self.assertEqual(
            self.scanner._extract_profile_ip(
                [ocr("IP：上海", (554, 617, 72, 28))], ""
            ),
            "上海",
        )
        self.assertIsNone(
            self.scanner._extract_profile_ip(
                [ocr("IP:不应命中", (100, 900, 120, 28))], ""
            )
        )

    def test_extracts_standard_profile_wealth_and_charm_levels(self) -> None:
        items = [
            ocr("ID 22214006", (46, 630, 152, 28)),
            ocr("O7", (214, 630, 33, 30)),
            ocr("126", (239, 633, 45, 27)),
            ocr("127", (45, 680, 76, 32)),
            ocr("O101", (131, 681, 80, 33)),
            ocr("粉丝:42", (289, 628, 85, 29)),
        ]

        self.assertEqual(self.scanner._extract_profile_levels(items), (127, 101))

    def test_profile_level_uses_visible_digit_count_to_fix_77(self) -> None:
        image = np.zeros((1280, 720, 3), dtype=np.uint8)
        # The OCR box says 177, while the bright glyphs in that same box are
        # visibly only two digits. This reproduces the current level-77 page.
        image[687:702, 86:95] = 255
        image[687:702, 98:107] = 255
        items = [
            ocr("ID 23437464", (48, 629, 150, 23)),
            ocr("177", (74, 681, 35, 21)),
            ocr("10", (131, 676, 75, 33)),
        ]

        self.assertEqual(
            self.scanner._extract_profile_levels(items, image),
            (77, 10),
        )

    def test_profile_levels_accept_numbers_joined_to_badge_or_following_text(self) -> None:
        items = [
            ocr("ID 165007", (43, 614, 81, 26)),
            ocr("1:151", (41, 677, 80, 29)),
            ocr("161钱比爱情", (132, 674, 168, 36)),
        ]

        self.assertEqual(self.scanner._extract_profile_levels(items), (151, 161))

    def test_hidden_profile_levels_are_not_retried(self) -> None:
        items = [
            ocr("ID 22992251", (51, 629, 144, 23)),
            ocr("???", (76, 682, 33, 22)),
            ocr("???", (162, 680, 39, 25)),
        ]
        context = SimpleNamespace(run_recognition_direct=lambda *_: self.fail())

        self.assertEqual(
            self.scanner._retry_missing_profile_levels(
                context,
                np.zeros((1280, 720, 3), dtype=np.uint8),
                items,
                None,
                None,
            ),
            (None, None),
        )

    def test_hidden_level_dots_prevent_retry_when_full_ocr_misses_question_marks(self) -> None:
        image = np.zeros((1280, 720, 3), dtype=np.uint8)
        for x in (82, 93, 104):
            image[703:706, x : x + 3] = 255
        items = [ocr("ID 23136443", (51, 633, 119, 23))]
        context = SimpleNamespace(run_recognition_direct=lambda *_: self.fail())

        self.assertEqual(
            self.scanner._retry_missing_profile_levels(
                context,
                image,
                items,
                None,
                151,
            ),
            (None, 151),
        )

    def test_retry_level_text_allows_common_digit_confusions(self) -> None:
        self.assertEqual(self.scanner._level_from_retry_text("O45"), 45)
        self.assertEqual(self.scanner._level_from_retry_text("I0"), 10)
        self.assertEqual(self.scanner._level_from_retry_text("¥197"), 197)
        self.assertIsNone(self.scanner._level_from_retry_text("???"))

    def test_profile_levels_ignore_numbers_outside_level_row(self) -> None:
        items = [
            ocr("ID 22214006", (46, 630, 152, 28)),
            ocr("300", (45, 780, 60, 25)),
            ocr("88", (300, 681, 40, 25)),
        ]

        self.assertEqual(self.scanner._extract_profile_levels(items), (None, None))

    def test_saves_and_indexes_unrecognized_level_sample(self) -> None:
        image = np.zeros((1280, 720, 3), dtype=np.uint8)
        record = {
            "room_id": "38405",
            "room_name": "测试厅",
            "rank": 8,
            "user_id": "22532364",
            "username": "测试用户",
            "wealth_level": None,
            "charm_level": 134,
            "scanned_at": "2026-09-14T12:30:00+08:00",
        }

        with tempfile.TemporaryDirectory() as temp_dir, patch.object(
            self.scanner, "_log"
        ):
            output_path = Path(temp_dir) / "contributions.sqlite3"
            sample_path = self.scanner._save_level_sample(
                image,
                output_path,
                record,
                ["wealth_level"],
            )

            self.assertIsNotNone(sample_path)
            self.assertTrue(Path(str(sample_path)).is_file())
            database = VoiceHallDatabase(output_path)
            entries = database.fetch_all("SELECT * FROM level_samples")
            self.assertEqual(len(entries), 1)
            self.assertEqual(
                json.loads(entries[0]["missing_fields"]),
                ["wealth_level"],
            )
            self.assertEqual(entries[0]["screenshot"], sample_path)

            updated_record = {**record, "rank": 7, "charm_level": None}
            updated_path = self.scanner._save_level_sample(
                image,
                output_path,
                updated_record,
                ["wealth_level", "charm_level"],
            )
            updated_entries = database.fetch_all("SELECT * FROM level_samples")

            self.assertEqual(updated_path, sample_path)
            self.assertEqual(len(updated_entries), 1)
            self.assertEqual(updated_entries[0]["rank"], 7)
            self.assertEqual(
                json.loads(updated_entries[0]["missing_fields"]),
                ["wealth_level", "charm_level"],
            )

    def test_packaged_release_does_not_save_unrecognized_level_samples(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            project_root = Path(temp_dir)
            self.assertTrue(_should_save_level_samples(project_root))

            (project_root / "interface.json").write_text("{}", encoding="utf-8")
            (project_root / "maafw").mkdir()

            self.assertFalse(_should_save_level_samples(project_root))

    def test_wealth_level_mapping_covers_all_levels_and_boundaries(self) -> None:
        self.assertEqual(len(WEALTH_LEVEL_MIN_CONTRIBUTIONS), 301)
        self.assertEqual(minimum_contribution_for_wealth_level(77), 95_000)
        self.assertEqual(minimum_contribution_for_wealth_level(151), 1_650_000)
        self.assertEqual(minimum_contribution_for_wealth_level(300), 220_000_000)
        self.assertEqual(wealth_level_for_contribution(94_999), 76)
        self.assertEqual(wealth_level_for_contribution(95_000), 77)
        self.assertEqual(wealth_level_for_contribution(999_999_999), 300)

    def test_sqlite_upsert_and_enriched_frontend_view(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            database = VoiceHallDatabase(Path(temp_dir) / "voice_hall.sqlite3")
            record = {
                "room_id": "64900",
                "room_name": "测试厅",
                "rank": 2,
                "contribution_gap": 120,
                "estimated_contribution_value": 345,
                "user_id": "23437464",
                "username": "用户23437464",
                "wealth_level": 77,
                "charm_level": 10,
                "scanned_at": "2026-09-14T12:30:00+08:00",
            }

            self.assertFalse(database.upsert_contribution(record))
            self.assertTrue(
                database.upsert_contribution({**record, "rank": 1, "username": "新名字"})
            )
            rows = database.fetch_all(
                "SELECT * FROM contribution_records_enriched"
            )
            thresholds = database.fetch_all(
                "SELECT * FROM wealth_level_thresholds ORDER BY level"
            )

            self.assertEqual(len(rows), 1)
            self.assertEqual(rows[0]["rank"], 1)
            self.assertEqual(rows[0]["contribution_gap"], 120)
            self.assertEqual(rows[0]["estimated_contribution_value"], 345)
            self.assertEqual(rows[0]["username"], "新名字")
            self.assertEqual(rows[0]["wealth_min_contribution"], 95_000)
            self.assertEqual(rows[0]["next_wealth_level"], 78)
            self.assertEqual(rows[0]["next_wealth_min_contribution"], 100_000)
            self.assertEqual(len(thresholds), 301)

    def test_sqlite_starts_empty_and_does_not_import_legacy_files(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            directory = Path(temp_dir)
            (directory / "voice_hall_contributions.jsonl").write_text(
                json.dumps(
                    {
                        "room_id": "old-room",
                        "user_id": "old-user",
                        "scanned_at": "2026-09-13T12:00:00+08:00",
                    }
                ),
                encoding="utf-8",
            )
            database = VoiceHallDatabase(directory / "voice_hall.sqlite3")
            database.initialize()

            contributions = database.fetch_all("SELECT * FROM contributions")
            thresholds = database.fetch_all("SELECT * FROM wealth_level_thresholds")
            contribution_columns = {
                row["name"]
                for row in database.fetch_all("PRAGMA table_info(contributions)")
            }

            self.assertEqual(contributions, [])
            self.assertEqual(len(thresholds), 301)
            self.assertNotIn("wealth_min_contribution", contribution_columns)

    def test_contribution_scroll_starts_above_bottom_overlay(self) -> None:
        controller = FakeController()
        self.scanner.controller = controller
        next_page = [
            ocr("房间贡献榜", (330, 55, 150, 35)),
            ocr("9", (48, 390, 18, 24)),
            ocr("10", (43, 560, 28, 24)),
        ]

        with (
            patch.object(self.scanner, "_capture_ocr", return_value=(None, next_page)),
            patch("voice_hall.time.sleep"),
        ):
            result = self.scanner._scroll_contribution(
                SimpleNamespace(),
                before_ranks=(4, 5, 6, 7, 8),
                delay=0.1,
            )

        self.assertIs(result, next_page)
        self.assertEqual(controller.swipes, [(650, 1040, 650, 650, 700)])

    def test_record_user_does_not_treat_leaderboard_as_profile(self) -> None:
        controller = FakeController()
        self.scanner.controller = controller
        leaderboard = [
            ocr("房间贡献榜", (330, 55, 150, 35)),
            ocr("ID:23485897", (76, 461, 115, 28)),
            ocr("14", (42, 1050, 28, 30)),
        ]

        with (
            patch.object(self.scanner, "_copy_profile_id", return_value=None),
            patch.object(
                self.scanner,
                "_capture_ocr",
                return_value=(None, leaderboard),
            ),
            patch(
                "voice_hall.VoiceHallDatabase.upsert_contribution",
            ) as upsert_database,
            patch.object(self.scanner, "_log") as log,
            patch("voice_hall.time.sleep"),
        ):
            result = self.scanner._record_user(
                context=SimpleNamespace(),
                room_id="51795",
                room_name="坏天气の新厅开业",
                rank=14,
                click_point=(130, 1063),
                output_path=Path("result.sqlite3"),
                records=[],
                processed_users=set(),
                delay=0.1,
                unknown_gender_as_male=False,
            )

        self.assertFalse(result)
        self.assertEqual(controller.clicks, [(130, 1063)])
        upsert_database.assert_not_called()
        log.assert_called_once_with("排名 14 的头像点击后仍在贡献榜，跳过")

    def test_record_user_waits_for_delayed_profile_page(self) -> None:
        leaderboard = [ocr("房间贡献榜", (330, 55, 150, 35))]
        loading_profile = [ocr("游客", (300, 260, 80, 30))]
        loaded_profile = [
            ocr("挚友 礼物墙 装扮展馆", (20, 980, 460, 32)),
            ocr("关注", (560, 700, 90, 38)),
        ]

        with (
            patch.object(
                self.scanner,
                "_capture_ocr",
                side_effect=[
                    (None, leaderboard),
                    (None, loading_profile),
                    (None, loaded_profile),
                    (None, loaded_profile),
                ],
            ) as capture,
            patch.object(self.scanner, "_sleep") as sleep,
        ):
            image, items = self.scanner._wait_for_profile_page(SimpleNamespace())

        self.assertIsNone(image)
        self.assertIs(items, loaded_profile)
        self.assertEqual(capture.call_count, 4)
        self.assertEqual(
            [call.args[1] for call in sleep.call_args_list],
            [0.8, 0.8, 0.8],
        )

    def test_profile_details_retry_unknown_fields_after_buffer(self) -> None:
        first_items = [ocr("ID:71110", (68, 624, 100, 25))]
        stable_items = [
            ocr("ID:71110", (68, 624, 100, 25)),
            ocr("测试用户", (80, 560, 150, 25)),
            ocr("IP:浙江", (540, 624, 100, 25)),
            ocr("♂", (170, 624, 30, 25)),
            ocr("120", (45, 680, 70, 30)),
            ocr("88", (130, 680, 70, 30)),
        ]

        with (
            patch.object(self.scanner, "_capture_ocr", return_value=(None, stable_items)),
            patch.object(self.scanner, "_dump_ui_hierarchy", return_value=""),
            patch.object(self.scanner, "_sleep") as sleep,
        ):
            details = self.scanner._read_profile_details_with_retry(
                SimpleNamespace(),
                None,
                first_items,
                "",
                False,
            )

        self.assertEqual(details[3], "男")
        self.assertEqual(details[5], "测试用户")
        self.assertEqual(details[6], "浙江")
        self.assertEqual(details[7:], (120, 88))
        sleep.assert_called_once_with(SimpleNamespace(), 0.8)

    def test_partial_profile_page_is_recognized_for_back_navigation(self) -> None:
        controller = FakeController()
        self.scanner.controller = controller
        items = [
            ocr("挚友 礼物墙 装扮展馆", (20, 980, 460, 32)),
            ocr("解锁挚友位", (220, 1040, 180, 32)),
            ocr("关注", (560, 700, 90, 38)),
            ocr("游客", (300, 260, 80, 30)),
        ]

        self.assertTrue(self.scanner._is_profile_page(items))
        with (
            patch.object(
                self.scanner,
                "_capture_ocr",
                side_effect=[(None, items), (None, [])],
            ),
            patch.object(
                self.scanner,
                "_is_hall_list",
                side_effect=[False, True],
            ),
            patch.object(self.scanner, "_sleep"),
        ):
            returned = self.scanner._return_to_hall_list(
                SimpleNamespace(),
                max_attempts=2,
            )

        self.assertTrue(returned)
        self.assertEqual(controller.clicks, [("key", 4)])

    def test_contribution_scan_skips_unopenable_mystery_user(self) -> None:
        controller = FakeController()
        self.scanner.controller = controller
        hierarchy = """<?xml version='1.0'?>
        <hierarchy>
          <node text="房间贡献榜" resource-id="" selected="true"
                bounds="[333,55][463,95]" />
          <node text="" resource-id="app:id/rv_rank_list"
                bounds="[0,547][720,1126]" />
          <node text="13" resource-id="app:id/tv_rank"
                bounds="[13,535][94,567]" />
          <node text="" resource-id="app:id/iv_avatar"
                bounds="[94,514][167,587]" />
          <node text="14" resource-id="app:id/tv_rank"
                bounds="[13,662][94,694]" />
          <node text="" resource-id="app:id/iv_avatar"
                bounds="[94,641][167,714]" />
          <node text="神秘人" resource-id="app:id/tv_nickname"
                bounds="[194,641][263,674]" />
          <node text="15" resource-id="app:id/tv_rank"
                bounds="[13,789][94,821]" />
          <node text="" resource-id="app:id/iv_avatar"
                bounds="[94,768][167,841]" />
        </hierarchy>"""

        with (
            patch.object(self.scanner, "_open_contribution_panel"),
            patch.object(
                self.scanner,
                "_capture_ocr",
                return_value=(None, [ocr("房间贡献榜", (330, 55, 150, 35))]),
            ),
            patch.object(
                self.scanner,
                "_dump_ui_hierarchy",
                return_value=hierarchy,
            ),
            patch.object(self.scanner, "_record_user") as record,
            patch.object(self.scanner, "_scroll_contribution", return_value=None),
            patch.object(self.scanner, "_log") as log,
        ):
            self.scanner._scan_contribution(
                context=SimpleNamespace(),
                room_id="51795",
                room_name="坏天气の新厅开业",
                output_path=Path("result.sqlite3"),
                records=[],
                processed_users=set(),
                delay=0.1,
                max_pages=1,
                max_users=0,
                include_top3=False,
                unknown_gender_as_male=False,
            )

        self.assertEqual(
            [call.kwargs["rank"] for call in record.call_args_list],
            [13, 15],
        )
        log.assert_any_call("排名 14 为神秘人，资料页不可访问，跳过")

    def test_contribution_scan_stops_at_configured_rank_limit(self) -> None:
        controller = FakeController()
        self.scanner.controller = controller
        hierarchy = """<?xml version='1.0'?>
        <hierarchy>
          <node text="房间贡献榜" resource-id="" selected="true"
                bounds="[333,55][463,95]" />
          <node text="" resource-id="app:id/iv_avatar_rank_1"
                bounds="[301,267][420,386]" />
          <node text="" resource-id="app:id/iv_avatar_rank_2"
                bounds="[83,290][179,386]" />
          <node text="" resource-id="app:id/iv_avatar_rank_3"
                bounds="[543,290][639,386]" />
          <node text="" resource-id="app:id/rv_rank_list"
                bounds="[0,547][720,1126]" />
          <node text="4" resource-id="app:id/tv_rank"
                bounds="[13,595][94,627]" />
          <node text="" resource-id="app:id/iv_avatar"
                bounds="[94,574][167,647]" />
          <node text="5" resource-id="app:id/tv_rank"
                bounds="[13,695][94,727]" />
          <node text="" resource-id="app:id/iv_avatar"
                bounds="[94,674][167,747]" />
        </hierarchy>"""

        with (
            patch.object(self.scanner, "_open_contribution_panel"),
            patch.object(
                self.scanner,
                "_capture_ocr",
                return_value=(None, [ocr("房间贡献榜", (330, 55, 150, 35))]),
            ),
            patch.object(self.scanner, "_dump_ui_hierarchy", return_value=hierarchy),
            patch.object(self.scanner, "_record_user") as record_user,
            patch.object(self.scanner, "_scroll_contribution") as scroll,
            patch.object(self.scanner, "_log"),
        ):
            self.scanner._scan_contribution(
                context=SimpleNamespace(),
                room_id="120323",
                room_name="测试厅",
                output_path=Path("result.sqlite3"),
                records=[],
                processed_users={
                    ("120323", "historical-1"),
                    ("120323", "historical-2"),
                    ("120323", "historical-3"),
                    ("120323", "historical-4"),
                },
                delay=0.1,
                max_pages=100,
                max_users=4,
                include_top3=True,
                unknown_gender_as_male=False,
            )

        self.assertEqual(
            [call.kwargs["rank"] for call in record_user.call_args_list],
            [1, 2, 3, 4],
        )
        scroll.assert_not_called()

    def test_missing_rank_limit_uses_safe_default_of_100(self) -> None:
        context = SimpleNamespace(
            tasker=SimpleNamespace(
                controller=FakeController(),
                stopping=False,
            )
        )
        argv = SimpleNamespace(
            custom_action_param=json.dumps({"skip_scanned_today": False})
        )
        with (
            patch.object(self.scanner, "_return_to_hall_list", return_value=False),
            patch.object(self.scanner, "_log") as log,
        ):
            self.assertFalse(self.scanner._run(context, argv))

        self.assertTrue(
            any(
                "每厅扫描上限=100" in str(argument)
                for call in log.call_args_list
                for argument in call.args
            )
        )
        self.assertTrue(
            any(
                "扫描日期=" in str(argument)
                for call in log.call_args_list
                for argument in call.args
            )
        )

    def test_interface_defaults_contribution_rank_limit_to_100(self) -> None:
        project_root = Path(__file__).resolve().parents[1]
        interface = json.loads(
            (project_root / "assets" / "interface.json").read_text(encoding="utf-8")
        )
        pipeline = json.loads(
            (
                project_root
                / "assets"
                / "resource"
                / "pipeline"
                / "my_task.json"
            ).read_text(encoding="utf-8")
        )

        scan_task = next(
            task for task in interface["task"] if task["entry"] == "VoiceHallScanStart"
        )
        rank_limit = interface["option"]["ContributionRankLimit"]
        self.assertIn("ContributionRankLimit", scan_task["option"])
        self.assertIn("SkipScannedToday", scan_task["option"])
        self.assertIn("ScanGenderFilter", scan_task["option"])
        self.assertIn("RecordGenders", scan_task["option"])
        self.assertEqual(rank_limit["inputs"][0]["default"], "100")
        self.assertEqual(
            pipeline["VoiceHallScanStart"]["custom_action_param"][
                "max_users_per_hall"
            ],
            100,
        )
        scan_params = pipeline["VoiceHallScanStart"]["custom_action_param"]
        self.assertEqual(
            {
                name: scan_params[name]
                for name in (
                    "record_gender_male",
                    "record_gender_female",
                    "record_gender_unknown",
                )
            },
            {
                "record_gender_male": False,
                "record_gender_female": False,
                "record_gender_unknown": False,
            },
        )
        skip_today = interface["option"]["SkipScannedToday"]
        record_genders = interface["option"]["RecordGenders"]
        self.assertEqual(skip_today["default_case"], "No")
        self.assertEqual(record_genders["type"], "checkbox")
        self.assertEqual(
            record_genders["default_case"],
            ["Male", "Female", "Unknown"],
        )
        self.assertEqual(record_genders["min_count"], 1)
        self.assertEqual(
            [case["label"] for case in record_genders["cases"]],
            ["男", "女", "未知"],
        )
        self.assertEqual(
            rank_limit["pipeline_override"].keys(),
            {"VoiceHallScanStart"},
        )
        self.assertEqual(
            rank_limit["pipeline_override"]["VoiceHallScanStart"]
            ["custom_action_param"]["max_users_per_hall"],
            "{limit}",
        )
        for case in skip_today["cases"]:
            self.assertEqual(
                case["pipeline_override"].keys(),
                {"VoiceHallScanStart"},
            )
        self.assertEqual(
            skip_today["cases"][0]["pipeline_override"]["VoiceHallScanStart"]
            ["custom_action"],
            "scan_voice_hall_contributions_skip_scanned",
        )
        self.assertIn(
            "[查看贡献记录](http://127.0.0.1:8765/)",
            scan_task["description"],
        )
        self.assertNotIn(
            "OpenContributionViewer",
            {task["entry"] for task in interface["task"]},
        )
        self.assertNotIn("OpenContributionViewer", pipeline)

    def test_scan_gender_filter_normalizes_aliases_and_filters_unknown(self) -> None:
        self.assertEqual(_normalize_setting_list("123, 456\n789"), {"123", "456", "789"})
        self.assertEqual(_normalize_gender_selection("male,女"), {"男", "女"})
        self.assertFalse(_should_record_gender("未知", {"女"}))
        self.assertTrue(_should_record_gender("女", {"女"}))
        self.assertFalse(_should_record_gender("男", {"女"}))
        self.assertEqual(_selected_record_genders({}), {"男", "女", "未知"})
        self.assertEqual(
            _selected_record_genders(
                {
                    "record_gender_male": "true",
                    "record_gender_female": "false",
                    "record_gender_unknown": True,
                }
            ),
            {"男", "未知"},
        )

    def test_run_honors_false_string_gender_flags(self) -> None:
        context = SimpleNamespace(
            tasker=SimpleNamespace(
                controller=FakeController(),
                stopping=False,
            )
        )
        argv = SimpleNamespace(
            custom_action_param=json.dumps(
                {
                    "record_gender_male": "true",
                    "record_gender_female": "false",
                    "record_gender_unknown": "false",
                }
            )
        )
        with (
            patch.object(self.scanner, "_return_to_hall_list", return_value=False),
            patch.object(self.scanner, "_log") as log,
        ):
            self.assertFalse(self.scanner._run(context, argv))

        self.assertTrue(
            any(
                "记录性别=男" in str(argument)
                for call in log.call_args_list
                for argument in call.args
            )
        )
        self.assertFalse(
            any(
                "记录性别=女" in str(argument) or "记录性别=未知" in str(argument)
                for call in log.call_args_list
                for argument in call.args
            )
        )

    def test_scanned_hall_state_only_matches_the_same_day(self) -> None:
        state = {"scanned_at": "2026-09-14T23:59:59+08:00"}

        self.assertTrue(_was_scanned_on(state, "2026-09-14"))
        self.assertFalse(_was_scanned_on(state, "2026-09-15"))
        self.assertFalse(_was_scanned_on({}, "2026-09-14"))
        self.assertFalse(_was_scanned_on("2026-09-14", "2026-09-14"))

    def test_interface_exposes_single_hall_debug_task(self) -> None:
        project_root = Path(__file__).resolve().parents[1]
        interface = json.loads(
            (project_root / "assets" / "interface.json").read_text(encoding="utf-8")
        )
        pipeline = json.loads(
            (
                project_root
                / "assets"
                / "resource"
                / "pipeline"
                / "my_task.json"
            ).read_text(encoding="utf-8")
        )

        debug_task = next(
            task
            for task in interface["task"]
            if task["entry"] == "VoiceHallSingleDebugStart"
        )
        self.assertFalse(debug_task["default_check"])
        self.assertEqual(
            debug_task["option"],
            ["SingleHallDebugConfig"],
        )
        debug_option = interface["option"]["SingleHallDebugConfig"]
        self.assertEqual(
            debug_option["pipeline_override"]["VoiceHallSingleDebugStart"]
            ["custom_action_param"]["room_id"],
            "{hall_id}",
        )
        self.assertTrue(
            debug_option["pipeline_override"]["VoiceHallSingleDebugStart"]
            ["custom_action_param"]["single_hall"]
        )
        self.assertEqual(
            debug_option["pipeline_override"]["VoiceHallSingleDebugStart"]
            ["custom_action_param"]["max_users_per_hall"],
            "{limit}",
        )
        debug_params = pipeline["VoiceHallSingleDebugStart"]["custom_action_param"]
        self.assertTrue(debug_params["single_hall"])
        self.assertEqual(debug_params["max_users_per_hall"], 100)

    def test_interface_starts_contribution_viewer_on_project_load(self) -> None:
        project_root = Path(__file__).resolve().parents[1]
        interface = json.loads(
            (project_root / "assets" / "interface.json").read_text(encoding="utf-8")
        )

        startup = interface["startup"]
        self.assertEqual(startup["exec"], "python")
        self.assertEqual(
            startup["args"],
            ["-u", "agent/contribution_viewer.py", "--serve"],
        )
        self.assertEqual(startup["cwd"], ".")
        self.assertNotIn("--owner-pid", startup["args"])

    def test_entry_rank_limit_and_skip_action_are_combined(self) -> None:
        context = SimpleNamespace(
            tasker=SimpleNamespace(
                controller=FakeController(),
                stopping=False,
            ),
        )
        argv = SimpleNamespace(
            custom_action_param=json.dumps({"max_users_per_hall": 5}),
            custom_action_name="scan_voice_hall_contributions_skip_scanned",
        )
        with (
            patch.object(self.scanner, "_return_to_hall_list", return_value=False),
            patch.object(self.scanner, "_log") as log,
        ):
            self.assertFalse(self.scanner._run(context, argv))

        self.assertTrue(
            any(
                "每厅扫描上限=5" in str(argument)
                and "跳过今日已扫描厅=True" in str(call.args)
                for call in log.call_args_list
                for argument in call.args
            )
        )

    def test_single_hall_debug_scans_current_room_without_hall_list_state(self) -> None:
        controller = FakeController()
        context = SimpleNamespace(
            tasker=SimpleNamespace(controller=controller, stopping=False)
        )
        room_items = [
            ocr("公告", (48, 141, 55, 25)),
            ocr("聊聊天", (33, 1201, 79, 33)),
        ]

        with tempfile.TemporaryDirectory() as temp_dir:
            database_path = Path(temp_dir) / "voice_hall.sqlite3"
            state_path = Path(temp_dir) / "state.json"
            argv = SimpleNamespace(
                custom_action_param=json.dumps(
                    {
                        "database": str(database_path),
                        "state_file": str(state_path),
                        "single_hall": True,
                        "room_id": "１２３４５",
                        "max_users_per_hall": 12,
                        "action_delay": 0,
                    }
                )
            )
            with (
                patch.object(
                    self.scanner,
                    "_capture_ocr",
                    return_value=(None, room_items),
                ),
                patch.object(
                    self.scanner,
                    "_scan_contribution",
                    return_value=True,
                ) as scan,
                patch.object(self.scanner, "_return_to_hall_list") as return_to_list,
                patch.object(self.scanner, "_log"),
            ):
                result = self.scanner._run(context, argv)

            self.assertTrue(result)
            return_to_list.assert_not_called()
            self.assertFalse(state_path.exists())
            self.assertEqual(scan.call_args.kwargs["room_id"], "12345")
            self.assertEqual(scan.call_args.kwargs["room_name"], "12345")
            self.assertEqual(scan.call_args.kwargs["max_users"], 12)

    def test_regular_scan_retries_recorded_hall_and_preserves_state_on_failure(self) -> None:
        controller = FakeController()
        context = SimpleNamespace(
            tasker=SimpleNamespace(controller=controller, stopping=False)
        )
        candidate = {"hall_id": "12345", "name": "测试厅", "card_y": 300}
        hall_list_items = [ocr("聊天室", (20, 1180, 100, 30))]
        room_items = [
            ocr("公告", (48, 141, 55, 25)),
            ocr("聊聊天", (33, 1201, 79, 33)),
        ]

        with tempfile.TemporaryDirectory() as temp_dir:
            database_path = Path(temp_dir) / "voice_hall.sqlite3"
            state_path = Path(temp_dir) / "state.json"
            existing_state = {
                "visited_halls": {
                    "12345": {
                        "name": "测试厅",
                        "scanned_at": "2026-09-14T08:00:00+08:00",
                    }
                }
            }
            state_path.write_text(
                json.dumps(existing_state, ensure_ascii=False),
                encoding="utf-8",
            )
            argv = SimpleNamespace(
                custom_action_param=json.dumps(
                    {
                        "database": str(database_path),
                        "state_file": str(state_path),
                        "max_hall_pages": 1,
                        "action_delay": 0,
                    }
                )
            )
            with (
                patch.object(
                    self.scanner,
                    "_capture_ocr",
                    side_effect=[
                        (None, hall_list_items),
                        (None, room_items),
                        (None, hall_list_items),
                    ],
                ),
                patch.object(self.scanner, "_return_to_hall_list", return_value=True),
                patch.object(self.scanner, "_is_hall_list", return_value=True),
                patch.object(
                    self.scanner,
                    "_find_hall_candidates",
                    return_value=[candidate],
                ),
                patch.object(
                    self.scanner,
                    "_open_hall",
                    return_value=True,
                ) as enter,
                patch.object(
                    self.scanner,
                    "_scan_contribution",
                    side_effect=[False, False],
                ) as scan,
                patch.object(self.scanner, "_scroll_hall_list"),
                patch.object(self.scanner, "_sleep") as sleep,
                patch.object(self.scanner, "_log"),
            ):
                result = self.scanner._run(context, argv)

            self.assertTrue(result)
            self.assertEqual(scan.call_count, 2)
            self.assertEqual(enter.call_count, 3)
            self.assertEqual(
                [call.args[1] for call in sleep.call_args_list],
                [3.0, 1.5],
            )
            self.assertEqual(
                json.loads(state_path.read_text(encoding="utf-8")),
                existing_state,
            )

    def test_regular_scan_records_hall_when_reentry_retry_succeeds(self) -> None:
        controller = FakeController()
        context = SimpleNamespace(
            tasker=SimpleNamespace(controller=controller, stopping=False)
        )
        candidate = {"hall_id": "12345", "name": "测试厅", "card_y": 300}
        hall_list_items = [ocr("聊天室", (20, 1180, 100, 30))]
        room_items = [
            ocr("公告", (48, 141, 55, 25)),
            ocr("聊聊天", (33, 1201, 79, 33)),
        ]

        with tempfile.TemporaryDirectory() as temp_dir:
            database_path = Path(temp_dir) / "voice_hall.sqlite3"
            state_path = Path(temp_dir) / "state.json"
            argv = SimpleNamespace(
                custom_action_param=json.dumps(
                    {
                        "database": str(database_path),
                        "state_file": str(state_path),
                        "max_hall_pages": 1,
                        "action_delay": 0,
                    }
                )
            )
            with (
                patch.object(
                    self.scanner,
                    "_capture_ocr",
                    side_effect=[
                        (None, hall_list_items),
                        (None, room_items),
                        (None, hall_list_items),
                    ],
                ),
                patch.object(self.scanner, "_return_to_hall_list", return_value=True),
                patch.object(self.scanner, "_is_hall_list", return_value=True),
                patch.object(
                    self.scanner,
                    "_find_hall_candidates",
                    return_value=[candidate],
                ),
                patch.object(self.scanner, "_open_hall", return_value=True) as enter,
                patch.object(
                    self.scanner,
                    "_scan_contribution",
                    side_effect=[False, True],
                ) as scan,
                patch.object(self.scanner, "_scroll_hall_list"),
                patch.object(self.scanner, "_sleep"),
                patch.object(self.scanner, "_now", return_value="2026-09-14T20:30:00+08:00"),
                patch.object(self.scanner, "_log"),
            ):
                result = self.scanner._run(context, argv)

            self.assertTrue(result)
            self.assertEqual(scan.call_count, 2)
            self.assertEqual(enter.call_count, 3)
            state = json.loads(state_path.read_text(encoding="utf-8"))
            self.assertEqual(
                state["visited_halls"]["12345"]["scanned_at"],
                "2026-09-14T20:30:00+08:00",
            )

    def test_contribution_scan_reports_panel_open_failure(self) -> None:
        with (
            patch.object(self.scanner, "_open_contribution_panel"),
            patch.object(self.scanner, "_capture_ocr", return_value=(None, [])),
            patch.object(self.scanner, "_dump_ui_hierarchy", return_value=""),
            patch.object(
                self.scanner,
                "_save_contribution_open_failure",
            ) as save_failure,
            patch.object(self.scanner, "_log"),
        ):
            opened = self.scanner._scan_contribution(
                context=SimpleNamespace(),
                room_id="12345",
                room_name="测试厅",
                output_path=Path("unused.sqlite3"),
                records=[],
                processed_users=set(),
                delay=0,
                max_pages=1,
                max_users=30,
                include_top3=True,
                unknown_gender_as_male=False,
            )

        self.assertFalse(opened)
        save_failure.assert_called_once_with(None, "", "12345")

    def test_open_contribution_does_not_click_default_filters(self) -> None:
        controller = FakeController()
        self.scanner.controller = controller
        items = [ocr("房间贡献榜", (330, 55, 150, 35))]

        with patch.object(
            self.scanner,
            "_capture_ocr",
            return_value=(None, items),
        ):
            self.scanner._open_contribution_panel(SimpleNamespace(), delay=0.1)

        self.assertEqual(controller.clicks, [])

    def test_open_contribution_does_not_dump_hierarchy_on_room_page(self) -> None:
        controller = FakeController()
        self.scanner.controller = controller
        room_items = [
            ocr("公告", (48, 141, 55, 25)),
            ocr("聊聊天", (33, 1201, 79, 33)),
        ]
        contribution_items = [ocr("房间贡献榜", (330, 55, 150, 35))]

        with (
            patch.object(
                self.scanner,
                "_capture_ocr",
                side_effect=[(None, room_items), (None, contribution_items)],
            ),
            patch.object(self.scanner, "_dump_ui_hierarchy") as dump_hierarchy,
            patch("voice_hall.time.sleep"),
        ):
            self.scanner._open_contribution_panel(SimpleNamespace(), delay=0.1)

        self.assertEqual(controller.clicks, [(594, 83)])
        dump_hierarchy.assert_not_called()


if __name__ == "__main__":
    unittest.main()
