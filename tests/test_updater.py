import json
import sys
import tempfile
import unittest
import zipfile
from pathlib import Path


sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "tools"))

from updater import (  # noqa: E402
    UpdateError,
    _package_root,
    _safe_extract,
    apply_package,
    current_version,
    select_release_asset,
    update,
)


class UpdaterTest(unittest.TestCase):
    def test_selects_windows_x64_release_zip(self) -> None:
        release = {
            "assets": [
                {"name": "source.zip"},
                {
                    "name": "HelloFish-win-x86_64-v1.2.3.zip",
                    "browser_download_url": "https://github.com/example/release.zip",
                },
            ]
        }

        self.assertEqual(
            select_release_asset(release)["name"],
            "HelloFish-win-x86_64-v1.2.3.zip",
        )

    def test_safe_extract_rejects_parent_path(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            archive = root / "bad.zip"
            destination = root / "out"
            destination.mkdir()
            with zipfile.ZipFile(archive, "w") as package:
                package.writestr("../outside.txt", "bad")

            with self.assertRaisesRegex(UpdateError, "越界路径"):
                _safe_extract(archive, destination)

            self.assertFalse((root / "outside.txt").exists())

    def test_safe_extract_rejects_windows_parent_path(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            archive = root / "bad-windows.zip"
            destination = root / "out"
            destination.mkdir()
            with zipfile.ZipFile(archive, "w") as package:
                package.writestr("..\\outside.txt", "bad")

            with self.assertRaisesRegex(UpdateError, "越界路径"):
                _safe_extract(archive, destination)

    def test_package_root_accepts_single_wrapper_directory(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            wrapped = root / "HelloFish"
            wrapped.mkdir()
            wrapped.joinpath("interface.json").write_text("{}", encoding="utf-8")

            self.assertEqual(_package_root(root), wrapped)

    def test_apply_overwrites_program_but_preserves_data(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            package = root / "package"
            install = root / "install"
            package.joinpath("agent").mkdir(parents=True)
            package.joinpath("data").mkdir()
            install.joinpath("agent").mkdir(parents=True)
            install.joinpath("data").mkdir()
            package.joinpath("agent", "main.py").write_text("new", encoding="utf-8")
            package.joinpath("data", "voice_hall.sqlite3").write_text(
                "release-data", encoding="utf-8"
            )
            install.joinpath("agent", "main.py").write_text("old", encoding="utf-8")
            install.joinpath("data", "voice_hall.sqlite3").write_text(
                "user-data", encoding="utf-8"
            )

            apply_package(package, install)

            self.assertEqual(
                install.joinpath("agent", "main.py").read_text(encoding="utf-8"),
                "new",
            )
            self.assertEqual(
                install.joinpath("data", "voice_hall.sqlite3").read_text(
                    encoding="utf-8"
                ),
                "user-data",
            )

    def test_reads_installed_interface_version(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            root.joinpath("interface.json").write_text(
                json.dumps({"version": "v1.2.3"}), encoding="utf-8"
            )
            self.assertEqual(current_version(root), "v1.2.3")

    def test_update_rejects_non_release_directory_before_network_access(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            with self.assertRaisesRegex(UpdateError, "缺少 interface.json"):
                update(Path(temp), "BigFishWuhu/HelloFish", False, True)


if __name__ == "__main__":
    unittest.main()
