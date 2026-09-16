import ctypes
import json
import os
import re
import time
import traceback
import xml.etree.ElementTree as ET
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

import numpy as np
import cv2
from maa.agent.agent_server import AgentServer
from maa.context import Context
from maa.custom_action import CustomAction
from maa.define import (
    MaaBool,
    MaaControllerHandle,
    MaaCtrlId,
    MaaStringBufferHandle,
)
from maa.library import Library
from maa.pipeline import JOCR, JRecognitionType

from voice_hall_storage import VoiceHallDatabase
from cloud_sync import CloudSyncClient, CloudSyncConfig


PROJECT_ROOT = Path(__file__).resolve().parents[1]
DEFAULT_DATABASE = PROJECT_ROOT / "data" / "voice_hall.sqlite3"
DEFAULT_STATE = PROJECT_ROOT / "data" / "voice_hall_scan_state.json"
DEFAULT_DEBUG_LOG = PROJECT_ROOT / "data" / "voice_hall_agent.log"
DEFAULT_OPEN_FAILURE_DIR = PROJECT_ROOT / "debug" / "on_error"
CHINA_TZ = timezone(timedelta(hours=8))

OCR_NODE = "OCRFull"
SKIP_SCANNED_CUSTOM_ACTION = "scan_voice_hall_contributions_skip_scanned"
CROWN_BUTTON = (594, 83)
FIRST_HALL_WARMUP_SECONDS = 3.0
CONTRIBUTION_REENTRY_WARMUP_SECONDS = 1.5
PROFILE_OPEN_MAX_ATTEMPTS = 5
PROFILE_OPEN_RETRY_SECONDS = 0.8
PROFILE_DETAIL_RETRY_ATTEMPTS = 2
PROFILE_DETAIL_RETRY_SECONDS = 0.8
CONTRIBUTION_HEADER_Y_RANGE = (35, 120)
TOP3_TARGETS = (
    (1, (362, 335)),
    (2, (132, 335)),
    (3, (592, 335)),
)
LEADERBOARD_AVATAR_X = 130
RANK_LABEL_MAX_X = 80
LEADERBOARD_START_Y = 330
UI_DUMP_PATH_PREFIX = "/sdcard/maa_voice_hall"
UI_DUMP_COMMAND = "timeout -k 1 3 uiautomator dump --compressed"
UI_DUMP_CONTROLLER_TIMEOUT_MS = 5000

HALL_ID_RE = re.compile(r"(?<!\d)(\d{4,8})(?!\d)")
PROFILE_ID_RE = re.compile(r"(?:I\s*D|ID|电)\s*[:：]?\s*(\d{4,12})", re.IGNORECASE)
PROFILE_IP_RE = re.compile(r"I\s*P(?:属地)?\s*[:：]\s*(\S+)", re.IGNORECASE)
ROOM_TITLE_RE = re.compile(r"^[^\n]{2,30}$")
HALL_CATEGORY_TEXTS = ("女神", "男神", "点唱", "派单", "游戏", "聊天")
HALL_LIST_NAV_TEXTS = ("聊天室", "娱乐", "消息", "我的")
HALL_CATEGORY_X_RANGES = {
    "女神": (10, 105),
    "男神": (115, 225),
    "点唱": (225, 340),
    "派单": (335, 455),
    "游戏": (450, 575),
    "聊天": (560, 710),
}
HALL_CATEGORY_Y_RANGE = (35, 115)
HALL_NAV_MIN_Y = 1160
HALL_CARD_X_RANGE = (180, 520)
HALL_CARD_Y_RANGE = (150, 1180)
PROFILE_ID_X_RANGE = (35, 175)
PROFILE_ID_Y_RANGE = (590, 690)
PROFILE_WEALTH_X_RANGE = (35, 125)
PROFILE_CHARM_X_RANGE = (126, 230)
PROFILE_LEVEL_Y_RANGE = (660, 735)
PROFILE_LEVEL_Y_OFFSET = (30, 100)
# The badges themselves extend much farther to the left than their digits.
# Keeping retry OCR inside these narrow ranges avoids reading the crystal,
# crown and heart decorations as leading 1/2/O characters.
PROFILE_WEALTH_DIGIT_X_RANGE = (68, 124)
PROFILE_CHARM_DIGIT_X_RANGE = (166, 214)
PROFILE_LEVEL_DIGIT_Y_OFFSET = (25, 78)
PROFILE_IP_X_RANGE = (480, 680)
PROFILE_IP_Y_RANGE = (560, 700)
PROFILE_NAME_X_RANGE = (30, 400)
PROFILE_NAME_Y_OFFSET = (-100, -20)
PROFILE_FRIEND_SCROLL = (360, 1050, 360, 450, 600)
PROFILE_FRIEND_MAX_SCROLLS = 10
CONTRIBUTION_SCROLL_X = 650
CONTRIBUTION_SCROLL_START_Y = 1040
CONTRIBUTION_SCROLL_END_Y = 650
UNOPENABLE_PROFILE_NAMES = {"神秘人"}


class _ScanStopped(Exception):
    pass


def _extract_hall_id(text: str) -> str | None:
    normalized = text.translate(str.maketrans("０１２３４５６７８９", "0123456789"))
    match = HALL_ID_RE.search(normalized)
    return match.group(1) if match else None


def _parse_android_bounds(value: str) -> tuple[int, int, int, int] | None:
    match = re.fullmatch(r"\[(\d+),(\d+)\]\[(\d+),(\d+)\]", value)
    if not match:
        return None
    left, top, right, bottom = (int(part) for part in match.groups())
    if right <= left or bottom <= top:
        return None
    return left, top, right - left, bottom - top


def _parse_android_hierarchy(hierarchy: str) -> ET.Element | None:
    xml_start = hierarchy.find("<?xml")
    if xml_start < 0:
        return None
    try:
        return ET.fromstring(hierarchy[xml_start:])
    except (ET.ParseError, UnicodeError):
        return None


def _build_ui_dump_command(token: str) -> tuple[str, str]:
    dump_path = f"{UI_DUMP_PATH_PREFIX}_{token}.xml"
    marker = f"__MAA_UI_DUMP_{token}__"
    command = (
        f"{UI_DUMP_COMMAND} {dump_path} >/dev/null; "
        'dump_status="$?"; '
        f'echo "{marker}:$dump_status"; '
        f'if [ "$dump_status" -eq 0 ]; then cat {dump_path}; fi; '
        f"rm -f {dump_path}"
    )
    return command, marker


def _find_profile_copy_target(
    hierarchy: str,
) -> tuple[str, tuple[int, int]] | None:
    root = _parse_android_hierarchy(hierarchy)
    if root is None:
        return None

    user_ids: dict[str, str] = {}
    copy_boxes: dict[str, tuple[int, int, int, int]] = {}
    for node in root.iter("node"):
        resource_id = node.attrib.get("resource-id", "")
        if resource_id.endswith(":id/tv_nice_num"):
            value = node.attrib.get("text", "").strip()
            if re.fullmatch(r"\d{4,12}", value):
                user_ids["nice"] = value
        elif resource_id.endswith(":id/tv_user_code"):
            value = node.attrib.get("text", "").strip()
            if re.fullmatch(r"\d{4,12}", value):
                user_ids["ordinary"] = value
        elif resource_id.endswith(":id/iv_copy"):
            box = _parse_android_bounds(node.attrib.get("bounds", ""))
            if box:
                copy_boxes["nice"] = box
        elif resource_id.endswith(":id/ll_copy"):
            box = _parse_android_bounds(node.attrib.get("bounds", ""))
            if box:
                copy_boxes["ordinary"] = box

    # The app renders vanity and ordinary IDs with different controls. Pair
    # each ID with its own copy target so unrelated profile numbers can never
    # be selected accidentally.
    for kind in ("nice", "ordinary"):
        user_id = user_ids.get(kind)
        copy_box = copy_boxes.get(kind)
        if user_id and copy_box:
            return user_id, _center(copy_box)
    return None


def _find_profile_resource(
    hierarchy: str,
    resource_suffix: str,
) -> tuple[str, tuple[int, int, int, int] | None] | None:
    root = _parse_android_hierarchy(hierarchy)
    if root is None:
        return None

    for node in root.iter("node"):
        if node.attrib.get("resource-id", "").endswith(resource_suffix):
            return (
                node.attrib.get("text", "").strip(),
                _parse_android_bounds(node.attrib.get("bounds", "")),
            )
    return None


def _find_profile_gender_box(
    hierarchy: str,
) -> tuple[int, int, int, int] | None:
    root = _parse_android_hierarchy(hierarchy)
    if root is None:
        return None

    copy_boxes: dict[str, tuple[int, int, int, int]] = {}
    age_box = None
    for node in root.iter("node"):
        resource_id = node.attrib.get("resource-id", "")
        box = _parse_android_bounds(node.attrib.get("bounds", ""))
        if not box:
            continue
        if resource_id.endswith(":id/iv_gender"):
            return box
        if resource_id.endswith(":id/ll_copy"):
            copy_boxes["ordinary"] = box
        elif resource_id.endswith(":id/iv_copy"):
            copy_boxes["nice"] = box
        elif resource_id.endswith(":id/tv_age"):
            age_box = box

    if age_box is None:
        return None
    copy_box = copy_boxes.get("ordinary") or copy_boxes.get("nice")
    if copy_box is None:
        return None

    copy_right = copy_box[0] + copy_box[2]
    age_left = age_box[0]
    gap_width = age_left - copy_right
    if not 18 <= gap_width <= 120:
        return None

    top = max(0, min(copy_box[1], age_box[1]) - 4)
    bottom = max(copy_box[1] + copy_box[3], age_box[1] + age_box[3]) + 4
    return copy_right, top, gap_width, bottom - top


