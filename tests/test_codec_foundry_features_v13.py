# -*- coding: utf-8 -*-
"""Feature tests: FFmpeg provisioning, update fallback, live scheduler reorder."""
from __future__ import annotations

import importlib.util
import json
import os
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
ROOT = Path(__file__).resolve().parents[1]
TEST_APP_DATA = Path(tempfile.mkdtemp(prefix="codec_foundry_feature_test_"))
os.environ["CODECFOUNDRY_DATA_DIR"] = str(TEST_APP_DATA)
MODULE_PATH = ROOT / "CodecFoundry.pyw"
SPEC = importlib.util.spec_from_file_location("codec_foundry_v1_features", MODULE_PATH)
assert SPEC is not None and SPEC.loader is not None
cf = importlib.util.module_from_spec(SPEC)
sys.modules[SPEC.name] = cf
SPEC.loader.exec_module(cf)


def make_task(output_name: str, codec: str = "hevc") -> cf.VideoTask:
    return cf.VideoTask(
        source=Path(f"D:/src/{output_name}"),
        output=Path(f"D:/out/{output_name}"),
        info=cf.VideoInfo(codec, 1920, 1080, 30.0, duration=1.0, frame_count=30),
    )


def make_settings(codec: str = "hevc") -> cf.EncodeSettings:
    return cf.EncodeSettings(
        codec=codec,
        maxrate=None,
        bufsize=None,
        fps_text=None,
        fps_value=None,
        resolution=None,
        cq=23.0,
        preset="p7",
        lookahead=20,
        multipass="fullres",
        overwrite=False,
        copy_subtitles=True,
    )


def _close_window(window) -> None:
    """Close a test window deterministically (filter removed, object deleted)."""
    app = cf.QApplication.instance()
    if app is not None:
        try:
            app.removeEventFilter(window)
        except RuntimeError:
            pass
    window.app_logger.close()
    window.close_finalized = True
    window.close()
    window.deleteLater()
    if app is not None:
        app.processEvents()


def make_item(output_name: str, codec: str = "hevc", preferred_slot: int = 0) -> cf.PendingTaskItem:
    return cf.PendingTaskItem(
        task=make_task(output_name, codec),
        settings=make_settings(codec),
        preferred_slot=preferred_slot,
        ffmpeg="ffmpeg",
        ffprobe="ffprobe",
        affinity_enabled=True,
        software_fallback=True,
        validation_gpus=(),
    )


