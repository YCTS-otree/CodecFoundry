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

    def test_ensure_ffmpeg_runtime_raises_when_nothing_works(self):
        cf._ffmpeg_runtime_cache = None
        with mock.patch.object(cf, "ffmpeg_version_ok", return_value=False), \
             mock.patch.object(cf, "_install_ffmpeg_binaries", return_value=False), \
             mock.patch.object(cf.shutil, "which", return_value=None):
            with self.assertRaises(cf.TranscodeError):
                cf.ensure_ffmpeg_runtime()
        cf._ffmpeg_runtime_cache = None


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
        with mock.patch.object(
            cf, "_fetch_update_json", return_value=dict(payload)
        ) as fetch:
            info = cf._get_latest_release_info()
        self.assertEqual(info["latest"], "1.4.0")
        self.assertEqual(info["notes"], [cf.FALLBACK_RELEASE_NOTES])
        fetch.assert_called_once()


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
            window.app_logger.close()
            window.close_finalized = True
            window.close()
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
            window.app_logger.close()
            window.close_finalized = True
            window.close()
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


if __name__ == "__main__":
    unittest.main()