def _classify_gender_color_points(roi: Any) -> str | None:
    """Classify an icon from several spatial color samples.

    The icon can contain anti-aliased edges and a transparent/background area,
    so a single pixel or the whole-ROI average is easy to skew. Each cell in
    a 3x3 grid contributes at most one vote based on its median colored pixel.
    """
    pixels = np.asarray(roi)
    if pixels.ndim != 3 or pixels.shape[2] < 3:
        return None

    height, width = pixels.shape[:2]
    if height < 3 or width < 3:
        return None

    male_votes = 0
    female_votes = 0
    for row in range(3):
        y0 = row * height // 3
        y1 = (row + 1) * height // 3
        for column in range(3):
            x0 = column * width // 3
            x1 = (column + 1) * width // 3
            colors = pixels[y0:y1, x0:x1, :3].reshape(-1, 3).astype(np.int16)
            if colors.size == 0:
                continue
            chroma = colors.max(axis=1) - colors.min(axis=1)
            colored = colors[(colors.max(axis=1) >= 140) & (chroma >= 15)]
            if colored.size == 0:
                continue

            median = np.median(colored, axis=0)
            green_delta = median[1] - (median[0] + median[2]) / 2
            if green_delta >= 8:
                male_votes += 1
            elif green_delta <= -8:
                female_votes += 1

    total_votes = male_votes + female_votes
    if total_votes < 2:
        return None
    if male_votes > female_votes and male_votes / total_votes >= 0.6:
        return "男"
    if female_votes > male_votes and female_votes / total_votes >= 0.6:
        return "女"
    return None


def _find_contribution_targets(
    hierarchy: str,
) -> tuple[list[tuple[int, tuple[int, int]]], list[tuple[int, int]]]:
    root = _parse_android_hierarchy(hierarchy)
    if root is None:
        return [], []

    top3_targets: list[tuple[int, tuple[int, int]]] = []
    rank_nodes: list[tuple[int, tuple[int, int, int, int]]] = []
    avatar_boxes: list[tuple[int, int, int, int]] = []

    for node in root.iter("node"):
        resource_id = node.attrib.get("resource-id", "")
        box = _parse_android_bounds(node.attrib.get("bounds", ""))
        if not box:
            continue

        top_match = re.search(r":id/iv_avatar_rank_([123])$", resource_id)
        if top_match:
            top3_targets.append((int(top_match.group(1)), _center(box)))
            continue

        if resource_id.endswith(":id/tv_rank"):
            text = node.attrib.get("text", "").strip()
            if re.fullmatch(r"\d{1,3}", text) and int(text) >= 4:
                rank_nodes.append((int(text), box))
        elif resource_id.endswith(":id/iv_avatar"):
            avatar_boxes.append(box)

    rank_rows: list[tuple[int, int]] = []
    used_avatars: set[int] = set()
    for rank, rank_box in sorted(rank_nodes, key=lambda value: value[1][1]):
        rank_y = _center(rank_box)[1]
        candidates = [
            (abs(_center(box)[1] - rank_y), index, box)
            for index, box in enumerate(avatar_boxes)
            if index not in used_avatars and abs(_center(box)[1] - rank_y) <= 60
        ]
        if not candidates:
            continue
        _, avatar_index, avatar_box = min(candidates, key=lambda value: value[0])
        used_avatars.add(avatar_index)
        rank_rows.append((rank, _center(avatar_box)[1]))

    return (
        sorted(top3_targets, key=lambda value: value[0]),
        sorted(rank_rows, key=lambda value: value[1]),
    )


def _find_unopenable_contribution_ranks(hierarchy: str) -> set[int]:
    root = _parse_android_hierarchy(hierarchy)
    if root is None:
        return set()

    rank_nodes: list[tuple[int, tuple[int, int, int, int]]] = []
    unopenable_name_ys: list[int] = []
    for node in root.iter("node"):
        resource_id = node.attrib.get("resource-id", "")
        box = _parse_android_bounds(node.attrib.get("bounds", ""))
        if not box:
            continue
        if resource_id.endswith(":id/tv_rank"):
            text = node.attrib.get("text", "").strip()
            if re.fullmatch(r"\d{1,3}", text) and int(text) >= 4:
                rank_nodes.append((int(text), box))
        elif (
            resource_id.endswith(":id/tv_nickname")
            and node.attrib.get("text", "").strip() in UNOPENABLE_PROFILE_NAMES
        ):
            unopenable_name_ys.append(_center(box)[1])

    return {
        rank
        for rank, box in rank_nodes
        if any(abs(_center(box)[1] - name_y) <= 60 for name_y in unopenable_name_ys)
    }


def _is_contribution_hierarchy(hierarchy: str) -> bool:
    root = _parse_android_hierarchy(hierarchy)
    if root is None:
        return False

    has_rank_list = False
    contribution_tab_selected = False
    for node in root.iter("node"):
        resource_id = node.attrib.get("resource-id", "")
        if resource_id.endswith(":id/rv_rank_list"):
            has_rank_list = True
        if (
            node.attrib.get("selected") == "true"
            and "房间贡献榜" in node.attrib.get("text", "")
        ):
            contribution_tab_selected = True
    return has_rank_list and contribution_tab_selected


def _ensure_shell_api_types() -> None:
    framework = Library.framework()
    framework.MaaControllerPostShell.restype = MaaCtrlId
    framework.MaaControllerPostShell.argtypes = [
        MaaControllerHandle,
        ctypes.c_char_p,
        ctypes.c_int64,
    ]
    framework.MaaControllerGetShellOutput.restype = MaaBool
    framework.MaaControllerGetShellOutput.argtypes = [
        MaaControllerHandle,
        MaaStringBufferHandle,
    ]


def _load_json(path: Path, default: dict[str, Any]) -> dict[str, Any]:
    try:
        with path.open("r", encoding="utf-8") as file:
            value = json.load(file)
    except (FileNotFoundError, json.JSONDecodeError, OSError):
        return default
    return value if isinstance(value, dict) else default


