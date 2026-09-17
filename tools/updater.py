from __future__ import annotations

import argparse
import hashlib
import json
import os
import shutil
import subprocess
import sys
import tempfile
import urllib.error
import urllib.request
import zipfile
from pathlib import Path
from typing import Any


DEFAULT_REPOSITORY = "BigFishWuhu/HelloFish"
ASSET_PREFIX = "HelloFish-win-x86_64-"
USER_AGENT = "HelloFish-Updater/1"
MAX_ARCHIVE_BYTES = 2 * 1024 * 1024 * 1024
MAX_EXTRACTED_BYTES = 4 * 1024 * 1024 * 1024


class UpdateError(RuntimeError):
    pass


def _request_json(url: str) -> dict[str, Any]:
    request = urllib.request.Request(
        url,
        headers={
            "Accept": "application/vnd.github+json",
            "User-Agent": USER_AGENT,
            "X-GitHub-Api-Version": "2022-11-28",
        },
    )
    try:
        with urllib.request.urlopen(request, timeout=30) as response:
            value = json.load(response)
    except (OSError, urllib.error.URLError, json.JSONDecodeError) as exc:
        raise UpdateError(f"无法读取 GitHub Release 信息：{exc}") from exc
    if not isinstance(value, dict):
        raise UpdateError("GitHub Release 返回了无效数据")
    return value


def select_release_asset(release: dict[str, Any]) -> dict[str, Any]:
    assets = release.get("assets")
    if not isinstance(assets, list):
        raise UpdateError("最新 Release 没有可下载文件")
    candidates = [
        asset
        for asset in assets
        if isinstance(asset, dict)
        and str(asset.get("name", "")).startswith(ASSET_PREFIX)
        and str(asset.get("name", "")).lower().endswith(".zip")
    ]
    if not candidates:
        raise UpdateError(f"最新 Release 中没有 {ASSET_PREFIX}*.zip")
    return candidates[0]


def current_version(install_dir: Path) -> str:
    try:
        value = json.loads((install_dir / "interface.json").read_text(encoding="utf-8"))
    except (FileNotFoundError, OSError, json.JSONDecodeError):
        return "未知"
    return str(value.get("version") or "未知") if isinstance(value, dict) else "未知"