class FfmpegVersionTests(unittest.TestCase):
    def test_old_ffmpeg_without_fps_mode_is_rejected(self):
        completed = cf.subprocess.CompletedProcess(["ffmpeg"], 0, "ffmpeg version 4.4.2-0ubuntu1\n", "")
        with mock.patch.object(cf, "run_captured_text", return_value=completed):
            self.assertEqual(cf._tool_version_tuple("ffmpeg"), (4, 4, 2, 0))
            self.assertFalse(cf.ffmpeg_version_ok("ffmpeg"))

    def test_modern_ffmpeg_is_accepted(self):
        completed = cf.subprocess.CompletedProcess(["ffmpeg"], 0, "ffmpeg version 6.1.1-full_build-www.gyan.dev\n", "")
        with mock.patch.object(cf, "run_captured_text", return_value=completed):
            self.assertTrue(cf.ffmpeg_version_ok("ffmpeg"))

    def test_gyan_full_build_version_parses(self):
        completed = cf.subprocess.CompletedProcess(
            ["ffmpeg"], 0, "ffmpeg version 9.0.1-full_build-www.gyan.dev Copyright (c) 2000-2026\n", ""
        )
        with mock.patch.object(cf, "run_captured_text", return_value=completed):
            self.assertEqual(cf._tool_version_tuple("ffmpeg"), (9, 0, 1, 0))
            self.assertTrue(cf.ffmpeg_version_ok("ffmpeg"))

    def test_git_build_without_dotted_version_is_judged_by_date(self):
        modern_git = cf.subprocess.CompletedProcess(
            ["ffmpeg"], 0,
            "ffmpeg version git-2026-02-09-9bfa1635ae-essentials_build-www.gyan.dev "
            "Copyright (c) 2000-2026\n",
            "",
        )
        with mock.patch.object(cf, "run_captured_text", return_value=modern_git):
            self.assertEqual(cf._tool_version_tuple("ffmpeg"), (2026, 2, 9, 0))
            self.assertTrue(cf.ffmpeg_version_ok("ffmpeg"))
        ancient_git = cf.subprocess.CompletedProcess(
            ["ffmpeg"], 0, "ffmpeg version git-2019-01-01-abcdef1\n", ""
        )
        with mock.patch.object(cf, "run_captured_text", return_value=ancient_git):
            self.assertFalse(cf.ffmpeg_version_ok("ffmpeg"))

    def test_broken_or_missing_executable_is_rejected(self):
        with mock.patch.object(cf, "run_captured_text", side_effect=OSError("missing")):
            self.assertIsNone(cf._tool_version_tuple("ffmpeg"))
            self.assertFalse(cf.ffmpeg_version_ok("ffmpeg"))

    def test_explicit_cli_paths_are_kept_verbatim(self):
        with mock.patch.object(cf, "ensure_ffmpeg_runtime") as ensure:
            ffmpeg, ffprobe = cf.resolve_ffmpeg_toolchain(
                "D:/tools/ffmpeg.exe", "D:/tools/ffprobe.exe",
                explicit_ffmpeg=True, explicit_ffprobe=True,
            )
        self.assertEqual(ffmpeg, "D:/tools/ffmpeg.exe")
        self.assertEqual(ffprobe, "D:/tools/ffprobe.exe")
        ensure.assert_not_called()

    def test_ensure_ffmpeg_runtime_uses_installed_pair(self):
        cf._ffmpeg_runtime_cache = None
        install_dir = cf.FFMPEG_INSTALL_DIR
        install_dir.mkdir(parents=True, exist_ok=True)
        (install_dir / "ffmpeg.exe").write_bytes(b"fake")
        (install_dir / "ffprobe.exe").write_bytes(b"fake")
        with mock.patch.object(cf, "ffmpeg_version_ok", return_value=True):
            ffmpeg, ffprobe = cf.ensure_ffmpeg_runtime()
        self.assertEqual(Path(ffmpeg), install_dir / "ffmpeg.exe")
        self.assertEqual(Path(ffprobe), install_dir / "ffprobe.exe")
        cf._ffmpeg_runtime_cache = None

    def test_external_ffmpeg_from_appdata_apps_is_preferred_over_path(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            appdata = Path(temp_dir) / "Roaming"
            flashcut = appdata / "FlashCut" / "ffmpeg"
            flashcut.mkdir(parents=True)
            flashcut_ff = flashcut / "ffmpeg.exe"
            flashcut_fp = flashcut / "ffprobe.exe"
            flashcut_ff.write_bytes(b"x")
            flashcut_fp.write_bytes(b"x")
            with mock.patch.dict(
                cf.os.environ, {"APPDATA": str(appdata)}
            ), mock.patch.object(cf, "ffmpeg_version_ok", return_value=True):
                found = cf._find_external_ffmpeg()
            self.assertIsNotNone(found)
            assert found is not None
            self.assertEqual(found[0].parent, flashcut)
            self.assertEqual(found[1].parent, flashcut)

    def test_ensure_ffmpeg_runtime_raises_when_nothing_works(self):
        cf._ffmpeg_runtime_cache = None
        with mock.patch.object(cf, "ffmpeg_version_ok", return_value=False), \
             mock.patch.object(cf, "_install_ffmpeg_binaries", return_value=False), \
             mock.patch.object(cf, "_find_external_ffmpeg", return_value=None):
            with self.assertRaises(cf.TranscodeError):
                cf.ensure_ffmpeg_runtime()
        cf._ffmpeg_runtime_cache = None

    def test_github_ffmpeg_assets_prefer_full_build(self):
        payload = {
            "tag_name": "9.0.1",
            "assets": [
                {"name": "ffmpeg-9.0.1-essentials_build.7z",
                 "browser_download_url": "https://github.example/essentials.7z"},
                {"name": "ffmpeg-9.0.1-full_build.7z",
                 "browser_download_url": "https://github.example/full.7z"},
            ],
        }
        with mock.patch.object(cf, "_fetch_update_json", return_value=dict(payload)):
            assets = cf._parse_github_ffmpeg_assets()
        self.assertEqual(assets, [("ffmpeg-9.0.1-full_build.7z",
                                   "https://github.example/full.7z")])

    def test_gitee_ffmpeg_assets_listed(self):
        payload = {
            "assets": [
                {"name": "ffmpeg.zip", "browser_download_url": "https://gitee.example/ffmpeg.zip"},
                {"name": "ffprobe.zip", "browser_download_url": "https://gitee.example/ffprobe.zip"},
                {"name": "notes.txt", "browser_download_url": "https://gitee.example/notes.txt"},
                {"name": "ffplay.zip", "browser_download_url": "https://gitee.example/ffplay.zip"},
            ],
        }
        with mock.patch.object(cf, "_fetch_update_json", return_value=dict(payload)):
            assets = cf._parse_gitee_ffmpeg_assets()
        self.assertEqual(len(assets), 2)
        self.assertTrue(all(name.endswith(".zip") for name, _ in assets))
        self.assertFalse(any("ffplay" in name for name, _ in assets))

    def test_stale_part_files_are_cleaned_before_install(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            install_dir = Path(temp_dir)
            (install_dir / "ffmpeg.zip.part").write_bytes(b"junk")
            (install_dir / "ffprobe.7z.part").write_bytes(b"junk")
            leftover = install_dir / ".dl-github"
            leftover.mkdir()
            (leftover / "old.zip").write_bytes(b"junk")
            (leftover / "old.zip.part").write_bytes(b"junk")
            extracted = leftover / "extracted"
            extracted.mkdir()
            (extracted / "partial.exe").write_bytes(b"x")
            cf._cleanup_stale_downloads(install_dir)
            self.assertEqual(list(install_dir.glob("*.part")), [])
            self.assertEqual(list(leftover.glob("*.part")), [])
            self.assertFalse(extracted.exists())
            # finished archives and the work dir survive so extraction can resume
            self.assertTrue((leftover / "old.zip").exists())

    def test_archive_readable_accepts_intact_zip(self):
        import zipfile as zip_module
        with tempfile.TemporaryDirectory() as temp_dir:
            archive = Path(temp_dir) / "ok.zip"
            with zip_module.ZipFile(archive, "w") as zip_file:
                zip_file.writestr("bin/ffmpeg.exe", b"fake")
            self.assertTrue(cf._archive_readable(archive))
            archive.write_bytes(b"not a zip")
            self.assertFalse(cf._archive_readable(archive))

    def test_extraction_prefers_native_7z_over_py7zr(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            archive = Path(temp_dir) / "x.zip"
            import zipfile as zip_module
            with zip_module.ZipFile(archive, "w") as zip_file:
                zip_file.writestr("ffmpeg.exe", b"fake")
            with mock.patch.object(cf.shutil, "which", return_value="C:/tools/7z.exe"), \
                 mock.patch.object(
                     cf, "_extract_with_native_tool", return_value=True
                 ) as native:
                self.assertTrue(cf._extract_archive(archive, Path(temp_dir) / "out"))
            native.assert_called_once()

    def test_fps_mode_capability_fallback_accepts_unparseable_build(self):
        def fake_run(command, *args, **kwargs):
            if "-h" in command:
                return cf.subprocess.CompletedProcess(
                    command, 0, "  -fps_mode            force constant frame rate\n", ""
                )
            return cf.subprocess.CompletedProcess(
                command, 0, "ffmpeg version some-unknown-build\n", ""
            )
        with mock.patch.object(cf, "run_captured_text", side_effect=fake_run):
            self.assertTrue(cf.ffmpeg_version_ok("ffmpeg"))

    def test_probe_sources_ranks_fastest_first(self):
        fast = cf.FfmpegSource("github", "fast", [("a.7z", "https://github.example/a")])
        slow = cf.FfmpegSource("gitee", "slow", [("b.zip", "https://gitee.example/b")])
        speeds = {"github": 50 * 1024 * 1024, "gitee": 1024 * 1024}
        with mock.patch.object(
            cf, "_probe_download_speed",
            side_effect=lambda url, timeout=5.0, cancel_event=None: speeds[
                "github" if "github" in url else "gitee"
            ],
        ):
            ranked = cf._probe_sources([fast, slow], None)
        self.assertEqual([source.key for _, source in ranked], ["github", "gitee"])

    def test_resume_uses_downloaded_archive_without_redownload(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            install_dir = Path(temp_dir)
            import zipfile as zip_module
            work_dir = install_dir / ".dl-gitee"
            work_dir.mkdir(parents=True)
            archive = work_dir / "ffmpeg.zip"
            with zip_module.ZipFile(archive, "w") as zip_file:
                zip_file.writestr("ffmpeg.exe", b"fake-ffmpeg")
                zip_file.writestr("ffprobe.exe", b"fake-ffprobe")
            source = cf.FfmpegSource("gitee", "gitee", [("ffmpeg.zip", "https://gitee.example/ffmpeg.zip")])
            with mock.patch.object(cf, "_download_archive", return_value=True) as download, \
                 mock.patch.object(cf, "ffmpeg_version_ok", return_value=True), \
                 mock.patch.object(cf, "say"):
                result = cf._install_from_source(
                    install_dir, source, None, None, "ffmpeg.exe", "ffprobe.exe"
                )
            self.assertTrue(result)
            download.assert_not_called()
            # after success the work dir (install packages) is deleted
            self.assertFalse(work_dir.exists())
            self.assertTrue((install_dir / "ffmpeg.exe").exists())


class UpdateFallbackTests(unittest.TestCase):
    def test_version_key_and_comparison(self):
        self.assertGreater(cf._version_key("v1.3.0"), cf._version_key("1.2.0"))
        self.assertGreater(cf._version_key("2.0.0-rc1"), cf._version_key("1.9.9"))
        self.assertFalse(cf._is_newer_version("1.2.0", "1.2.0"))
        self.assertTrue(cf._is_newer_version("v1.2.1", "1.2.0"))

    def test_update_json_is_the_normal_path(self):
        info = {
            "latest": "9.9.9",
            "published": "2026-01-01",
            "notes": ["第一条", "第二条"],
            "download_pages": ["https://gitee.example/", "https://github.example/"],
        }
        with mock.patch.object(cf, "_get_update_json_info", return_value=dict(info)) as json_info, \
             mock.patch.object(cf, "_get_latest_release_info") as release_info:
            result = cf.check_for_updates("1.2.0")
        self.assertEqual(result["latest"], "9.9.9")
        self.assertEqual(cf.update_notes_text(result), "1. 第一条\n2. 第二条")
        json_info.assert_called_once()
        release_info.assert_not_called()

    def test_release_fallback_when_update_json_fails(self):
        release = {
            "latest": "1.3.0",
            "published": "2026-02-01",
            "notes": ["fix: 修复BUG"],
            "download_pages": ["https://github.com/YCTS-otree/CodecFoundry/releases/tag/v1.3.0"],
            "from_release": True,
        }
        with mock.patch.object(cf, "_get_update_json_info", return_value=None), \
             mock.patch.object(cf, "_get_latest_release_info", return_value=dict(release)):
            result = cf.check_for_updates("1.2.0")
        self.assertIsNotNone(result)
        self.assertTrue(result["from_release"])

    def test_no_dialog_when_latest_is_not_newer(self):
        release = {
            "latest": "1.0.0",
            "notes": ["x"],
            "download_pages": ["https://example.com/"],
            "from_release": True,
        }
        with mock.patch.object(cf, "_get_update_json_info", return_value=None), \
             mock.patch.object(cf, "_get_latest_release_info", return_value=dict(release)):
            self.assertIsNone(cf.check_for_updates("1.2.0"))

    def test_broken_release_body_shows_fallback_notes(self):
        self.assertEqual(
            cf.update_notes_text({"notes": None, "latest": "1.3.0"}),
            cf.FALLBACK_RELEASE_NOTES,
        )
        self.assertEqual(
            cf.update_notes_text({"notes": [], "latest": "1.3.0"}),
            cf.FALLBACK_RELEASE_NOTES,
        )

    def test_offline_check_is_distinguished_from_up_to_date(self):
        with mock.patch.object(cf, "urlopen", side_effect=cf.URLError("offline")):
            self.assertIsNone(cf.check_for_updates("1.2.0"))
            self.assertFalse(cf.update_sources_reachable())

        class FakeResponse:
            def __enter__(self):
                return self

            def __exit__(self, *args):
                return False

            def read(self):
                return b'{"tag_name": "v1.2.0", "html_url": "https://example.com/"}'

        with mock.patch.object(cf, "urlopen", return_value=FakeResponse()):
            self.assertIsNone(cf.check_for_updates("1.2.0"))
            self.assertTrue(cf.update_sources_reachable())

    def test_latest_release_parses_tag_and_broken_body(self):
        payload = {
            "tag_name": "v1.4.0",
            "html_url": "https://github.com/YCTS-otree/CodecFoundry/releases/tag/v1.4.0",
            "body": "",
            "published_at": "2026-03-01T00:00:00Z",
        }
        results = {url: dict(payload) for url in cf.RELEASE_INFO_URLS}
        with mock.patch.object(cf, "_fetch_urls_race", return_value=results):
            info = cf._get_latest_release_info()
        self.assertEqual(info["latest"], "1.4.0")
        self.assertEqual(info["notes"], [cf.FALLBACK_RELEASE_NOTES])

    def test_update_json_race_prefers_github_when_reachable(self):
        github = {
            "latest": "2.0.0",
            "download_pages": ["https://github.example/x"],
            "notes": ["github note"],
        }
        gitee = {
            "latest": "1.5.0",
            "download_pages": ["https://gitee.example/x"],
            "notes": ["gitee note"],
        }
        results = {
            cf.UPDATE_INFO_URLS[0]: dict(github),
            cf.UPDATE_INFO_URLS[1]: dict(gitee),
        }
        with mock.patch.object(cf, "_fetch_urls_race", return_value=results):
            info = cf._get_update_json_info()
        self.assertEqual(info["latest"], "2.0.0")
        self.assertIn("github", info["source"])

    def test_update_json_falls_back_to_gitee_when_github_down(self):
        gitee = {
            "latest": "1.6.0",
            "download_pages": ["https://gitee.example/x"],
        }
        results = {cf.UPDATE_INFO_URLS[0]: None, cf.UPDATE_INFO_URLS[1]: dict(gitee)}
        with mock.patch.object(cf, "_fetch_urls_race", return_value=results):
            info = cf._get_update_json_info()
        self.assertEqual(info["latest"], "1.6.0")
        self.assertIn("gitee", info["source"])


class SchedulerReorderTests(unittest.TestCase):
    def setUp(self):
        self.scheduler = cf.LiveScheduler(cf.ProcessController())

    def test_reorder_waiting_moves_items_and_keeps_unknowns(self):
        first, second, third = (make_item("a.mp4"), make_item("b.mp4"), make_item("c.mp4"))
        self.scheduler.waiting = [first, second, third]
        self.scheduler.waiting_keys = {str(item.task.output) for item in self.scheduler.waiting}
        self.scheduler.reorder_waiting([str(Path("D:/out/c.mp4")), str(Path("D:/out/a.mp4"))])
        order = [str(item.task.output) for item in self.scheduler.waiting]
        expected = [str(Path("D:/out/c.mp4")), str(Path("D:/out/a.mp4")), str(Path("D:/out/b.mp4"))]
        self.assertEqual(order, expected)

    def test_reorder_ignores_non_waiting_keys(self):
        first, second = make_item("a.mp4"), make_item("b.mp4")
        self.scheduler.waiting = [first, second]
        self.scheduler.waiting_keys = {str(item.task.output) for item in self.scheduler.waiting}
        self.scheduler.reorder_waiting([str(Path("D:/out/missing.mp4")), str(Path("D:/out/b.mp4"))])
        order = [str(item.task.output) for item in self.scheduler.waiting]
        expected = [str(Path("D:/out/b.mp4")), str(Path("D:/out/a.mp4"))]
        self.assertEqual(order, expected)

    def test_take_next_respects_codec_and_order(self):
        hevc_item = make_item("a.mp4", codec="hevc")
        av1_item = make_item("b.mp4", codec="av1")
        self.scheduler.waiting = [av1_item, hevc_item]
        gpu = cf.GpuCapability(0, "GPU", 1, frozenset({"hevc", "av1"}), frozenset({"h264", "hevc", "av1"}))
        hevc_slot = cf.EncoderSlot(gpu, 0, codec="hevc")
        av1_slot = cf.EncoderSlot(gpu, 0, codec="av1")
        self.assertIs(self.scheduler._take_next_locked(hevc_slot), hevc_item)
        self.assertIs(self.scheduler._take_next_locked(av1_slot), av1_item)
        self.assertIsNone(self.scheduler._take_next_locked(hevc_slot))

    def test_plan_assignment_balances_load(self):
        tasks = [make_task(f"v{index}.mp4") for index in range(4)]
        gpu = cf.GpuCapability(0, "GPU", 2, frozenset({"hevc"}), frozenset({"h264", "hevc"}))
        slots = [
            cf.EncoderSlot(gpu, engine, codec="hevc", slot_id=engine)
            for engine in range(2)
        ]
        pairs = cf.plan_task_assignment(tasks, slots, make_settings())
        self.assertEqual(len(pairs), 4)
        per_slot = {}
        for _task, slot in pairs:
            per_slot[slot.slot_id] = per_slot.get(slot.slot_id, 0) + 1
        self.assertEqual(set(per_slot.values()), {2})

    def test_plan_assignment_preserves_intake_order_top_to_bottom(self):
        # Execution must follow the visible list top-to-bottom, so assignment
        # keeps the intake order instead of re-sorting by workload.
        small = make_task("a.mp4")
        small = cf.replace(small, info=cf.VideoInfo("hevc", 1280, 720, 30.0, duration=1.0, frame_count=30))
        huge = make_task("b.mp4")
        huge = cf.replace(huge, info=cf.VideoInfo("hevc", 3840, 2160, 30.0, duration=100.0, frame_count=3000))
        tasks = [small, huge]
        gpu = cf.GpuCapability(0, "GPU", 2, frozenset({"hevc"}), frozenset({"h264", "hevc"}))
        slots = [cf.EncoderSlot(gpu, engine, codec="hevc", slot_id=engine) for engine in range(2)]
        pairs = cf.plan_task_assignment(tasks, slots, make_settings())
        self.assertEqual([task.output.name for task, _ in pairs], ["a.mp4", "b.mp4"])

    def test_report_with_mixed_settings_writes_generic_header(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            folder = Path(temp_dir)
            task = cf.VideoTask(
                source=folder / "x.mp4",
                output=folder / "x_out.mp4",
                info=cf.VideoInfo("hevc", 1920, 1080, 30.0, duration=1.0, frame_count=30),
            )
            results = [(task, False, "失败：测试")]
            report = cf.write_compression_report(
                [task], results, None, None, cf.datetime.now(), cf.datetime.now()
            )
            self.assertTrue(report.is_file())
            self.assertIn("多种", report.read_text(encoding="utf-8-sig"))


class CliForwardTests(unittest.TestCase):
    def test_only_real_jobs_are_forwarded(self):
        self.assertTrue(cf._cli_request_can_forward(["--hlm", "D:/x.HLM"]))
        self.assertTrue(cf._cli_request_can_forward(["a.mp4", "b.mkv"]))
        self.assertFalse(cf._cli_request_can_forward(["--version"]))
        self.assertFalse(cf._cli_request_can_forward(["--doctor"]))
        self.assertFalse(cf._cli_request_can_forward(["--dry-run", "a.mp4"]))
        self.assertFalse(cf._cli_request_can_forward(["--help"]))

    def test_ipc_roundtrip_delivers_payload(self):
        import threading

        unique_name = f"CodecFoundry-test-{os.getpid()}"
        received: list[str] = []
        with mock.patch.object(cf, "SINGLE_INSTANCE_SERVER_NAME", unique_name):
            server, forwarded = cf.acquire_single_instance([])
            if server is None:
                self.skipTest("sandbox blocks named pipes; IPC covered by logic tests")
            self.assertFalse(forwarded)

            sockets: list[object] = []

            def handle_connection() -> None:
                socket = server.nextPendingConnection()
                sockets.append(socket)

                def read_payload() -> None:
                    raw = bytes(socket.readAll())
                    if not raw.strip():
                        return
                    socket.write(b"ok")
                    socket.flush()
                    socket.disconnectFromServer()
                    data = json.loads(raw.decode("utf-8"))
                    received.extend(str(item) for item in data.get("argv", []))

                socket.readyRead.connect(read_payload)

            server.newConnection.connect(handle_connection)
            app = cf.QApplication.instance() or cf.QApplication([])
            # Write without waitFor*: their return values are unreliable under
            # this sandbox, but the server-side processing is deterministic.
            client = cf.QLocalSocket()
            client.connectToServer(unique_name)
            payload = json.dumps(
                {"argv": ["--hlm", "D:/forwarded.HLM", "--codec", "av1"]}
            ).encode("utf-8")
            client.write(payload)
            client.flush()
            deadline = cf.time.time() + 4
            while not received and cf.time.time() < deadline:
                app.processEvents()
                cf.time.sleep(0.01)
            client.disconnectFromServer()
            client.deleteLater()
            server.close()
            server.deleteLater()
        self.assertEqual(received, ["--hlm", "D:/forwarded.HLM", "--codec", "av1"])

    def test_forward_requires_acknowledgement(self):
        fake_socket = mock.Mock()
        fake_socket.waitForConnected.return_value = True
        fake_socket.waitForBytesWritten.return_value = True
        fake_socket.waitForReadyRead.return_value = True
        fake_socket.readAll.return_value = b"ok"
        with mock.patch.object(cf, "QLocalSocket", return_value=fake_socket):
            self.assertTrue(cf._forward_to_running_instance(["--hlm", "D:/x.HLM"]))
        fake_socket.readAll.return_value = b""
        with mock.patch.object(cf, "QLocalSocket", return_value=fake_socket):
            self.assertFalse(cf._forward_to_running_instance(["--hlm", "D:/x.HLM"]))


class GuiReorderTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app = cf.QApplication.instance() or cf.QApplication([])

    def test_card_reorder_updates_visible_and_real_order(self):
        window = cf.CodecFoundryWindow()
        window.waiting_order = ["A", "B", "C"]
        fake_scheduler = mock.Mock()
        window.scheduler = fake_scheduler
        try:
            window._handle_card_reorder("C", "A")
        finally:
            _close_window(window)
        self.assertEqual(window.waiting_order, ["C", "A", "B"])
        fake_scheduler.reorder_waiting.assert_called_once_with(["C", "A", "B"])

    def test_card_reorder_ignores_unknown_dragged_key(self):
        window = cf.CodecFoundryWindow()
        window.waiting_order = ["A", "B"]
        fake_scheduler = mock.Mock()
        window.scheduler = fake_scheduler
        try:
            window._handle_card_reorder("MISSING", "A")
        finally:
            _close_window(window)
        self.assertEqual(window.waiting_order, ["A", "B"])
        fake_scheduler.reorder_waiting.assert_not_called()

    def test_task_card_frame_starts_long_press_drag_only_when_draggable(self):
        frame = cf.TaskCardFrame()
        frame.set_task_key("K")
        frame.set_draggable(False)
        self.assertFalse(frame._draggable)
        frame.set_draggable(True)
        self.assertTrue(frame._draggable)
        frame.deleteLater()

    def test_begin_card_drag_only_accepts_waiting_tasks(self):
        window = cf.CodecFoundryWindow()
        try:
            source = str(Path("D:/video/source.mp4"))
            record = {
                "source": source,
                "output": str(Path("D:/out/a.mp4")),
                "filename": "a.mp4",
            }
            window._create_task_card(0, record, "waiting")
            waiting_key = record["output"]
            window.waiting_order = [waiting_key]
            self.assertTrue(window.begin_card_drag(waiting_key))
            self.assertEqual(window._active_drag_key, waiting_key)
            window.on_card_drag_finished(waiting_key)
            # running cards cannot be dragged
            window._update_task_card(waiting_key, "running", 0)
            self.assertFalse(window.begin_card_drag(waiting_key))
        finally:
            _close_window(window)

    def test_drag_moved_live_reorders_waiting_order(self):
        window = cf.CodecFoundryWindow()
        try:
            window.resize(1500, 900)
            window.show()
            cf.QApplication.instance().processEvents()
            source = str(Path("D:/video/source.mp4"))
            keys = []
            for index in range(3):
                record = {
                    "source": source,
                    "output": str(Path(f"D:/out/{index}.mp4")),
                    "filename": f"f{index}.mp4",
                }
                window._create_task_card(0, record, "waiting")
                keys.append(record["output"])
            window.waiting_order = list(keys)
            cf.QApplication.instance().processEvents()
            window.begin_card_drag(keys[0])
            first_center = window.task_widgets[keys[1]]["frame"].mapToGlobal(
                window.task_widgets[keys[1]]["frame"].rect().center()
            ).y()
            second_center = window.task_widgets[keys[2]]["frame"].mapToGlobal(
                window.task_widgets[keys[2]]["frame"].rect().center()
            ).y()
            midpoint = (first_center + second_center) // 2
            window.on_card_drag_moved(keys[0], midpoint)
            self.assertEqual(window.waiting_order, [keys[1], keys[0], keys[2]])
            window.on_card_drag_finished(keys[0])
            self.assertEqual(window.waiting_order, [keys[1], keys[0], keys[2]])
        finally:
            _close_window(window)

    def test_ffmpeg_setup_window_shows_manual_buttons_on_failure(self):
        window = cf.FfmpegSetupWindow()
        try:
            window._on_progress(
                {"stage": "download", "name": "x.zip", "done": 50, "total": 100,
                 "speed": 1024 * 1024, "eta": 5.0}
            )
            first_bar = next(iter(window._bars.values()))["bar"]
            self.assertIn("50%", first_bar.text())
            window._on_done({"error": "安装失败", "install_dir": "D:/x"})
            self.assertTrue(window._failed)
            self.assertFalse(window.manual_frame.isHidden())
        finally:
            window.deleteLater()

    def test_ffmpeg_setup_window_two_bars_for_two_archives(self):
        window = cf.FfmpegSetupWindow()
        try:
            window._on_progress(
                {"stage": "download", "name": "ffmpeg.zip", "done": 10, "total": 100,
                 "speed": 1024 * 1024, "eta": 3.0}
            )
            window._on_progress(
                {"stage": "download", "name": "ffprobe.zip", "done": 20, "total": 100,
                 "speed": 2048 * 1024, "eta": 2.0}
            )
            self.assertEqual(len(window._bars), 2)
        finally:
            window.deleteLater()

    def test_setup_window_close_cancels_install(self):
        window = cf.FfmpegSetupWindow()
        cancel_event = cf.threading.Event()
        try:
            window.set_cancel_event(cancel_event)
            window.set_install_running(True)
            window.close()
            self.assertTrue(cancel_event.is_set())
        finally:
            window.deleteLater()

    def test_single_instance_preference_defaults_enabled(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            data_dir = Path(temp_dir) / "data"
            data_dir.mkdir()
            (data_dir / "settings.json").write_text(
                json.dumps({"single_instance": False}), encoding="utf-8"
            )
            with mock.patch.object(cf, "APP_SETTINGS_PATH", data_dir / "settings.json"):
                self.assertFalse(cf.single_instance_preferred())
            with mock.patch.object(cf, "APP_SETTINGS_PATH", data_dir / "missing.json"):
                self.assertTrue(cf.single_instance_preferred())


class StartupWindowTests(unittest.TestCase):
    def test_ffmpeg_setup_window_construction(self):
        app = cf.QApplication.instance() or cf.QApplication([])
        window = cf.FfmpegSetupWindow()
        self.assertIsNotNone(window.bar_host)
        self.assertIsNotNone(window.detail_label)
        self.assertIsNotNone(window.background_button)
        self.assertIsNotNone(window.close_button)
        window.deleteLater()


class FramelessResizeTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app = cf.QApplication.instance() or cf.QApplication([])

    @staticmethod
    def _mouse_event(kind, global_pos: cf.QPoint):
        from PySide6.QtGui import QMouseEvent
        return QMouseEvent(
            kind,
            cf.QPoint(0, 0),
            global_pos,
            cf.Qt.MouseButton.LeftButton,
            cf.Qt.MouseButton.LeftButton,
            cf.Qt.KeyboardModifier.NoModifier,
        )

    def test_resize_edge_detection_is_only_3px_border(self):
        window = cf.CodecFoundryWindow()
        try:
            window.resize(1000, 800)
            width, height = window.rect().width(), window.rect().height()
            self.assertEqual(window._resize_edge_at(cf.QPoint(2, height // 2)), "l")
            self.assertEqual(window._resize_edge_at(cf.QPoint(width - 2, height // 2)), "r")
            self.assertEqual(window._resize_edge_at(cf.QPoint(width // 2, 2)), "t")
            self.assertEqual(window._resize_edge_at(cf.QPoint(width // 2, height - 2)), "b")
            self.assertEqual(window._resize_edge_at(cf.QPoint(2, 2)), "lt")
            self.assertEqual(window._resize_edge_at(cf.QPoint(width - 2, height - 2)), "rb")
            # Beyond the 3px border (or inside the window): no resize zone at all
            self.assertIsNone(window._resize_edge_at(cf.QPoint(6, height // 2)))
            self.assertIsNone(window._resize_edge_at(cf.QPoint(width - 6, height // 2)))
            self.assertIsNone(window._resize_edge_at(cf.QPoint(width // 2, 6)))
            self.assertIsNone(window._resize_edge_at(cf.QPoint(width // 2, height - 6)))
            self.assertIsNone(window._resize_edge_at(cf.QPoint(width // 2, height // 2)))
        finally:
            _close_window(window)

    def test_press_on_edge_starts_resize_and_resizes_on_move(self):
        window = cf.CodecFoundryWindow()
        try:
            window.resize(1000, 800)
            window.show()
            self.app.processEvents()
            rect = window.geometry()
            right_edge = window.mapToGlobal(cf.QPoint(rect.width() - 2, rect.height() // 2))
            press = self._mouse_event(cf.QEvent.Type.MouseButtonPress, right_edge)
            self.assertTrue(window.eventFilter(window, press))
            self.assertEqual(window._resize_edge, "r")
            moved = self._mouse_event(cf.QEvent.Type.MouseMove, right_edge + cf.QPoint(40, 0))
            self.assertTrue(window.eventFilter(window, moved))
            self.assertEqual(window.width(), rect.width() + 40)
            release = self._mouse_event(cf.QEvent.Type.MouseButtonRelease, right_edge + cf.QPoint(40, 0))
            self.assertTrue(window.eventFilter(window, release))
            self.assertIsNone(window._resize_edge)
        finally:
            _close_window(window)

    def test_press_inside_window_never_resizes(self):
        window = cf.CodecFoundryWindow()
        try:
            window.resize(1000, 800)
            window.show()
            self.app.processEvents()
            rect = window.geometry()
            center = window.mapToGlobal(cf.QPoint(rect.width() // 2, rect.height() // 2))
            press = self._mouse_event(cf.QEvent.Type.MouseButtonPress, center)
            self.assertFalse(window.eventFilter(window, press))
            self.assertIsNone(window._resize_edge)
        finally:
            _close_window(window)

    def test_minimum_size_is_respected_while_resizing(self):
        window = cf.CodecFoundryWindow()
        try:
            window.resize(1000, 800)
            window.show()
            self.app.processEvents()
            rect = window.geometry()
            left_edge = window.mapToGlobal(cf.QPoint(2, rect.height() // 2))
            press = self._mouse_event(cf.QEvent.Type.MouseButtonPress, left_edge)
            window.eventFilter(window, press)
            self.assertEqual(window._resize_edge, "l")
            moved = self._mouse_event(cf.QEvent.Type.MouseMove, left_edge + cf.QPoint(5000, 0))
            window.eventFilter(window, moved)
            self.assertGreaterEqual(window.width(), window.minimumWidth())
            release = self._mouse_event(cf.QEvent.Type.MouseButtonRelease, left_edge)
            window.eventFilter(window, release)
        finally:
            _close_window(window)

    def test_press_over_foreign_popup_does_not_start_resize(self):
        window = cf.CodecFoundryWindow()
        try:
            window.resize(1000, 800)
            window.show()
            self.app.processEvents()
            rect = window.geometry()
            right_edge = window.mapToGlobal(cf.QPoint(rect.width() - 2, rect.height() // 2))
            foreign = cf.QWidget()  # a real top-level (dialog-like) widget
            self.assertIs(foreign.window(), foreign)
            press = self._mouse_event(cf.QEvent.Type.MouseButtonPress, right_edge)
            with mock.patch.object(cf.QApplication, "widgetAt", return_value=foreign):
                self.assertFalse(window.eventFilter(window, press))
            self.assertIsNone(window._resize_edge)
            foreign.deleteLater()
        finally:
            _close_window(window)

    def test_press_over_scrollbar_like_child_still_resizes(self):
        # Even when a scrollbar sits under the right border, the press must
        # start the resize (regression: right edge dead in the middle band).
        window = cf.CodecFoundryWindow()
        try:
            window.resize(1400, 900)
            window.show()
            self.app.processEvents()
            rect = window.geometry()
            right_edge = window.mapToGlobal(cf.QPoint(rect.width() - 2, rect.height() // 2))
            child = mock.Mock()
            child.window.return_value = window  # any child of the main window
            press = self._mouse_event(cf.QEvent.Type.MouseButtonPress, right_edge)
            with mock.patch.object(cf.QApplication, "widgetAt", return_value=child):
                self.assertTrue(window.eventFilter(window, press))
            self.assertEqual(window._resize_edge, "r")
            release = self._mouse_event(cf.QEvent.Type.MouseButtonRelease, right_edge)
            window.eventFilter(window, release)
        finally:
            _close_window(window)

    def test_splitter_handle_stays_draggable(self):
        window = cf.CodecFoundryWindow()
        try:
            window.resize(1500, 900)
            window.show()
            self.app.processEvents()
            # The sidebar width is bounded but never locked: min != max.
            self.assertEqual(window.task_sidebar.minimumWidth(), 360)
            self.assertEqual(window.task_sidebar.maximumWidth(), 760)
            handle = window.main_splitter.handle(1)
            self.assertIsNotNone(handle)
            # A manual "drag" width is preserved by the responsive layout pass:
            # the splitter scales the requested sizes to the available width,
            # but must NOT reset the sidebar back to its initial default.
            window.main_splitter.setSizes([900, 500])
            window._apply_responsive_layout()
            dragged_sizes = list(window.main_splitter.sizes())
            window._apply_responsive_layout()
            self.assertEqual(list(window.main_splitter.sizes()), dragged_sizes)
            self.assertGreater(dragged_sizes[1], 360)
        finally:
            _close_window(window)

    def test_safe_exit_releases_ipc_slot_and_starts_watchdog(self):
        window = cf.CodecFoundryWindow()
        try:
            server = cf.QLocalServer()
            server.listen("CodecFoundry-test-exit-" + str(os.getpid()))
            window.ipc_server = server
            window._begin_safe_exit(running=False)
            self.assertTrue(window.closing)
            self.assertIsNone(window.ipc_server)
            self.assertFalse(server.isListening())
            self.assertIsNotNone(getattr(window, "exit_watchdog", None))
            self.assertTrue(window.close_finalized)
        finally:
            _close_window(window)

    def test_external_activation_ignored_while_closing(self):
        window = cf.CodecFoundryWindow()
        try:
            window.closing = True
            window.hide()
            window._activate_from_external()
            self.assertFalse(window.isVisible())
        finally:
            _close_window(window)

    def test_drop_indicator_shown_during_card_drag(self):
        window = cf.CodecFoundryWindow()
        try:
            source = str(Path("D:/video/source.mp4"))
            keys = []
            for index in range(3):
                record = {
                    "source": source,
                    "output": str(Path(f"D:/out/{index}.mp4")),
                    "filename": f"f{index}.mp4",
                }
                window._create_task_card(0, record, "waiting")
                keys.append(record["output"])
            window.waiting_order = list(keys)
            window.begin_card_drag(keys[0])
            window.on_card_drag_started(keys[0])
            self.assertIsNotNone(window._drop_indicator)
            self.assertGreaterEqual(window.task_layout.indexOf(window._drop_indicator), 0)
            window.on_card_drag_finished(keys[0])
            self.assertEqual(window.task_layout.indexOf(window._drop_indicator), -1)
        finally:
            _close_window(window)


if __name__ == "__main__":
    unittest.main()