def _write_json_atomic(path: Path, value: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temp_path = path.with_suffix(path.suffix + ".tmp")
    with temp_path.open("w", encoding="utf-8") as file:
        json.dump(value, file, ensure_ascii=False, indent=2)
    os.replace(temp_path, path)


def _result_text(result: Any) -> str:
    text = getattr(result, "text", "")
    return str(text).strip() if text is not None else ""


def _box(result_or_box: Any) -> tuple[int, int, int, int]:
    box = getattr(result_or_box, "box", result_or_box)
    if isinstance(box, (list, tuple)) and len(box) == 4:
        return tuple(int(value) for value in box)
    return (int(box.x), int(box.y), int(box.w), int(box.h))


def _center(box: tuple[int, int, int, int]) -> tuple[int, int]:
    x, y, width, height = box
    return x + width // 2, y + height // 2


def _resolve_path(value: Any, default: Path) -> Path:
    path = Path(str(value)) if value else default
    if not path.is_absolute():
        path = PROJECT_ROOT / path
    return path.resolve()


def _was_scanned_on(value: Any, scan_day: str) -> bool:
    if not isinstance(value, dict):
        return False
    return str(value.get("scanned_at", ""))[:10] == scan_day


def _should_save_level_samples(project_root: Path = PROJECT_ROOT) -> bool:
    """Keep unrecognized-level screenshots in source runs, not releases."""
    is_packaged_release = (project_root / "interface.json").is_file() and (
        project_root / "maafw"
    ).is_dir()
    return not is_packaged_release


class ContributionScanner(CustomAction):
    controller: Any = None
    _last_profile_hierarchy = ""
    cloud_sync: CloudSyncClient | None = None

    @staticmethod
    def _check_stopping(context: Context) -> None:
        try:
            tasker = getattr(context, "tasker", None)
            if tasker is not None and bool(tasker.stopping):
                raise _ScanStopped
        except _ScanStopped:
            raise
        except (AttributeError, ctypes.ArgumentError, RuntimeError, OSError, TypeError):
            return

    def _sleep(self, context: Context, duration: float) -> None:
        remaining = max(0.0, duration)
        while remaining > 0:
            self._check_stopping(context)
            interval = min(0.1, remaining)
            time.sleep(interval)
            remaining -= interval
        self._check_stopping(context)

    def run(
        self,
        context: Context,
        argv: CustomAction.RunArg,
    ) -> bool:
        try:
            return self._run(context, argv)
        except _ScanStopped:
            self._log("收到停止请求，扫描已停止")
            return False
        except Exception as exc:  # noqa: BLE001
            self._log("未处理异常", repr(exc), traceback.format_exc())
            return False

    def _run(
        self,
        context: Context,
        argv: CustomAction.RunArg,
    ) -> bool:
        params = json.loads(argv.custom_action_param or "{}") or {}
        output_path = _resolve_path(
            params.get("database") or params.get("output_database"),
            DEFAULT_DATABASE,
        )
        state_path = _resolve_path(params.get("state_file"), DEFAULT_STATE)
        delay = float(params.get("action_delay", 0.8))
        max_halls = max(0, int(params.get("max_halls", 0)))
        max_hall_pages = max(1, int(params.get("max_hall_pages", 100)))
        max_contribution_pages = max(1, int(params.get("max_contribution_pages", 100)))
        # Keep the documented default as the final safety boundary even when
        # a third-party runner invokes this custom action without UI options.
        max_users_per_hall = max(1, int(params.get("max_users_per_hall", 100)))
        include_top3 = bool(params.get("include_top3", True))
        unknown_gender_as_male = bool(params.get("unknown_gender_as_male", False))
        single_hall = bool(params.get("single_hall", False))
        skip_scanned_today = bool(params.get("skip_scanned_today", False)) or (
            getattr(argv, "custom_action_name", "") == SKIP_SCANNED_CUSTOM_ACTION
        )

        self.cloud_sync = None
        cloud_config = CloudSyncConfig.from_params(params)
        if cloud_config.enabled:
            try:
                self.cloud_sync = CloudSyncClient(cloud_config)
                self._log(f"已启用云端上传：{cloud_config.url}")
            except (TypeError, ValueError) as exc:
                self._log(f"云端上传配置无效，已禁用：{exc}")

        database = VoiceHallDatabase(output_path)
        database.initialize()
        records = database.load_contributions()
        processed_users = {
            (str(item.get("room_id")), str(item.get("user_id"))) for item in records
        }

        self.controller = context.tasker.controller
        controller = self.controller
        scan_day = self._now()[:10]
        self._log(
            "开始扫描，当前设备分辨率=",
            getattr(controller, "resolution", "unknown"),
            f"扫描日期={scan_day}",
            f"每厅扫描上限={max_users_per_hall}",
            f"跳过今日已扫描厅={skip_scanned_today}",
        )
        self._check_stopping(context)

        if single_hall:
            room_id = _extract_hall_id(str(params.get("room_id", "")))
            if room_id is None:
                self._log("单厅调试缺少有效厅 ID，请填写 4 到 8 位数字")
                return False
            room_name = str(params.get("room_name") or room_id).strip() or room_id
            _, items = self._capture_ocr(context)
            if self._is_more_menu(items):
                self.controller.post_click_key(4).wait()
                self._sleep(context, delay)
                _, items = self._capture_ocr(context)
            if not (
                self._is_room_page(items)
                or self._is_contribution_panel(items)
            ):
                self._log(
                    f"单厅调试未识别到厅 {room_id} 的房间页，"
                    "请先手动进入目标厅再运行任务",
                )
                return False

            self._log(f"开始单厅调试：厅 {room_name} ({room_id})")
            opened = self._scan_contribution(
                context=context,
                room_id=room_id,
                room_name=room_name,
                output_path=output_path,
                records=records,
                processed_users=processed_users,
                delay=delay,
                max_pages=max_contribution_pages,
                max_users=max_users_per_hall,
                include_top3=include_top3,
                unknown_gender_as_male=unknown_gender_as_male,
            )
            if opened:
                self._log(f"单厅调试完成：厅 {room_id}，输出：{output_path}")
            return opened

        state = _load_json(state_path, {"visited_halls": {}})
        visited_halls = state.setdefault("visited_halls", {})
        if not isinstance(visited_halls, dict):
            visited_halls = {}
            state["visited_halls"] = visited_halls

        if not self._return_to_hall_list(context, max_attempts=5):
            self._log("没有回到厅列表页，任务结束")
            return False

        new_hall_count = 0
        page_signatures: set[tuple[str, ...]] = set()
        scanned_this_run: set[str] = set()
        needs_first_hall_warmup = True

        for _ in range(max_hall_pages):
            self._check_stopping(context)
            if max_halls and new_hall_count >= max_halls:
                break

            image, items = self._capture_ocr(context)
            if not self._is_hall_list(items):
                if not self._return_to_hall_list(context, max_attempts=3):
                    break
                image, items = self._capture_ocr(context)

            hall_candidates = self._find_hall_candidates(items)
            signature = tuple(str(candidate["hall_id"]) for candidate in hall_candidates)
            if not signature:
                self._log("当前页没有识别到厅卡片")
                self._scroll_hall_list(context, delay)
                continue
            if signature in page_signatures:
                self._log("厅列表已无新内容，停止翻页")
                break
            page_signatures.add(signature)

            for candidate in hall_candidates:
                self._check_stopping(context)
                if max_halls and new_hall_count >= max_halls:
                    break

                hall_id = str(candidate["hall_id"])
                if hall_id in scanned_this_run:
                    continue
                if skip_scanned_today and _was_scanned_on(
                    visited_halls.get(hall_id), scan_day
                ):
                    self._log(f"厅 {hall_id} 今天已经扫描，按设置跳过")
                    continue

                hall_name = str(candidate.get("name") or hall_id)
                self._log(f"进入厅 {hall_name} ({hall_id})")

                if not self._open_hall(context, int(candidate["card_y"]), delay):
                    self._log(f"厅 {hall_id} 未进入成功，跳过")
                    self._return_to_hall_list(context, max_attempts=2)
                    continue

                room_image, room_items = self._capture_ocr(context)
                if not self._is_room_page(room_items):
                    self._log(f"厅 {hall_id} 页面状态异常，跳过")
                    self._return_to_hall_list(context, max_attempts=2)
                    continue

                if needs_first_hall_warmup:
                    self._log(
                        f"厅 {hall_id} 是本次任务首个厅，等待房间页面完成初始化"
                    )
                    self._sleep(context, FIRST_HALL_WARMUP_SECONDS)
                    needs_first_hall_warmup = False

                opened = self._scan_contribution(
                    context=context,
                    room_id=hall_id,
                    room_name=hall_name,
                    output_path=output_path,
                    records=records,
                    processed_users=processed_users,
                    delay=delay,
                    max_pages=max_contribution_pages,
                    max_users=max_users_per_hall,
                    include_top3=include_top3,
                    unknown_gender_as_male=unknown_gender_as_male,
                )

                if not opened:
                    self._log(
                        f"厅 {hall_id} 首次打开贡献榜失败，退出并重新进入后重试一次"
                    )
                    returned = self._return_to_hall_list(context, max_attempts=4)
                    reentered = returned and self._open_hall(
                        context,
                        int(candidate["card_y"]),
                        delay,
                    )
                    if reentered:
                        self._sleep(context, CONTRIBUTION_REENTRY_WARMUP_SECONDS)
                        opened = self._scan_contribution(
                            context=context,
                            room_id=hall_id,
                            room_name=hall_name,
                            output_path=output_path,
                            records=records,
                            processed_users=processed_users,
                            delay=delay,
                            max_pages=max_contribution_pages,
                            max_users=max_users_per_hall,
                            include_top3=include_top3,
                            unknown_gender_as_male=unknown_gender_as_male,
                        )
                    else:
                        self._log(f"厅 {hall_id} 未能重新进入，取消本次重试")

                if opened:
                    visited_halls[hall_id] = {
                        "name": hall_name,
                        "scanned_at": self._now(),
                    }
                    _write_json_atomic(state_path, state)
                    scanned_this_run.add(hall_id)
                    new_hall_count += 1
                else:
                    self._log(
                        f"厅 {hall_id} 重试后仍未完成贡献榜扫描，不记入已扫描状态"
                    )

                if not self._return_to_hall_list(context, max_attempts=4):
                    self._log("扫描后没有回到厅列表页，停止任务")
                    return False

            if max_halls and new_hall_count >= max_halls:
                break

            before_signature = signature
            self._scroll_hall_list(context, delay)
            _, after_items = self._capture_ocr(context)
            after_signature = tuple(
                str(candidate["hall_id"])
                for candidate in self._find_hall_candidates(after_items)
            )
            if not after_signature or after_signature == before_signature:
                self._log("厅列表到底或页面未变化，停止遍历")
                break

        self._log(f"扫描完成，本轮到访 {new_hall_count} 个厅，输出：{output_path}")
        return True

    def _scan_contribution(
        self,
        context: Context,
        room_id: str,
        room_name: str,
        output_path: Path,
        records: list[dict[str, Any]],
        processed_users: set[tuple[str, str]],
        delay: float,
        max_pages: int,
        max_users: int,
        include_top3: bool,
        unknown_gender_as_male: bool,
    ) -> bool:
        self._log(f"厅 {room_id} 正在打开贡献榜")
        self._open_contribution_panel(context, delay)
        seen_ranks: set[int] = set()
        page_fingerprints: set[tuple[int, ...]] = set()

        for page_index in range(max_pages):
            self._check_stopping(context)
            self._log(f"厅 {room_id} 正在读取贡献榜第 {page_index + 1} 页")
            image, items = self._capture_ocr(context)
            hierarchy = self._dump_ui_hierarchy()
            hierarchy_top3, hierarchy_rows = _find_contribution_targets(hierarchy)
            if not (
                self._is_contribution_panel(items)
                or _is_contribution_hierarchy(hierarchy)
            ):
                self._log(f"厅 {room_id} 未打开贡献榜，跳过")
                self._save_contribution_open_failure(image, hierarchy, room_id)
                return False
            if not hierarchy_top3 and not hierarchy_rows:
                self._log(
                    f"厅 {room_id} 第 {page_index + 1} 页结构读取失败，使用 OCR 兜底"
                )

            if page_index == 0 and include_top3:
                top3_targets = hierarchy_top3 or list(TOP3_TARGETS)
                for rank, point in top3_targets:
                    self._check_stopping(context)
                    if max_users and rank > max_users:
                        self._log(f"厅 {room_id} 已达到贡献榜前 {max_users} 名的扫描上限")
                        return True
                    self._log(f"厅 {room_id} 正在读取排名 {rank} 的用户")
                    self._record_user(
                        context=context,
                        room_id=room_id,
                        room_name=room_name,
                        rank=rank,
                        click_point=point,
                        output_path=output_path,
                        records=records,
                        processed_users=processed_users,
                        delay=delay,
                        unknown_gender_as_male=unknown_gender_as_male,
                    )
                    if max_users and rank >= max_users:
                        self._log(f"厅 {room_id} 已完成贡献榜前 {max_users} 名的扫描")
                        return True

            rank_rows = hierarchy_rows or self._find_rank_rows(items)
            unopenable_ranks = _find_unopenable_contribution_ranks(hierarchy)
            unopenable_ranks.update(
                self._find_unopenable_ocr_ranks(items, rank_rows)
            )
            fingerprint = tuple(rank for rank, _ in rank_rows)
            if not fingerprint or fingerprint in page_fingerprints:
                self._log(f"厅 {room_id} 贡献榜已到底")
                return True
            page_fingerprints.add(fingerprint)

            for rank, row_y in rank_rows:
                self._check_stopping(context)
                if rank in seen_ranks:
                    continue
                seen_ranks.add(rank)
                if max_users and rank > max_users:
                    self._log(f"厅 {room_id} 已达到贡献榜前 {max_users} 名的扫描上限")
                    return True
                if rank in unopenable_ranks:
                    self._log(f"排名 {rank} 为神秘人，资料页不可访问，跳过")
                    if max_users and rank >= max_users:
                        self._log(f"厅 {room_id} 已完成贡献榜前 {max_users} 名的扫描")
                        return True
                    continue
                self._log(f"厅 {room_id} 正在读取排名 {rank} 的用户")
                self._record_user(
                    context=context,
                    room_id=room_id,
                    room_name=room_name,
                    rank=rank,
                    click_point=(LEADERBOARD_AVATAR_X, row_y),
                    output_path=output_path,
                    records=records,
                    processed_users=processed_users,
                    delay=delay,
                    unknown_gender_as_male=unknown_gender_as_male,
                )
                if max_users and rank >= max_users:
                    self._log(f"厅 {room_id} 已完成贡献榜前 {max_users} 名的扫描")
                    return True

            next_items = self._scroll_contribution(
                context,
                before_ranks=fingerprint,
                delay=delay,
            )
            if next_items is None:
                self._log(f"厅 {room_id} 贡献榜已到底或滑动未生效")
                return True
        return True

    def _scroll_contribution(
        self,
        context: Context,
        before_ranks: tuple[int, ...],
        delay: float,
    ) -> list[Any] | None:
        for _ in range(2):
            self._check_stopping(context)
            self.controller.post_swipe(
                CONTRIBUTION_SCROLL_X,
                CONTRIBUTION_SCROLL_START_Y,
                CONTRIBUTION_SCROLL_X,
                CONTRIBUTION_SCROLL_END_Y,
                700,
            ).wait()
            self._sleep(context, delay)
            _, items = self._capture_ocr(context)
            hierarchy = self._dump_ui_hierarchy()
            _, hierarchy_rows = _find_contribution_targets(hierarchy)
            if not (
                self._is_contribution_panel(items)
                or _is_contribution_hierarchy(hierarchy)
            ):
                continue
            rank_rows = hierarchy_rows or self._find_rank_rows(items)
            after_ranks = tuple(rank for rank, _ in rank_rows)
            if after_ranks and after_ranks != before_ranks:
                return items
        return None

    def _record_user(
        self,
        context: Context,
        room_id: str,
        room_name: str,
        rank: int,
        click_point: tuple[int, int],
        output_path: Path,
        records: list[dict[str, Any]],
        processed_users: set[tuple[str, str]],
        delay: float,
        unknown_gender_as_male: bool,
    ) -> bool:
        self._check_stopping(context)
        self.controller.post_click(click_point[0], click_point[1]).wait()
        self._sleep(context, max(delay, PROFILE_OPEN_RETRY_SECONDS))

        self._last_profile_hierarchy = ""
        profile_image, profile_items = self._wait_for_profile_page(context)
        user_id = self._copy_profile_id(context)

        # Some leaderboard users do not expose a profile even though Android
        # reports their avatar as clickable. Wait for slow transitions before
        # deciding that the click did not open anything. Do not press Back in
        # the confirmed leaderboard state: it would close the contribution
        # panel and make the next scroll look like the end.
        if (
            not user_id
            and (
                self._is_contribution_panel(profile_items)
                or _is_contribution_hierarchy(self._last_profile_hierarchy)
            )
        ):
            self._log(f"排名 {rank} 的头像点击后仍在贡献榜，跳过")
            return False

        if not profile_items:
            profile_image, profile_items = self._capture_ocr(context)

        if not user_id:
            user_id = self._extract_profile_id(profile_items)
            if user_id:
                self._log(f"排名 {rank} 的 ID 使用 OCR 兜底：{user_id}")
        if not user_id:
            self._log(f"排名 {rank} 的用户详情未获取到 ID，跳过")
            self.controller.post_click_key(4).wait()
            self._sleep(context, delay * 0.6)
            return True

        key = (room_id, user_id)
        ocr_user_id = self._extract_profile_id(profile_items)
        if (
            key in processed_users
            and ocr_user_id
            and ocr_user_id != user_id
            and (room_id, ocr_user_id) not in processed_users
        ):
            self._log(
                f"排名 {rank} 的界面结构 ID={user_id} 已记录，"
                f"当前页 OCR ID={ocr_user_id}，改用 OCR 结果"
            )
            user_id = ocr_user_id
            key = (room_id, user_id)
        (
            profile_image,
            profile_items,
            profile_hierarchy,
            gender,
            gender_source,
            username,
            ip,
            wealth_level,
            charm_level,
        ) = self._read_profile_details_with_retry(
            context,
            profile_image,
            profile_items,
            self._last_profile_hierarchy,
            unknown_gender_as_male,
        )
        self._last_profile_hierarchy = profile_hierarchy
        close_friend_count = self._scan_close_friend_count(
            context,
            profile_items,
            profile_hierarchy,
            delay,
        )
        if close_friend_count is None:
            self._log(f"排名 {rank} 的挚友数量尚未识别，等待页面稳定后重试")
            self._sleep(context, PROFILE_DETAIL_RETRY_SECONDS)
            retry_image, retry_items = self._capture_ocr(context)
            retry_hierarchy = self._dump_ui_hierarchy()
            if retry_items or retry_hierarchy:
                close_friend_count = self._scan_close_friend_count(
                    context,
                    retry_items,
                    retry_hierarchy,
                    delay,
                )
        self._check_stopping(context)
        wealth_level, charm_level = self._retry_missing_profile_levels(
            context,
            profile_image,
            profile_items,
            wealth_level,
            charm_level,
        )
        self.controller.post_click_key(4).wait()
        self._sleep(context, delay * 0.6)
        scanned_at = self._now()
        record = {
            "room_id": room_id,
            "room_name": room_name,
            "user_id": user_id,
            "username": username,
            "gender": gender,
            "gender_source": gender_source,
            "ip": ip,
            "close_friend_count": close_friend_count,
            "wealth_level": wealth_level,
            "charm_level": charm_level,
            "rank": rank,
            "scanned_at": scanned_at,
        }
        missing_level_fields = [
            field
            for field, value in (
                ("wealth_level", wealth_level),
                ("charm_level", charm_level),
            )
            if value is None
        ]
        level_sample_path = None
        if (
            missing_level_fields
            and profile_image is not None
            and _should_save_level_samples()
        ):
            level_sample_path = self._save_level_sample(
                profile_image,
                output_path,
                record,
                missing_level_fields,
            )
        record["level_sample_path"] = level_sample_path
        processed_users.add(key)
        updated = VoiceHallDatabase(output_path).upsert_contribution(record)
        if self.cloud_sync is not None:
            try:
                self.cloud_sync.upload(record)
                self._log(f"云端上传成功：厅={room_id} 用户={user_id}")
            except Exception as exc:  # noqa: BLE001
                # Local persistence is authoritative; a transient cloud outage
                # must not discard a record or fail the scan.
                self._log(f"云端上传失败，已保留本地记录：厅={room_id} 用户={user_id}，{exc}")
        self._upsert_daily_record(records, record)
        action = "覆盖更新" if updated else "记录成功"
        self._log(
            f"{action}：厅={room_name}({room_id}) 排名={rank} "
            f"用户={username}({user_id}) 性别={gender} IP={ip} "
            f"挚友={close_friend_count} 财富={wealth_level} 魅力={charm_level}"
        )
        return True

    def _wait_for_profile_page(
        self,
        context: Context,
    ) -> tuple[Any, list[Any]]:
        image = None
        items: list[Any] = []
        stable_candidate: tuple[Any, list[Any]] | None = None
        for attempt in range(PROFILE_OPEN_MAX_ATTEMPTS):
            self._check_stopping(context)
            image, items = self._capture_ocr(context)
            if self._is_profile_page(items):
                if stable_candidate is not None:
                    return image, items
                stable_candidate = (image, items)
            if attempt + 1 < PROFILE_OPEN_MAX_ATTEMPTS:
                self._sleep(context, PROFILE_OPEN_RETRY_SECONDS)
        return stable_candidate or (image, items)

    def _read_profile_details_with_retry(
        self,
        context: Context,
        image: Any,
        items: list[Any],
        hierarchy: str,
        unknown_gender_as_male: bool,
    ) -> tuple[
        Any,
        list[Any],
        str,
        str,
        str,
        str | None,
        str | None,
        int | None,
        int | None,
    ]:
        gender = "未知"
        gender_source = "unknown"
        username = None
        ip = None
        wealth_level = None
        charm_level = None
        hidden_levels: set[str] = set()

        for attempt in range(PROFILE_DETAIL_RETRY_ATTEMPTS + 1):
            if gender == "未知":
                gender, gender_source = self._extract_gender(
                    image,
                    items,
                    hierarchy,
                    unknown_gender_as_male=False,
                )
            if username is None:
                username = self._extract_profile_name(items, hierarchy)
            if ip is None:
                ip = self._extract_profile_ip(items, hierarchy)
            retry_wealth, retry_charm = self._extract_profile_levels(items, image)
            if wealth_level is None:
                wealth_level = retry_wealth
            if charm_level is None:
                charm_level = retry_charm
            hidden_levels.update(self._hidden_profile_levels(items, image))

            missing: list[str] = []
            if gender == "未知":
                missing.append("性别")
            if username is None:
                missing.append("用户名")
            if ip is None:
                missing.append("IP")
            if wealth_level is None and "wealth" not in hidden_levels:
                missing.append("财富等级")
            if charm_level is None and "charm" not in hidden_levels:
                missing.append("魅力等级")
            if not missing or attempt >= PROFILE_DETAIL_RETRY_ATTEMPTS:
                break

            self._log(
                "资料页字段尚未稳定，等待后重试：",
                ",".join(missing),
            )
            self._sleep(context, PROFILE_DETAIL_RETRY_SECONDS)
            retry_image, retry_items = self._capture_ocr(context)
            retry_hierarchy = self._dump_ui_hierarchy()
            if retry_items or retry_hierarchy:
                image = retry_image
                items = retry_items
                hierarchy = retry_hierarchy

        if gender == "未知" and unknown_gender_as_male:
            gender = "男"
            gender_source = "inferred:未识别到性别图标"
        return (
            image,
            items,
            hierarchy,
            gender,
            gender_source,
            username,
            ip,
            wealth_level,
            charm_level,
        )

    def _save_level_sample(
        self,
        image: Any,
        output_path: Path,
        record: dict[str, Any],
        missing_fields: list[str],
    ) -> str | None:
        try:
            pixels = np.asarray(image)
            if pixels.size == 0 or pixels.ndim not in (2, 3):
                return None
            if pixels.dtype != np.uint8:
                pixels = np.clip(pixels, 0, 255).astype(np.uint8)

            scanned_day = str(record.get("scanned_at", "unknown"))[:10]
            room_id = re.sub(r"[^0-9A-Za-z_-]", "_", str(record.get("room_id", "")))
            user_id = re.sub(r"[^0-9A-Za-z_-]", "_", str(record.get("user_id", "")))
            sample_dir = output_path.parent / "voice_hall_level_samples"
            sample_dir.mkdir(parents=True, exist_ok=True)
            screenshot_path = sample_dir / f"{scanned_day}_{room_id}_{user_id}.png"
            success, encoded = cv2.imencode(".png", pixels)
            if not success:
                return None
            encoded.tofile(screenshot_path)

            try:
                display_path = str(screenshot_path.resolve().relative_to(PROJECT_ROOT))
            except ValueError:
                display_path = str(screenshot_path.resolve())
            sample_record = {
                **record,
                "missing_fields": missing_fields,
                "screenshot": display_path,
            }
            VoiceHallDatabase(output_path).upsert_level_sample(sample_record)
            self._log(
                f"等级识别样本已保存：{display_path} "
                f"缺失={','.join(missing_fields)}"
            )
            return display_path
        except (OSError, TypeError, ValueError, cv2.error) as exc:
            self._log(f"等级识别样本保存失败：{exc}")
            return None

    def _save_contribution_open_failure(
        self,
        image: Any,
        hierarchy: str,
        room_id: str,
    ) -> None:
        try:
            DEFAULT_OPEN_FAILURE_DIR.mkdir(parents=True, exist_ok=True)
            timestamp = datetime.now(CHINA_TZ).strftime("%Y.%m.%d-%H.%M.%S.%f")[:-3]
            safe_room_id = re.sub(r"[^0-9A-Za-z_-]", "_", room_id)
            prefix = DEFAULT_OPEN_FAILURE_DIR / (
                f"{timestamp}_ContributionPanel_{safe_room_id}"
            )
            saved: list[str] = []

            pixels = np.asarray(image) if image is not None else np.asarray([])
            if pixels.size and pixels.ndim in (2, 3):
                if pixels.dtype != np.uint8:
                    pixels = np.clip(pixels, 0, 255).astype(np.uint8)
                success, encoded = cv2.imencode(".png", pixels)
                if success:
                    screenshot_path = prefix.with_suffix(".png")
                    encoded.tofile(screenshot_path)
                    saved.append(str(screenshot_path.relative_to(PROJECT_ROOT)))

            if hierarchy:
                hierarchy_path = prefix.with_suffix(".xml")
                hierarchy_path.write_text(hierarchy, encoding="utf-8")
                saved.append(str(hierarchy_path.relative_to(PROJECT_ROOT)))

            if saved:
                self._log("贡献榜打开失败现场已保存：", ", ".join(saved))
        except (OSError, TypeError, ValueError, cv2.error) as exc:
            self._log(f"贡献榜打开失败现场保存失败：{exc}")

    def _copy_profile_id(self, context: Context | None = None) -> str | None:
        if context is not None:
            self._check_stopping(context)
        hierarchy = self._dump_ui_hierarchy()
        if context is not None:
            self._check_stopping(context)
        if hierarchy.find("<?xml") >= 0:
            self._last_profile_hierarchy = hierarchy
        target = _find_profile_copy_target(hierarchy)
        if target:
            user_id, copy_point = target
            self.controller.post_click(*copy_point).wait()
            if context is None:
                time.sleep(0.15)
            else:
                self._sleep(context, 0.15)
            return user_id
        return None

    def _dump_ui_hierarchy(self) -> str:
        token = str(time.monotonic_ns())
        command, marker = _build_ui_dump_command(token)
        try:
            _ensure_shell_api_types()
            hierarchy = self.controller.post_shell(
                command,
                timeout=UI_DUMP_CONTROLLER_TIMEOUT_MS,
            ).get(wait=True)
        except (AttributeError, ctypes.ArgumentError, RuntimeError, OSError):
            return ""
        hierarchy = str(hierarchy) if hierarchy is not None else ""
        # MaaControllerGetShellOutput can retain the previous command's output
        # after a failed shell action. A per-call marker prevents stale profile
        # XML from being treated as the current user.
        if f"{marker}:0" not in hierarchy:
            return ""
        return hierarchy

    def _open_contribution_panel(self, context: Context, delay: float) -> None:
        for _ in range(3):
            self._check_stopping(context)
            _, items = self._capture_ocr(context)
            if self._is_contribution_panel(items):
                return
            if self._is_more_menu(items):
                self.controller.post_click_key(4).wait()
                self._sleep(context, delay)
                continue

            self.controller.post_click(CROWN_BUTTON[0], CROWN_BUTTON[1]).wait()
            self._sleep(context, delay)
            _, items = self._capture_ocr(context)
            if self._is_contribution_panel(items):
                return
            # Only inspect the accessibility hierarchy after clicking the
            # contribution entry. Dumping it on the animated room page can
            # keep UIAutomator waiting for an idle window.
            if _is_contribution_hierarchy(self._dump_ui_hierarchy()):
                return

    def _return_to_hall_list(self, context: Context, max_attempts: int) -> bool:
        for _ in range(max_attempts):
            self._check_stopping(context)
            _, items = self._capture_ocr(context)
            if self._is_hall_list(items):
                return True

            known_inner_page = (
                self._is_room_page(items)
                or self._is_contribution_panel(items)
                or self._is_more_menu(items)
                or self._is_profile_page(items)
            )
            if not known_inner_page:
                self._log(
                    "厅列表识别未命中，保持当前页重试：",
                    [_result_text(item) for item in items[:8]],
                )
                self._sleep(context, 0.8)
                continue

            self.controller.post_click_key(4).wait()
            self._sleep(context, 0.7)
        _, items = self._capture_ocr(context)
        return self._is_hall_list(items)

    def _open_hall(self, context: Context, card_y: int, delay: float) -> bool:
        self._check_stopping(context)
        self.controller.post_click(360, card_y).wait()
        for _ in range(3):
            self._sleep(context, delay + 0.4)
            _, items = self._capture_ocr(context)
            self._log(
                "进厅确认",
                [_result_text(item) for item in items[:8]],
            )
            if self._is_room_page(items):
                self._log("进厅成功，准备打开贡献榜")
                return True
        return False

    def _scroll_hall_list(self, context: Context, delay: float) -> None:
        self._check_stopping(context)
        self.controller.post_swipe(360, 1140, 360, 360, 750).wait()
        self._sleep(context, delay)

    def _capture_ocr(
        self,
        context: Context,
    ) -> tuple[Any, list[Any]]:
        self._check_stopping(context)
        image_job = self.controller.post_screencap()
        image_job.wait()
        self._check_stopping(context)
        image = image_job.get()
        detail = context.run_recognition(OCR_NODE, image)
        self._check_stopping(context)
        if detail is None:
            return image, []
        return image, [
            result
            for result in detail.all_results
            if _result_text(result)
        ]

    def _find_hall_candidates(self, items: list[Any]) -> list[dict[str, Any]]:
        numeric_items: list[dict[str, Any]] = []
        for item in items:
            text = _result_text(item)
            hall_id = _extract_hall_id(text)
            if not hall_id:
                continue
            x, y, width, height = _box(item)
            if not (
                HALL_CARD_X_RANGE[0] <= x <= HALL_CARD_X_RANGE[1]
                and HALL_CARD_Y_RANGE[0] <= y <= HALL_CARD_Y_RANGE[1]
            ):
                continue
            numeric_items.append(
                {
                    "hall_id": hall_id,
                    "x": x,
                    "y": y,
                    "width": width,
                    "height": height,
                    "card_y": max(HALL_CARD_Y_RANGE[0], y - 55),
                }
            )

        candidates: list[dict[str, Any]] = []
        used_ids: set[str] = set()
        for item in sorted(numeric_items, key=lambda value: value["y"]):
            hall_id = item["hall_id"]
            if hall_id in used_ids:
                continue
            used_ids.add(hall_id)

            name = hall_id
            name_candidates: list[tuple[int, str]] = []
            for other in items:
                text = _result_text(other)
                if not ROOM_TITLE_RE.fullmatch(text):
                    continue
                ox, oy, _, height = _box(other)
                if not (100 <= ox <= 500 and height >= 28):
                    continue
                if (
                    _extract_hall_id(text)
                    or text.startswith("监视器")
                    or any(label in text for label in HALL_CATEGORY_TEXTS)
                    or text in HALL_LIST_NAV_TEXTS
                ):
                    continue
                distance = item["y"] - oy
                if 35 <= distance <= 170:
                    name_candidates.append((distance, text))
            if name_candidates:
                name = max(name_candidates, key=lambda value: value[0])[1]
            item["name"] = name
            candidates.append(item)
        return candidates

    def _find_rank_rows(self, items: list[Any]) -> list[tuple[int, int]]:
        rows: list[tuple[int, int]] = []
        seen: set[int] = set()
        for item in items:
            text = _result_text(item)
            if not re.fullmatch(r"\d{1,3}", text):
                continue
            x, y, _, height = _box(item)
            rank = int(text)
            if x > RANK_LABEL_MAX_X or y < LEADERBOARD_START_Y:
                continue
            if rank < 4 or rank in seen:
                continue
            seen.add(rank)
            rows.append((rank, y + height // 2))
        return sorted(rows, key=lambda value: value[1])

    @staticmethod
    def _find_unopenable_ocr_ranks(
        items: list[Any],
        rank_rows: list[tuple[int, int]],
    ) -> set[int]:
        unopenable_name_ys = [
            _box(item)[1] + _box(item)[3] // 2
            for item in items
            if _result_text(item) in UNOPENABLE_PROFILE_NAMES
        ]
        return {
            rank
            for rank, row_y in rank_rows
            if any(abs(row_y - name_y) <= 60 for name_y in unopenable_name_ys)
        }

    def _extract_profile_id(self, items: list[Any]) -> str | None:
        for item in sorted(items, key=lambda value: _box(value)[1]):
            text = _result_text(item).translate(
                str.maketrans("０１２３４５６７８９", "0123456789")
            )
            match = PROFILE_ID_RE.search(text)
            if match:
                return match.group(1)

            x, y, _, _ = _box(item)
            if (
                PROFILE_ID_X_RANGE[0] <= x <= PROFILE_ID_X_RANGE[1]
                and PROFILE_ID_Y_RANGE[0] <= y <= PROFILE_ID_Y_RANGE[1]
                and re.fullmatch(r"\d{4,12}", text)
            ):
                # The badge immediately to the left of the ID is sometimes
                # joined to the OCR box and recognized as a leading zero.
                if x < 55 and text.startswith("0") and len(text) >= 5:
                    text = text[1:]
                return text
        return None

    def _extract_gender(
        self,
        image: Any,
        items: list[Any],
        hierarchy: str,
        unknown_gender_as_male: bool,
    ) -> tuple[str, str]:
        gender_box = _find_profile_gender_box(hierarchy)

        if gender_box:
            x, y, width, height = gender_box
            # Some app versions expose iv_gender; others omit it entirely. In
            # that layout the box is inferred from the gap between the ID copy
            # control and tv_age. Its left square contains the actual symbol:
            # cyan ♂ for male, pink/purple ♀ for female.
            icon_width = min(width, height)
            try:
                pixels = np.asarray(image)
                roi = pixels[y : y + height, x : x + icon_width, :3]
                gender = _classify_gender_color_points(roi)
                if gender == "男":
                    return "男", "icon:♂"
                if gender == "女":
                    return "女", "icon:♀"
            except (AttributeError, IndexError, TypeError, ValueError):
                pass

        # OCR is only accepted when it sees the literal symbol on the same row
        # as the ID/copy control. Words such as 女神/男神 elsewhere on the page
        # are categories or badges and must not determine the user's gender.
        id_y = self._profile_id_y(items)
        if id_y is None:
            id_resource = _find_profile_resource(hierarchy, ":id/tv_nice_num")
            if id_resource and id_resource[1]:
                id_y = _center(id_resource[1])[1]
        for item in items:
            text = _result_text(item)
            _, y, _, height = _box(item)
            if id_y is None or abs((y + height // 2) - id_y) > 35:
                continue
            if "♀" in text:
                return "女", "icon:♀:ocr"
            if "♂" in text:
                return "男", "icon:♂:ocr"
        if unknown_gender_as_male:
            return "男", "inferred:未识别到性别图标"
        return "未知", "unknown"

    def _extract_profile_name(self, items: list[Any], hierarchy: str) -> str | None:
        nickname_resource = _find_profile_resource(hierarchy, ":id/tv_nickname")
        if nickname_resource and nickname_resource[0]:
            return nickname_resource[0]

        id_y = self._profile_id_y(items)
        if id_y is None:
            return None
        min_y = id_y + PROFILE_NAME_Y_OFFSET[0]
        max_y = id_y + PROFILE_NAME_Y_OFFSET[1]
        candidates: list[tuple[int, str]] = []
        for item in items:
            text = _result_text(item)
            x, y, _, height = _box(item)
            center_y = y + height // 2
            if (
                text
                and PROFILE_NAME_X_RANGE[0] <= x <= PROFILE_NAME_X_RANGE[1]
                and min_y <= center_y <= max_y
                and not PROFILE_ID_RE.search(text)
            ):
                candidates.append((center_y, text))
        return max(candidates, default=(0, ""))[1] or None

    @staticmethod
    def _close_friend_state(
        hierarchy: str,
    ) -> tuple[list[str], bool, bool]:
        root = _parse_android_hierarchy(hierarchy)
        if root is None:
            return [], False, False

        has_friend_tab = any(
            node.attrib.get("text", "").strip() == "挚友"
            for node in root.iter("node")
        )
        friend_list = next(
            (
                node
                for node in root.iter("node")
                if node.attrib.get("resource-id", "").endswith(
                    ":id/recyclerView"
                )
            ),
            None,
        )
        if friend_list is None:
            return [], False, False

        explicitly_empty = any(
            "暂无挚友" in node.attrib.get("text", "")
            for node in friend_list.iter("node")
        )
        entries: list[tuple[int, int, str]] = []
        placeholders = {"虚位以待", "暂无挚友", "解锁挚友位"}
        for card in list(friend_list):
            card_box = _parse_android_bounds(card.attrib.get("bounds", ""))
            if card_box is None:
                continue
            name = ""
            day = ""
            for node in card.iter("node"):
                resource_id = node.attrib.get("resource-id", "")
                text = node.attrib.get("text", "").strip()
                if (
                    resource_id.endswith(":id/tv_header_right_name")
                    or resource_id.endswith(":id/tvCPName")
                ) and text not in placeholders:
                    name = text
                elif (
                    resource_id.endswith(":id/tv_header_cp_day")
                    or resource_id.endswith(":id/tvCPDay")
                ) and re.fullmatch(r"一起\s*\d+\s*天", text):
                    day = text
            if name and day:
                entries.append((card_box[1], card_box[0], f"{name}\x1f{day}"))
        entries.sort()
        return (
            [entry for _, _, entry in entries],
            explicitly_empty,
            has_friend_tab,
        )

    @staticmethod
    def _merge_close_friend_entries(
        collected: list[str],
        visible: list[str],
    ) -> None:
        max_overlap = min(len(collected), len(visible))
        overlap = 0
        for size in range(max_overlap, 0, -1):
            if collected[-size:] == visible[:size]:
                overlap = size
                break
        collected.extend(visible[overlap:])

    def _scan_close_friend_count(
        self,
        context: Context,
        items: list[Any],
        hierarchy: str,
        delay: float,
    ) -> int | None:
        collected, explicitly_empty, has_friend_section = self._close_friend_state(
            hierarchy
        )
        if explicitly_empty:
            return 0
        if not has_friend_section:
            return self._extract_close_friend_count(items, hierarchy)

        previous_visible = collected.copy()
        for _ in range(PROFILE_FRIEND_MAX_SCROLLS):
            self._check_stopping(context)
            self.controller.post_swipe(*PROFILE_FRIEND_SCROLL).wait()
            self._sleep(context, max(0.2, delay * 0.6))
            visible, explicitly_empty, current_has_section = self._close_friend_state(
                self._dump_ui_hierarchy()
            )
            if explicitly_empty:
                return 0
            if not current_has_section and not visible:
                continue
            if visible == previous_visible:
                break
            self._merge_close_friend_entries(collected, visible)
            previous_visible = visible
        return len(collected) if collected else None

    @classmethod
    def _extract_close_friend_count(
        cls,
        items: list[Any],
        hierarchy: str,
    ) -> int | None:
        entries, explicitly_empty, _ = cls._close_friend_state(hierarchy)
        if explicitly_empty:
            return 0
        if entries:
            return len(entries)

        texts = [_result_text(item) for item in items]
        if not any("挚友" in text for text in texts):
            return None
        if any("暂无挚友" in text for text in texts):
            return 0
        visible_cards = sum(
            bool(re.search(r"一起\s*\d+\s*天", text)) for text in texts
        )
        return visible_cards or None

    def _extract_profile_ip(self, items: list[Any], hierarchy: str) -> str | None:
        location_resource = _find_profile_resource(hierarchy, ":id/tv_location")
        if location_resource:
            match = PROFILE_IP_RE.search(location_resource[0])
            if match:
                return match.group(1).strip()

        for item in items:
            text = _result_text(item)
            match = PROFILE_IP_RE.search(text)
            if not match:
                continue
            x, y, _, _ = _box(item)
            if (
                PROFILE_IP_X_RANGE[0] <= x <= PROFILE_IP_X_RANGE[1]
                and PROFILE_IP_Y_RANGE[0] <= y <= PROFILE_IP_Y_RANGE[1]
            ):
                return match.group(1).strip()
        return None

    def _profile_id_y(self, items: list[Any]) -> int | None:
        for item in items:
            text = _result_text(item).translate(
                str.maketrans("０１２３４５６７８９", "0123456789")
            )
            x, y, _, height = _box(item)
            if PROFILE_ID_RE.search(text) or (
                PROFILE_ID_X_RANGE[0] <= x <= PROFILE_ID_X_RANGE[1]
                and PROFILE_ID_Y_RANGE[0] <= y <= PROFILE_ID_Y_RANGE[1]
                and re.fullmatch(r"\d{4,12}", text)
            ):
                return y + height // 2
        return None

    def _extract_profile_levels(
        self,
        items: list[Any],
        image: Any = None,
    ) -> tuple[int | None, int | None]:
        id_y = None
        for item in items:
            text = _result_text(item).translate(
                str.maketrans("０１２３４５６７８９", "0123456789")
            )
            x, y, _, _ = _box(item)
            if PROFILE_ID_RE.search(text) or (
                PROFILE_ID_X_RANGE[0] <= x <= PROFILE_ID_X_RANGE[1]
                and PROFILE_ID_Y_RANGE[0] <= y <= PROFILE_ID_Y_RANGE[1]
                and re.fullmatch(r"\d{4,12}", text)
            ):
                id_y = y
                break

        min_y, max_y = PROFILE_LEVEL_Y_RANGE
        if id_y is not None:
            min_y = id_y + PROFILE_LEVEL_Y_OFFSET[0]
            max_y = id_y + PROFILE_LEVEL_Y_OFFSET[1]

        candidates: dict[str, list[tuple[int, int]]] = {
            "wealth": [],
            "charm": [],
        }
        for item in items:
            text = _result_text(item).translate(
                str.maketrans("０１２３４５６７８９", "0123456789")
            )
            x, y, width, height = _box(item)
            if not (min_y <= y <= max_y):
                continue

            field = self._profile_level_field(x)
            if field is None or "?" in text:
                continue

            runs = re.findall(r"\d+", text)
            if not runs:
                continue
            # Decorations commonly produce `1:141`, `2151` or `Q171`.
            # The real level is the longest run, limited to its last 3 digits.
            raw_digits = max(runs, key=len)
            digits = raw_digits[-3:]
            corrected = self._correct_level_digits_from_image(
                image,
                (x, y, width, height),
                field,
                digits,
            )
            if corrected is None:
                continue
            level = int(corrected)
            # Prefer a candidate containing more actual digits. This handles
            # OCR that splits a badge into an icon-shaped `O` and its number.
            candidates[field].append((len(corrected), level))

        wealth_level = max(candidates["wealth"], default=(0, None))[1]
        charm_level = max(candidates["charm"], default=(0, None))[1]
        return wealth_level, charm_level

    @staticmethod
    def _profile_level_field(x: int) -> str | None:
        if PROFILE_WEALTH_X_RANGE[0] <= x <= PROFILE_WEALTH_X_RANGE[1]:
            return "wealth"
        if PROFILE_CHARM_X_RANGE[0] <= x <= PROFILE_CHARM_X_RANGE[1]:
            return "charm"
        return None

    @staticmethod
    def _correct_level_digits_from_image(
        image: Any,
        box: tuple[int, int, int, int],
        field: str,
        digits: str,
    ) -> str | None:
        if image is None or not digits:
            return digits

        x, y, width, height = box
        digit_x_range = (
            PROFILE_WEALTH_DIGIT_X_RANGE
            if field == "wealth"
            else PROFILE_CHARM_DIGIT_X_RANGE
        )
        # OCR boxes beginning in the decoration area deliberately include the
        # badge icon. Long runs from such boxes are handled by taking the last
        # three digits, but their connected components cannot be counted.
        if x < digit_x_range[0] - 2:
            return digits

        try:
            pixels = np.asarray(image)
            if pixels.ndim != 3 or pixels.shape[2] < 3:
                return digits
            image_height, image_width = pixels.shape[:2]
            left = max(0, x - 3)
            top = max(0, y - 3)
            right = min(image_width, x + width + 3)
            bottom = min(image_height, y + height + 3)
            roi = pixels[top:bottom, left:right, :3].astype(np.int16)
            if roi.size == 0:
                return digits

            channel_min = roi.min(axis=2)
            channel_max = roi.max(axis=2)
            chroma = channel_max - channel_min
            mask = (
                (channel_min >= 150)
                & (channel_max >= 185)
                & (chroma <= 90)
            ).astype(np.uint8)
            _, _, stats, _ = cv2.connectedComponentsWithStats(mask, 8)
            min_height = max(8, int(height * 0.38))
            component_count = sum(
                1
                for component_x, _, component_width, component_height, area in stats[1:]
                if component_width >= 2
                and component_height >= min_height
                and area >= 15
                and (left + component_x) >= digit_x_range[0] - 3
            )
        except (AttributeError, IndexError, TypeError, ValueError, cv2.error):
            return digits

        if not 1 <= component_count <= 3:
            return digits
        if len(digits) > component_count:
            return digits[-component_count:]
        if len(digits) < component_count:
            # A partial fragment such as the trailing `0` of level 10 should
            # trigger the narrow retry OCR instead of being recorded as 0.
            return None
        return digits

    @staticmethod
    def _profile_level_looks_hidden(image: Any, field: str) -> bool:
        if image is None:
            return False
        x_range = (
            PROFILE_WEALTH_DIGIT_X_RANGE
            if field == "wealth"
            else PROFILE_CHARM_DIGIT_X_RANGE
        )
        try:
            pixels = np.asarray(image)
            roi = pixels[
                PROFILE_LEVEL_Y_RANGE[0] : PROFILE_LEVEL_Y_RANGE[1],
                x_range[0] : x_range[1],
                :3,
            ].astype(np.int16)
            if roi.size == 0:
                return False
            channel_min = roi.min(axis=2)
            channel_max = roi.max(axis=2)
            mask = (
                (channel_min >= 150)
                & (channel_max >= 185)
                & ((channel_max - channel_min) <= 90)
            ).astype(np.uint8)
            _, _, stats, _ = cv2.connectedComponentsWithStats(mask, 8)
        except (AttributeError, IndexError, TypeError, ValueError, cv2.error):
            return False

        # Each visible `?` consists of a large hook and a detached 2-4 px dot.
        # Three small, evenly spaced dots on the same baseline distinguish the
        # app's `???` privacy badge from ordinary digits and badge sparkles.
        dots = sorted(
            (
                int(x + x_range[0] + width // 2),
                int(y + PROFILE_LEVEL_Y_RANGE[0] + height // 2),
            )
            for x, y, width, height, area in stats[1:]
            if x + x_range[0] >= x_range[0] + 3
            and 2 <= width <= 5
            and 2 <= height <= 4
            and 3 <= area <= 15
        )
        for first, second, third in zip(dots, dots[1:], dots[2:]):
            xs = (first[0], second[0], third[0])
            ys = (first[1], second[1], third[1])
            if (
                max(ys) - min(ys) <= 2
                and 7 <= xs[1] - xs[0] <= 15
                and 7 <= xs[2] - xs[1] <= 15
            ):
                return True
        return False

    def _hidden_profile_levels(
        self,
        items: list[Any],
        image: Any = None,
    ) -> set[str]:
        hidden: set[str] = set()
        id_y = self._profile_id_y(items)
        min_y, max_y = PROFILE_LEVEL_Y_RANGE
        if id_y is not None:
            min_y = id_y + PROFILE_LEVEL_Y_OFFSET[0] - 15
            max_y = id_y + PROFILE_LEVEL_Y_OFFSET[1]
        for item in items:
            text = _result_text(item)
            x, y, _, _ = _box(item)
            field = self._profile_level_field(x)
            if field and min_y <= y <= max_y and "?" in text:
                hidden.add(field)
        for field in ("wealth", "charm"):
            if self._profile_level_looks_hidden(image, field):
                hidden.add(field)
        return hidden

    @staticmethod
    def _level_from_retry_text(text: str) -> int | None:
        normalized = text.translate(
            str.maketrans(
                "０１２３４５６７８９OoQqIl|",
                "01234567890000111",
            )
        )
        runs = re.findall(r"\d+", normalized)
        if not runs:
            return None
        digits = max(runs, key=len)
        return int(digits[-3:])

    def _retry_profile_level(
        self,
        context: Context,
        image: Any,
        field: str,
        id_y: int | None,
    ) -> int | None:
        if image is None:
            return None
        x_range = (
            PROFILE_WEALTH_DIGIT_X_RANGE
            if field == "wealth"
            else PROFILE_CHARM_DIGIT_X_RANGE
        )
        top = (
            id_y + PROFILE_LEVEL_DIGIT_Y_OFFSET[0]
            if id_y is not None
            else PROFILE_LEVEL_Y_RANGE[0]
        )
        bottom = (
            id_y + PROFILE_LEVEL_DIGIT_Y_OFFSET[1]
            if id_y is not None
            else PROFILE_LEVEL_Y_RANGE[1]
        )
        try:
            pixels = np.asarray(image)
            crop = pixels[top:bottom, x_range[0] : x_range[1], :3]
            if crop.size == 0:
                return None
            crop = cv2.resize(crop, None, fx=3, fy=3, interpolation=cv2.INTER_CUBIC)
            crop = cv2.copyMakeBorder(
                crop,
                12,
                12,
                12,
                12,
                cv2.BORDER_REPLICATE,
            )
            detail = context.run_recognition_direct(
                JRecognitionType.OCR,
                JOCR(only_rec=True, threshold=0.2),
                crop,
            )
            if detail is None:
                return None
            for result in getattr(detail, "all_results", []):
                level = self._level_from_retry_text(_result_text(result))
                if level is not None:
                    return level
        except (
            AttributeError,
            IndexError,
            RuntimeError,
            TypeError,
            ValueError,
            cv2.error,
        ):
            return None
        return None

    def _retry_missing_profile_levels(
        self,
        context: Context,
        image: Any,
        items: list[Any],
        wealth_level: int | None,
        charm_level: int | None,
    ) -> tuple[int | None, int | None]:
        hidden = self._hidden_profile_levels(items, image)
        id_y = self._profile_id_y(items)
        if wealth_level is None and "wealth" not in hidden:
            wealth_level = self._retry_profile_level(context, image, "wealth", id_y)
            if wealth_level is not None:
                self._log(f"财富等级使用数字窄区 OCR 补识别：{wealth_level}")
        if charm_level is None and "charm" not in hidden:
            charm_level = self._retry_profile_level(context, image, "charm", id_y)
            if charm_level is not None:
                self._log(f"魅力等级使用数字窄区 OCR 补识别：{charm_level}")
        return wealth_level, charm_level

    def _is_hall_list(self, items: list[Any]) -> bool:
        if (
            self._is_room_page(items)
            or self._is_contribution_panel(items)
            or self._is_more_menu(items)
            or self._is_profile_page(items)
        ):
            return False

        category_labels: set[str] = set()
        for item in items:
            text = _result_text(item)
            x, y, _, _ = _box(item)
            if not (HALL_CATEGORY_Y_RANGE[0] <= y <= HALL_CATEGORY_Y_RANGE[1]):
                continue
            for label, x_range in HALL_CATEGORY_X_RANGES.items():
                if label in text and x_range[0] <= x <= x_range[1]:
                    category_labels.add(label)

        category_count = len(category_labels)
        has_hall_nav = any(
            label in _result_text(item) and _box(item)[1] >= HALL_NAV_MIN_Y
            for label in HALL_LIST_NAV_TEXTS
            for item in items
        )
        candidate_count = len(self._find_hall_candidates(items))

        # Room/profile pages also contain long numeric IDs. Only accept card IDs
        # when accompanied by a spatially verified list-page anchor.
        return (
            category_count >= 2
            or (candidate_count >= 2 and has_hall_nav)
            or (candidate_count >= 1 and (category_count >= 1 or has_hall_nav))
        )

    @staticmethod
    def _is_room_page(items: list[Any]) -> bool:
        texts = [_result_text(item) for item in items]
        has_bulletin = any("公告" in text for text in texts)
        has_room_control = any(
            "房间榜" in text or "聊聊天" in text or "上麦" in text
            for text in texts
        )
        return has_bulletin and has_room_control

    @staticmethod
    def _is_contribution_panel(items: list[Any]) -> bool:
        for item in items:
            if "房间贡献榜" not in _result_text(item):
                continue
            _, y, _, _ = _box(item)
            if CONTRIBUTION_HEADER_Y_RANGE[0] <= y <= CONTRIBUTION_HEADER_Y_RANGE[1]:
                return True
        return False

    @staticmethod
    def _is_more_menu(items: list[Any]) -> bool:
        texts = [_result_text(item) for item in items]
        return any("关闭房间" in text for text in texts) and any(
            "举报房间" in text for text in texts
        )

    @staticmethod
    def _is_profile_page(items: list[Any]) -> bool:
        texts = [_result_text(item) for item in items]
        has_header = any("分享" in text for text in texts) and any(
            "更多" in text for text in texts
        )
        has_profile_info = any(
            "粉丝" in text or PROFILE_IP_RE.search(text) for text in texts
        )
        joined_text = " ".join(texts)
        profile_tab_count = sum(
            label in joined_text for label in ("挚友", "礼物墙", "装扮展馆")
        )
        has_profile_action = any(
            "关注" in text or "编辑资料" in text for text in texts
        )
        return (has_header and has_profile_info) or (
            profile_tab_count >= 2 and has_profile_action
        )

    @staticmethod
    def _now() -> str:
        return datetime.now(CHINA_TZ).isoformat(timespec="seconds")

    @staticmethod
    def _log(*values: Any) -> None:
        message = "[VoiceHall] " + " ".join(str(value) for value in values)
        print(message, flush=True)
        try:
            DEFAULT_DEBUG_LOG.parent.mkdir(parents=True, exist_ok=True)
            with DEFAULT_DEBUG_LOG.open("a", encoding="utf-8") as file:
                file.write(f"{datetime.now(CHINA_TZ).isoformat(timespec='seconds')} {message}\n")
        except OSError:
            pass

    @staticmethod
    def _upsert_daily_record(
        records: list[dict[str, Any]],
        record: dict[str, Any],
    ) -> bool:
        scanned_day = str(record.get("scanned_at", ""))[:10]
        matches = [
            index
            for index, existing in enumerate(records)
            if str(existing.get("room_id")) == str(record.get("room_id"))
            and str(existing.get("user_id")) == str(record.get("user_id"))
            and str(existing.get("scanned_at", ""))[:10] == scanned_day
        ]
        if not matches:
            records.append(record)
            return False

        records[matches[0]] = record
        for index in reversed(matches[1:]):
            del records[index]
        return True

@AgentServer.custom_action("scan_voice_hall_contributions")
class VoiceHallContributionAction(ContributionScanner):
    pass


@AgentServer.custom_action(SKIP_SCANNED_CUSTOM_ACTION)
class VoiceHallSkipScannedContributionAction(ContributionScanner):
    pass