def _download_asset(asset: dict[str, Any], destination: Path) -> None:
    url = str(asset.get("browser_download_url", ""))
    if not url.startswith("https://github.com/"):
        raise UpdateError("Release 下载地址无效")
    expected_size = int(asset.get("size") or 0)
    if expected_size > MAX_ARCHIVE_BYTES:
        raise UpdateError("Release 压缩包超过 2 GB，已拒绝下载")

    request = urllib.request.Request(url, headers={"User-Agent": USER_AGENT})
    digest = hashlib.sha256()
    downloaded = 0
    try:
        with (
            urllib.request.urlopen(request, timeout=60) as response,
            destination.open("wb") as file,
        ):
            total = int(response.headers.get("Content-Length") or expected_size or 0)
            while True:
                chunk = response.read(1024 * 1024)
                if not chunk:
                    break
                downloaded += len(chunk)
                if downloaded > MAX_ARCHIVE_BYTES:
                    raise UpdateError("Release 压缩包超过 2 GB，已停止下载")
                digest.update(chunk)
                file.write(chunk)
                if total:
                    percent = min(100, downloaded * 100 // total)
                    print(f"\r下载进度：{percent:3d}%", end="", flush=True)
    except (OSError, urllib.error.URLError) as exc:
        raise UpdateError(f"下载 Release 失败：{exc}") from exc
    finally:
        if downloaded:
            print()

    if expected_size and downloaded != expected_size:
        raise UpdateError(f"下载大小不符：期望 {expected_size}，实际 {downloaded}")
    expected_digest = str(asset.get("digest") or "")
    if expected_digest.startswith("sha256:"):
        actual_digest = digest.hexdigest()
        if actual_digest.lower() != expected_digest.removeprefix("sha256:").lower():
            raise UpdateError("Release SHA-256 校验失败")


def _safe_extract(archive: Path, destination: Path) -> None:
    destination = destination.resolve()
    try:
        with zipfile.ZipFile(archive) as package:
            extracted_size = 0
            for member in package.infolist():
                extracted_size += member.file_size
                if extracted_size > MAX_EXTRACTED_BYTES:
                    raise UpdateError("Release 解压后超过 4 GB，已拒绝处理")
                normalized_name = member.filename.replace("\\", "/")
                target = (destination / normalized_name).resolve()
                try:
                    inside_destination = (
                        os.path.commonpath((destination, target)) == str(destination)
                    )
                except ValueError:
                    inside_destination = False
                if not inside_destination:
                    raise UpdateError(f"压缩包包含越界路径：{member.filename}")
                mode = member.external_attr >> 16
                if mode & 0o170000 == 0o120000:
                    raise UpdateError(f"压缩包包含不支持的符号链接：{member.filename}")
            package.extractall(destination)
    except (OSError, zipfile.BadZipFile) as exc:
        raise UpdateError(f"Release 压缩包无效：{exc}") from exc


def _package_root(extracted: Path) -> Path:
    if (extracted / "interface.json").is_file():
        return extracted
    children = [path for path in extracted.iterdir() if path.is_dir()]
    if len(children) == 1 and (children[0] / "interface.json").is_file():
        return children[0]
    raise UpdateError("Release 中没有找到 interface.json，未覆盖现有安装")


def _mxu_running() -> bool:
    if os.name != "nt":
        return False
    result = subprocess.run(
        ["tasklist", "/FI", "IMAGENAME eq mxu.exe", "/FO", "CSV", "/NH"],
        capture_output=True,
        text=True,
        check=False,
        creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
    )
    return '"mxu.exe"' in result.stdout.lower()


def apply_package(package_root: Path, install_dir: Path) -> None:
    install_dir.mkdir(parents=True, exist_ok=True)
    for source in package_root.iterdir():
        if source.name.lower() == "data":
            continue
        destination = install_dir / source.name
        if source.is_dir():
            shutil.copytree(source, destination, dirs_exist_ok=True)
        else:
            shutil.copy2(source, destination)


def update(install_dir: Path, repository: str, force: bool, assume_yes: bool) -> bool:
    if not (install_dir / "interface.json").is_file():
        raise UpdateError(
            f"目标目录不是 HelloFish 发布包：{install_dir}（缺少 interface.json）"
        )
    if _mxu_running():
        raise UpdateError("检测到 mxu.exe 正在运行，请关闭 MXU 后重新执行更新")

    api_url = f"https://api.github.com/repos/{repository}/releases/latest"
    print("正在检查 GitHub 最新版本...")
    release = _request_json(api_url)
    latest = str(release.get("tag_name") or "未知")
    installed = current_version(install_dir)
    print(f"当前版本：{installed}")
    print(f"最新版本：{latest}")
    if not force and installed == latest:
        print("当前已经是最新版本。")
        return False

    asset = select_release_asset(release)
    print(f"更新包：{asset.get('name')}")
    if not assume_yes:
        answer = input("更新将覆盖程序文件并保留 data 目录，是否继续？[y/N] ").strip().lower()
        if answer not in {"y", "yes"}:
            print("已取消更新。")
            return False

    with tempfile.TemporaryDirectory(prefix="hellofish-update-") as temp:
        temp_dir = Path(temp)
        archive = temp_dir / "release.zip"
        extracted = temp_dir / "extracted"
        extracted.mkdir()
        _download_asset(asset, archive)
        print("正在解压并验证更新包...")
        _safe_extract(archive, extracted)
        package_root = _package_root(extracted)
        print("正在覆盖程序文件...")
        apply_package(package_root, install_dir)

    print(f"更新完成：{latest}")
    return True


def main() -> int:
    parser = argparse.ArgumentParser(description="HelloFish GitHub Release 更新器")
    parser.add_argument(
        "--install-dir",
        type=Path,
        default=Path(__file__).resolve().parent,
        help="HelloFish 安装目录，默认为更新器所在目录",
    )
    parser.add_argument("--repo", default=DEFAULT_REPOSITORY, help="GitHub owner/repo")
    parser.add_argument("--force", action="store_true", help="版本相同时仍覆盖更新")
    parser.add_argument("--yes", action="store_true", help="不询问确认")
    args = parser.parse_args()
    try:
        update(args.install_dir.resolve(), args.repo, args.force, args.yes)
    except (UpdateError, PermissionError) as exc:
        print(f"更新失败：{exc}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
