#!/usr/bin/env python3
"""CodecFoundry v1.2.0: single-file PySide6 NVENC transcoder.

The backend and desktop UI intentionally live in this one source file.  The GPU
capability database remains external data: driver/runtime discovery tells us which
GPUs exist, while ``gpus.json`` states how many independent encoding jobs the
operator wants to run and which codecs each GPU can encode/decode.
"""

from __future__ import annotations

import argparse
import csv
import ctypes
from ctypes import wintypes
import json
import logging
from logging.handlers import RotatingFileHandler
import math
import os
import queue
import re
import signal
import shlex
import shutil
import subprocess
import sys
import tempfile
import threading
import time
from collections import deque
from datetime import datetime
from dataclasses import dataclass, field, replace
from pathlib import Path
from typing import Iterable, Sequence


def windows_creation_flags(*, new_process_group: bool = False) -> int:
    """Return Windows child-process flags that never allocate a console window."""
    if os.name != "nt":
        return 0
    flags = getattr(subprocess, "CREATE_NO_WINDOW", 0)
    if new_process_group:
        flags |= getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0)
    return flags


def windows_console_window() -> int | None:
    """Return this process' console HWND, if Windows attached one."""
    if os.name != "nt" or not hasattr(ctypes, "windll"):
        return None
    try:
        get_console_window = ctypes.windll.kernel32.GetConsoleWindow
        get_console_window.restype = wintypes.HWND
        return get_console_window() or None
    except (AttributeError, OSError):
        return None


def relaunch_gui_with_pythonw() -> bool:
    """Hand a console-hosted GUI launch to pythonw.exe when it is available."""
    if not windows_console_window():
        return False
    executable = Path(sys.executable)
    if executable.name.casefold() != "python.exe":
        return False
    pythonw = executable.with_name("pythonw.exe")
    if not pythonw.is_file():
        return False
    try:
        subprocess.Popen(
            [str(pythonw), str(Path(__file__).resolve()), *sys.argv[1:]],
            stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            close_fds=True,
            creationflags=(
                getattr(subprocess, "DETACHED_PROCESS", 0)
                | getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0)
            ),
        )
    except OSError:
        return False
    return True


def detach_windows_console_for_gui() -> None:
    """Hide and release an accidentally attached console for GUI operation."""
    console_window = windows_console_window()
    if not console_window:
        return
    try:
        ctypes.windll.user32.ShowWindow(console_window, 0)  # SW_HIDE
        ctypes.windll.kernel32.FreeConsole()
    except (AttributeError, OSError):
        return
    # Prevent print() and libraries from writing through invalid console handles.
    sys.stdin = None
    sys.stdout = None
    sys.stderr = None


# Do this before importing PySide6 or discovering hardware.  A .pyw association
# may still point at python.exe on a user's machine; handing off early prevents its
# temporary console from lingering for the lifetime of the GUI application.
if __name__ == "__main__" and not ({"--cli", "--version"} & set(sys.argv)):
    if relaunch_gui_with_pythonw():
        raise SystemExit(0)
    detach_windows_console_for_gui()


VIDEO_EXTENSIONS = {
    ".3gp", ".avi", ".flv", ".m2ts", ".m4v", ".mkv", ".mov", ".mp4",
    ".mpeg", ".mpg", ".mts", ".ts", ".webm", ".wmv",
}
CODECFOUNDRY_VERSION = "1.2.0"
HLM_FORMAT = "FlashCut Highlight Markers"
SUPPORTED_HLM_VERSION = 2
CODEC_ALIASES = {
    "av01": "av1",
    "avc": "h264",
    "avc1": "h264",
    "h265": "hevc",
    "hev1": "hevc",
    "hvc1": "hevc",
    "mpeg2video": "mpeg2",
}
PRINT_LOCK = threading.Lock()
OUTPUT_CALLBACK = None
EVENT_CALLBACK = None
REFERENCE_PIXELS = 1920 * 1080


class TranscodeError(RuntimeError):
    """An expected configuration, probing, or transcoding error."""


def run_captured_text(command: Sequence[str]) -> subprocess.CompletedProcess[str]:
    """Run a tool whose redirected output is UTF-8, regardless of Windows locale."""
    return subprocess.run(
        command,
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        check=False,
        creationflags=windows_creation_flags(),
    )


@dataclass(frozen=True)
class GpuCapability:
    index: int
    name: str
    encoder_engines: int
    encode: frozenset[str]
    decode: frozenset[str]
    uuid: str | None = None

    def can_encode(self, codec: str) -> bool:
        return normalize_codec(codec) in self.encode

    def can_decode(self, codec: str) -> bool:
        return normalize_codec(codec) in self.decode


@dataclass(frozen=True)
class VideoInfo:
    codec: str
    width: int
    height: int
    fps: float | None
    bitrate: int | None = None
    duration: float | None = None
    frame_count: int | None = None


@dataclass(frozen=True)
class VideoTask:
    source: Path
    output: Path
    info: VideoInfo
    skip_reason: str | None = None
    overwrite_existing: bool = False
    replacement_reason: str | None = None
    clip_start: float | None = None
    clip_duration: float | None = None
    source_duration: float | None = None
    display_name: str | None = None
    external_id: str | None = None

    @property
    def is_clip(self) -> bool:
        return self.clip_start is not None or self.clip_duration is not None

    @property
    def label(self) -> str:
        return self.display_name or self.source.name


@dataclass(frozen=True)
class FileTimestamps:
    """Filesystem timestamps captured before FFmpeg creates the output file."""

    accessed_ns: int
    modified_ns: int
    created_ns: int | None


@dataclass
class EncoderSlot:
    gpu: GpuCapability
    engine: int
    tasks: list[VideoTask] = field(default_factory=list)
    cpu_ids: tuple[int, ...] = ()
    slot_id: int = -1


@dataclass(frozen=True)
class EncodeSettings:
    codec: str
    maxrate: int | None
    bufsize: int | None
    fps_text: str | None
    fps_value: float | None
    resolution: tuple[int, int] | None
    cq: float
    preset: str
    lookahead: int
    multipass: str
    overwrite: bool
    copy_subtitles: bool


class ProcessController:
    """Own all active FFmpeg processes so one cancellation stops the whole batch."""

    def __init__(self) -> None:
        self.cancel_event = threading.Event()
        self._lock = threading.Lock()
        self._processes: dict[int, subprocess.Popen[str]] = {}
        self._process_tasks: dict[int, str] = {}
        self._cancelled_tasks: set[str] = set()

    @property
    def cancelled(self) -> bool:
        return self.cancel_event.is_set()

    def register(
        self,
        process: subprocess.Popen[str],
        task_key: str | None = None,
    ) -> None:
        with self._lock:
            self._processes[process.pid] = process
            if task_key is not None:
                self._process_tasks[process.pid] = task_key
            already_cancelled = self.cancel_event.is_set() or (
                task_key is not None and task_key in self._cancelled_tasks
            )
        if already_cancelled:
            self._force_stop(process)

    def unregister(self, process: subprocess.Popen[str]) -> None:
        with self._lock:
            self._processes.pop(process.pid, None)
            self._process_tasks.pop(process.pid, None)

    def task_cancelled(self, task_key: str) -> bool:
        with self._lock:
            return task_key in self._cancelled_tasks

    def cancel_task(self, task_key: str, grace_seconds: float = 1.0) -> None:
        """Cancel one queued or active task without stopping the rest of the batch."""

        with self._lock:
            self._cancelled_tasks.add(task_key)
            processes = [
                process
                for pid, process in self._processes.items()
                if self._process_tasks.get(pid) == task_key
            ]
        for process in processes:
            self._request_stop(process)
        deadline = time.monotonic() + max(0.0, grace_seconds)
        while time.monotonic() < deadline and any(
            process.poll() is None for process in processes
        ):
            time.sleep(0.05)
        for process in processes:
            if process.poll() is None:
                self._force_stop(process)

    def active_count(self) -> int:
        with self._lock:
            return sum(process.poll() is None for process in self._processes.values())

    def cancel(self, grace_seconds: float = 1.0) -> None:
        self.cancel_event.set()
        with self._lock:
            processes = list(self._processes.values())
        for process in processes:
            self._request_stop(process)
        deadline = time.monotonic() + max(0.0, grace_seconds)
        while time.monotonic() < deadline and any(process.poll() is None for process in processes):
            time.sleep(0.05)
        for process in processes:
            if process.poll() is None:
                self._force_stop(process)

    @staticmethod
    def _request_stop(process: subprocess.Popen[str]) -> None:
        if process.poll() is not None:
            return
        try:
            if os.name == "nt" and hasattr(signal, "CTRL_BREAK_EVENT"):
                process.send_signal(signal.CTRL_BREAK_EVENT)
            elif os.name != "nt":
                os.killpg(process.pid, signal.SIGTERM)
            else:
                process.terminate()
        except (OSError, ProcessLookupError):
            pass

    @staticmethod
    def _force_stop(process: subprocess.Popen[str]) -> None:
        if process.poll() is not None:
            return
        try:
            if os.name == "nt":
                subprocess.run(
                    ["taskkill", "/PID", str(process.pid), "/T", "/F"],
                    stdout=subprocess.DEVNULL,
                    stderr=subprocess.DEVNULL,
                    check=False,
                    timeout=5,
                    creationflags=windows_creation_flags(),
                )
                # taskkill may be unavailable or denied in restricted shells. Popen.kill
                # is the final guaranteed fallback for the actual FFmpeg process.
                if process.poll() is None:
                    process.kill()
            else:
                os.killpg(process.pid, signal.SIGKILL)
        except (OSError, ProcessLookupError, subprocess.TimeoutExpired):
            try:
                process.kill()
            except OSError:
                pass


def say(message: str) -> None:
    with PRINT_LOCK:
        if OUTPUT_CALLBACK is not None:
            OUTPUT_CALLBACK(message)
        else:
            print(message, flush=True)


def set_output_callback(callback) -> None:
    """Route status lines to a GUI-safe callback; pass None to restore stdout."""
    global OUTPUT_CALLBACK
    OUTPUT_CALLBACK = callback


def set_event_callback(callback) -> None:
    """Route structured lifecycle/progress events to the GUI."""
    global EVENT_CALLBACK
    EVENT_CALLBACK = callback


def emit_event(event_type: str, **payload: object) -> None:
    if EVENT_CALLBACK is None:
        return
    try:
        EVENT_CALLBACK(event_type, payload)
    except Exception:
        # A presentation callback must never break an encode.
        pass


def warn(message: str) -> None:
    say(f"[警告] {message}")


def normalize_codec(codec: str) -> str:
    value = codec.strip().lower()
    return CODEC_ALIASES.get(value, value)


def parse_support(value: object, field_name: str) -> frozenset[str]:
    if isinstance(value, list):
        if not all(isinstance(item, str) for item in value):
            raise TranscodeError(f"{field_name} 列表中只能包含字符串")
        return frozenset(normalize_codec(item) for item in value)
    if isinstance(value, dict):
        return frozenset(
            normalize_codec(str(codec)) for codec, supported in value.items() if supported
        )
    raise TranscodeError(f"{field_name} 必须是编解码器列表或布尔值字典")


def load_gpu_config(path: Path) -> list[GpuCapability]:
    try:
        raw = json.loads(path.read_text(encoding="utf-8-sig"))
    except FileNotFoundError as exc:
        raise TranscodeError(f"找不到 GPU 配置：{path}") from exc
    except (OSError, json.JSONDecodeError) as exc:
        raise TranscodeError(f"无法读取 GPU 配置 {path}：{exc}") from exc

    entries = raw.get("gpus") if isinstance(raw, dict) else None
    if not isinstance(entries, list) or not entries:
        raise TranscodeError("GPU 配置必须包含非空的 gpus 数组")

    result: list[GpuCapability] = []
    seen_indices: set[int] = set()
    for number, entry in enumerate(entries, 1):
        if not isinstance(entry, dict):
            raise TranscodeError(f"gpus 第 {number} 项不是对象")
        try:
            index = int(entry["index"])
            name = str(entry["name"])
            engines = int(entry["encoder_engines"])
            encode = parse_support(entry["encode"], f"gpus[{number}].encode")
            decode = parse_support(entry["decode"], f"gpus[{number}].decode")
        except KeyError as exc:
            raise TranscodeError(f"gpus 第 {number} 项缺少字段：{exc.args[0]}") from exc
        except (TypeError, ValueError) as exc:
            raise TranscodeError(f"gpus 第 {number} 项字段类型错误：{exc}") from exc
        if index < 0 or index in seen_indices:
            raise TranscodeError(f"GPU index 必须为不重复的非负整数：{index}")
        if engines < 0:
            raise TranscodeError(f"GPU {index} 的 encoder_engines 不能为负数")
        if engines == 0 and encode:
            raise TranscodeError(f"GPU {index} 声明支持编码，但 encoder_engines 为 0")
        if not encode.issubset({"hevc", "av1"}):
            invalid = ", ".join(sorted(encode - {"hevc", "av1"}))
            raise TranscodeError(f"GPU {index} 配置了不支持的输出编码：{invalid}")
        seen_indices.add(index)
        result.append(
            GpuCapability(
                index=index,
                name=name,
                encoder_engines=engines,
                encode=encode,
                decode=decode,
                uuid=str(entry["uuid"]) if entry.get("uuid") else None,
            )
        )
    return result


def detect_nvidia_gpus(executable: str) -> dict[int, tuple[str, str]]:
    command = [
        executable,
        "--query-gpu=index,name,uuid",
        "--format=csv,noheader,nounits",
    ]
    try:
        completed = run_captured_text(command)
    except OSError as exc:
        raise TranscodeError(f"无法运行 nvidia-smi：{exc}") from exc
    if completed.returncode != 0:
        detail = completed.stderr.strip() or completed.stdout.strip()
        raise TranscodeError(f"nvidia-smi 执行失败：{detail}")

    detected: dict[int, tuple[str, str]] = {}
    for row in csv.reader(completed.stdout.splitlines(), skipinitialspace=True):
        if len(row) < 3:
            continue
        try:
            detected[int(row[0].strip())] = (row[1].strip(), row[2].strip())
        except ValueError:
            continue
    if not detected:
        raise TranscodeError("nvidia-smi 没有返回任何 NVIDIA GPU")
    return detected


def validate_configured_gpus(
    configured: Sequence[GpuCapability],
    detected: dict[int, tuple[str, str]],
    selected_indices: set[int] | None,
) -> list[GpuCapability]:
    def normalized_name(value: str) -> str:
        return re.sub(r"[^a-z0-9]+", "", value.casefold()).removeprefix("nvidia")

    usable: list[GpuCapability] = []
    for actual_index, (actual_name, actual_uuid) in sorted(detected.items()):
        if selected_indices is not None and actual_index not in selected_indices:
            continue
        uuid_matches = [
            gpu for gpu in configured
            if gpu.uuid and gpu.uuid.casefold() == actual_uuid.casefold()
        ]
        name_matches = [
            gpu for gpu in configured
            if normalized_name(gpu.name) == normalized_name(actual_name)
        ]
        matches = uuid_matches or name_matches
        if not matches:
            # Permit a unique shortened/custom name without confusing e.g. 4080 and 4080 SUPER.
            actual_normalized = normalized_name(actual_name)
            partial = [
                gpu for gpu in configured
                if normalized_name(gpu.name) in actual_normalized
                or actual_normalized in normalized_name(gpu.name)
            ]
            matches = partial if len(partial) == 1 else []
        if not matches:
            warn(f"实际 GPU {actual_index}（{actual_name}）在能力字典中没有唯一匹配项")
            continue
        if len(matches) > 1:
            raise TranscodeError(f"GPU 型号“{actual_name}”在能力字典中重复")
        capability = matches[0]
        usable.append(
            GpuCapability(
                index=actual_index,
                name=actual_name,
                encoder_engines=capability.encoder_engines,
                encode=capability.encode,
                decode=capability.decode,
                uuid=actual_uuid,
            )
        )
    if selected_indices is not None:
        missing = selected_indices - {gpu.index for gpu in usable}
        if missing:
            raise TranscodeError(f"指定的 GPU 不可用或不在配置中：{sorted(missing)}")
    if not usable:
        actual_names = ", ".join(f"{index}:{name}" for index, (name, _) in detected.items())
        raise TranscodeError(f"GPU 能力字典与当前检测结果没有可用交集；实际设备：{actual_names}")
    return usable


_BITRATE_RE = re.compile(r"^\s*(\d+(?:\.\d+)?)\s*([kKmMgG]?)\s*(?:[bB](?:it)?(?:/s|ps)?)?\s*$")


def parse_bitrate(text: str) -> int:
    match = _BITRATE_RE.match(text)
    if not match:
        raise argparse.ArgumentTypeError(f"无效码率“{text}”，示例：8000k、8M、0.02G")
    number = float(match.group(1))
    multiplier = {"": 1, "k": 1_000, "m": 1_000_000, "g": 1_000_000_000}[match.group(2).lower()]
    value = int(round(number * multiplier))
    if value <= 0:
        raise argparse.ArgumentTypeError("码率必须大于 0")
    return value


def format_bitrate(value: int) -> str:
    if value >= 1_000_000:
        return f"{value / 1_000_000:.3g}M"
    if value >= 1_000:
        return f"{value / 1_000:.3g}k"
    return str(value)


def parse_resolution(text: str) -> tuple[int, int]:
    match = re.fullmatch(r"\s*(\d+)\s*[xX×]\s*(\d+)\s*", text)
    if not match:
        raise argparse.ArgumentTypeError("分辨率格式应为 宽x高，例如 1920x1080")
    width, height = int(match.group(1)), int(match.group(2))
    if width < 2 or height < 2 or width % 2 or height % 2:
        raise argparse.ArgumentTypeError("NVENC 4:2:0 输出要求宽高为不小于 2 的偶数")
    return width, height


def parse_fps(text: str) -> tuple[str, float]:
    value = text.strip()
    try:
        if "/" in value:
            numerator_text, denominator_text = value.split("/", 1)
            numerator, denominator = float(numerator_text), float(denominator_text)
            fps = numerator / denominator
        else:
            fps = float(value)
    except (ValueError, ZeroDivisionError) as exc:
        raise argparse.ArgumentTypeError("帧率应为数字或分数，例如 30、59.94、60000/1001") from exc
    if not math.isfinite(fps) or fps <= 0 or fps > 1000:
        raise argparse.ArgumentTypeError("帧率必须在 0 到 1000 之间")
    return value, fps


def ratio_to_float(value: str | None) -> float | None:
    if not value or value in {"0/0", "N/A"}:
        return None
    try:
        numerator, denominator = value.split("/", 1)
        result = float(numerator) / float(denominator)
    except (ValueError, ZeroDivisionError):
        return None
    return result if math.isfinite(result) and result > 0 else None


def probe_video(path: Path, ffprobe: str) -> VideoInfo:
    command = [
        ffprobe,
        "-v", "error",
        "-select_streams", "v:0",
        "-show_entries",
        "stream=codec_name,width,height,avg_frame_rate,r_frame_rate,bit_rate,duration,nb_frames:format=bit_rate,duration,size",
        "-of", "json",
        str(path),
    ]
    try:
        completed = run_captured_text(command)
    except OSError as exc:
        raise TranscodeError(f"无法运行 ffprobe：{exc}") from exc
    if completed.returncode != 0:
        raise TranscodeError(f"ffprobe 无法读取 {path}：{completed.stderr.strip()}")
    try:
        probe_data = json.loads(completed.stdout)
        streams = probe_data.get("streams", [])
        stream = streams[0]
        codec = normalize_codec(stream["codec_name"])
        width, height = int(stream["width"]), int(stream["height"])
    except (json.JSONDecodeError, IndexError, KeyError, TypeError, ValueError) as exc:
        raise TranscodeError(f"{path} 中没有可用的主视频流") from exc
    fps = ratio_to_float(stream.get("avg_frame_rate")) or ratio_to_float(stream.get("r_frame_rate"))
    bitrate: int | None = None
    try:
        stream_bitrate = int(stream.get("bit_rate", 0))
        bitrate = stream_bitrate if stream_bitrate > 0 else None
    except (TypeError, ValueError):
        pass
    if bitrate is None:
        format_info = probe_data.get("format", {})
        try:
            container_bitrate = int(format_info.get("bit_rate", 0))
            bitrate = container_bitrate if container_bitrate > 0 else None
        except (TypeError, ValueError):
            pass
        if bitrate is None:
            try:
                duration = float(format_info["duration"])
                size = int(format_info["size"])
                if duration > 0 and size > 0:
                    bitrate = int(round(size * 8 / duration))
            except (KeyError, TypeError, ValueError, ZeroDivisionError):
                pass
    duration: float | None = None
    for raw_duration in (stream.get("duration"), probe_data.get("format", {}).get("duration")):
        try:
            parsed_duration = float(raw_duration)
            if math.isfinite(parsed_duration) and parsed_duration > 0:
                duration = parsed_duration
                break
        except (TypeError, ValueError):
            continue
    frame_count: int | None = None
    try:
        parsed_frames = int(stream.get("nb_frames", 0))
        frame_count = parsed_frames if parsed_frames > 0 else None
    except (TypeError, ValueError):
        pass
    if frame_count is None and duration and fps:
        frame_count = max(1, round(duration * fps))
    return VideoInfo(
        codec=codec,
        width=width,
        height=height,
        fps=fps,
        bitrate=bitrate,
        duration=duration,
        frame_count=frame_count,
    )


def expand_inputs(values: Sequence[str], recursive: bool) -> list[Path]:
    result: list[Path] = []
    seen: set[Path] = set()
    for raw in values:
        path = Path(raw).expanduser().resolve()
        candidates: Iterable[Path]
        if path.is_dir():
            iterator = path.rglob("*") if recursive else path.glob("*")
            candidates = (item for item in iterator if item.is_file() and item.suffix.lower() in VIDEO_EXTENSIONS)
        elif path.is_file():
            candidates = [path]
        else:
            raise TranscodeError(f"输入不存在：{path}")
        for candidate in sorted(candidates):
            resolved = candidate.resolve()
            if resolved not in seen:
                seen.add(resolved)
                result.append(resolved)
    if not result:
        raise TranscodeError("没有找到视频输入")
    return result


def _version_tuple(value: object) -> tuple[int, int, int, int] | None:
    match = re.search(r"(?<!\d)(\d+)\.(\d+)(?:\.(\d+))?(?:\.(\d+))?", str(value or ""))
    if not match:
        return None
    return tuple(int(part or 0) for part in match.groups())  # type: ignore[return-value]


def load_hlm_document(path: str | Path) -> dict:
    """Load and validate the normalized FlashCut-to-CodecFoundry contract."""

    manifest = Path(path).expanduser().resolve()
    try:
        document = json.loads(manifest.read_text(encoding="utf-8-sig"))
    except FileNotFoundError as exc:
        raise TranscodeError(f"HLM 文件不存在：{manifest}") from exc
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise TranscodeError(f"无法读取 HLM 文件 {manifest}：{exc}") from exc
    if not isinstance(document, dict) or document.get("format") != HLM_FORMAT:
        raise TranscodeError("HLM 格式标识无效")
    try:
        format_version = int(document.get("format_version", 0))
    except (TypeError, ValueError) as exc:
        raise TranscodeError("HLM format_version 必须是整数") from exc
    if format_version != SUPPORTED_HLM_VERSION:
        raise TranscodeError(
            f"不支持 HLM v{format_version}，当前 CodecFoundry 仅支持 v{SUPPORTED_HLM_VERSION}"
        )
    source = document.get("source")
    highlights = document.get("highlights")
    processing = document.get("processing")
    if not isinstance(source, dict) or not str(source.get("path") or "").strip():
        raise TranscodeError("HLM 缺少 source.path 原片路径")
    if not isinstance(highlights, list) or not all(isinstance(item, dict) for item in highlights):
        raise TranscodeError("HLM highlights 必须是完整的对象数组")
    if not isinstance(processing, dict):
        raise TranscodeError("HLM 缺少 CodecFoundry processing 信息")
    processor = processing.get("processor")
    if not isinstance(processor, dict) or processor.get("name") != "CodecFoundry":
        raise TranscodeError("HLM processing.processor 不是 CodecFoundry")
    minimum = _version_tuple(processor.get("minimum_version"))
    current = _version_tuple(CODECFOUNDRY_VERSION)
    if minimum is None:
        raise TranscodeError("HLM processing.processor.minimum_version 无效")
    if minimum and current and current < minimum:
        raise TranscodeError(
            f"此 HLM 要求 CodecFoundry >= {processor['minimum_version']}，当前为 {CODECFOUNDRY_VERSION}"
        )
    jobs = processing.get("jobs")
    if not isinstance(jobs, list) or not jobs or not all(isinstance(item, dict) for item in jobs):
        raise TranscodeError("HLM processing.jobs 必须是非空任务数组")
    if processing.get("source_mode") != "original":
        raise TranscodeError("HLM source_mode 必须为 original")
    highlight_ids = [str(item.get("id") or "") for item in highlights]
    if any(not item for item in highlight_ids) or len(set(highlight_ids)) != len(highlight_ids):
        raise TranscodeError("HLM highlights 的 id 必须存在且唯一")
    known_highlights = set(highlight_ids)
    seen_jobs: set[str] = set()
    for job in jobs:
        job_id = str(job.get("id") or "").strip()
        if not job_id or job_id in seen_jobs:
            raise TranscodeError(f"HLM 任务 id 缺失或重复：{job_id or '(空)'}")
        seen_jobs.add(job_id)
        linked = job.get("highlight_ids")
        if not isinstance(linked, list) or not linked:
            raise TranscodeError(f"HLM 任务 {job_id} 缺少 highlight_ids")
        missing = {str(item) for item in linked} - known_highlights
        if missing:
            raise TranscodeError(
                f"HLM 任务 {job_id} 引用了不存在的 HL：{', '.join(sorted(missing))}"
            )
        try:
            start = float(job["start"])
            end = float(job["end"])
            duration = float(job["duration"])
        except (KeyError, TypeError, ValueError) as exc:
            raise TranscodeError(f"HLM 任务 {job_id} 的时间字段无效") from exc
        if (
            not all(math.isfinite(value) for value in (start, end, duration))
            or start < 0
            or end <= start
            or abs(duration - (end - start)) > 0.001
        ):
            raise TranscodeError(f"HLM 任务 {job_id} 的 start/end/duration 不一致")
        output_stem = str(job.get("output_stem") or "").strip(" .")
        if (
            not output_stem
            or Path(output_stem).name != output_stem
            or re.search(r'[<>:"/\\|?*\x00-\x1f]', output_stem)
        ):
            raise TranscodeError(f"HLM 任务 {job_id} 的 output_stem 不是安全文件名")
    hlm_processing_defaults(document, manifest)
    return document


def hlm_source_path(document: dict, manifest_path: str | Path) -> Path:
    raw_path = Path(str(document["source"]["path"])).expanduser()
    if not raw_path.is_absolute():
        raw_path = Path(manifest_path).expanduser().resolve().parent / raw_path
    source = raw_path.resolve()
    if not source.is_file() or source.suffix.lower() not in VIDEO_EXTENSIONS:
        raise TranscodeError(f"HLM 原片不存在或不是支持的视频：{source}")
    try:
        expected_size = int(document["source"].get("size", 0) or 0)
    except (TypeError, ValueError) as exc:
        raise TranscodeError("HLM source.size 必须是整数") from exc
    if expected_size and source.stat().st_size != expected_size:
        raise TranscodeError(
            f"HLM 原片尺寸不匹配：记录 {expected_size} 字节，实际 {source.stat().st_size} 字节"
        )
    return source


def hlm_processing_defaults(document: dict, manifest_path: str | Path) -> dict[str, object]:
    processing = document["processing"]
    encoding = processing.get("encoding") if isinstance(processing.get("encoding"), dict) else {}
    output = processing.get("output") if isinstance(processing.get("output"), dict) else {}
    raw_directory = str(output.get("directory") or "").strip()
    output_dir: Path | None = None
    if raw_directory:
        output_dir = Path(raw_directory).expanduser()
        if not output_dir.is_absolute():
            output_dir = Path(manifest_path).expanduser().resolve().parent / output_dir
        output_dir = output_dir.resolve()
    codec = normalize_codec(str(encoding.get("codec") or "hevc"))
    container = str(encoding.get("container") or "mp4").lower()
    if codec not in {"hevc", "av1"}:
        raise TranscodeError(f"HLM 指定了不支持的输出编码：{codec}")
    if container not in {"mp4", "mkv"}:
        raise TranscodeError(f"HLM 指定了不支持的输出容器：{container}")
    return {
        "codec": codec,
        "container": container,
        "copy_subtitles": bool(encoding.get("copy_subtitles", False)),
        "output_dir": output_dir,
        "overwrite": bool(output.get("overwrite", False)),
    }


def output_for(source: Path, output_dir: Path | None, codec: str, container: str) -> Path:
    del codec  # The codec is intentionally no longer part of the output filename.
    directory = output_dir if output_dir is not None else source.parent
    output = (directory / f"{source.stem}.{container}").resolve()
    if output == source.resolve():
        output = (directory / "compressed" / source.name).resolve()
    return output


def duration_match(
    source_duration: float | None,
    output_duration: float | None,
    source_fps: float | None = None,
) -> tuple[bool, str]:
    if source_duration is None or output_duration is None:
        return False, "无法取得源文件或输出文件时长"
    frame_tolerance = 2.0 / source_fps if source_fps and source_fps > 0 else 0.0
    tolerance = max(
        0.1,
        frame_tolerance,
        min(0.5, source_duration * 0.0001),
    )
    difference = abs(source_duration - output_duration)
    if difference > tolerance:
        return (
            False,
            f"时长不一致（源 {source_duration:.3f}s，输出 {output_duration:.3f}s，"
            f"差 {difference:.3f}s）",
        )
    return True, f"时长一致（差 {difference:.3f}s）"


def build_validation_decode_command(
    output: Path,
    ffmpeg: str,
    gpu_index: int,
) -> list[str]:
    """Build a full-stream video validation command that can only use NVDEC.

    Audio and container structure are inspected by FFprobe.  The expensive video
    decode is deliberately pinned to CUDA so validation never silently consumes a
    CPU core for software video decoding.
    """
    return [
        ffmpeg,
        "-hide_banner",
        "-nostdin",
        "-v",
        "error",
        "-xerror",
        "-hwaccel",
        "cuda",
        "-hwaccel_device",
        str(gpu_index),
        "-hwaccel_output_format",
        "cuda",
        "-i",
        str(output),
        "-map",
        "0:v:0",
        "-f",
        "null",
        os.devnull,
    ]


def run_validation_decode(
    command: Sequence[str],
    controller: ProcessController | None,
    task_key: str | None = None,
) -> tuple[int | None, str]:
    process: subprocess.Popen[str] | None = None
    try:
        popen_options: dict[str, object] = {}
        if os.name == "nt":
            popen_options["creationflags"] = windows_creation_flags(
                new_process_group=True
            )
        else:
            popen_options["start_new_session"] = True
        process = subprocess.Popen(
            list(command),
            stdout=subprocess.DEVNULL,
            stderr=subprocess.PIPE,
            text=True,
            encoding="utf-8",
            errors="replace",
            **popen_options,
        )
        if controller is not None:
            if task_key is None:
                controller.register(process)
            else:
                controller.register(process, task_key)
        _, stderr_text = process.communicate()
        return process.returncode, stderr_text or ""
    except OSError as exc:
        return None, str(exc)
    finally:
        if process is not None and controller is not None:
            controller.unregister(process)


def validate_existing_output(
    output: Path,
    source_info: VideoInfo,
    ffprobe: str,
    ffmpeg: str,
    expected_codec: str,
    expected_resolution: tuple[int, int] | None = None,
    expected_fps: float | None = None,
    controller: ProcessController | None = None,
    source: Path | None = None,
    validation_gpus: Sequence[GpuCapability] = (),
    task_key: str | None = None,
) -> tuple[bool, str]:
    try:
        usable_file = output.is_file() and output.stat().st_size > 0
    except OSError as exc:
        return False, f"无法读取输出文件属性：{exc}"
    if not usable_file:
        return False, "输出为空或不是普通文件"
    try:
        output_info = probe_video(output, ffprobe)
    except TranscodeError as exc:
        return False, f"FFprobe 检查失败：{exc}"
    source_copy = False
    if (
        source is not None
        and expected_resolution is None
        and expected_fps is None
        and source.suffix.casefold() == output.suffix.casefold()
        and normalize_codec(output_info.codec) == normalize_codec(source_info.codec)
    ):
        try:
            source_copy = source.is_file() and source.stat().st_size == output.stat().st_size
        except OSError:
            source_copy = False
    if (
        normalize_codec(output_info.codec) != normalize_codec(expected_codec)
        and not source_copy
    ):
        return (
            False,
            f"编码格式不一致（期望 {expected_codec.upper()}，"
            f"现有 {output_info.codec.upper()}）",
        )
    target_width, target_height = expected_resolution or (
        source_info.width,
        source_info.height,
    )
    if (output_info.width, output_info.height) != (target_width, target_height):
        return (
            False,
            f"分辨率不一致（期望 {target_width}x{target_height}，"
            f"现有 {output_info.width}x{output_info.height}）",
        )
    target_fps = expected_fps or source_info.fps
    if target_fps and (
        output_info.fps is None
        or abs(output_info.fps - target_fps) > max(0.01, target_fps * 0.001)
    ):
        existing_fps = f"{output_info.fps:g}" if output_info.fps else "未知"
        return (
            False,
            f"帧率不一致（期望 {target_fps:g}，现有 {existing_fps}）",
        )
    same_duration, duration_detail = duration_match(
        source_info.duration, output_info.duration, source_info.fps
    )
    if not same_duration:
        return False, duration_detail

    attempts = []
    seen_gpu_indexes: set[int] = set()
    for gpu in validation_gpus:
        if gpu.index in seen_gpu_indexes or not gpu.can_decode(output_info.codec):
            continue
        seen_gpu_indexes.add(gpu.index)
        attempts.append((gpu.index, f"GPU {gpu.index}"))
    if not attempts:
        return (
            False,
            f"没有可用于 {output_info.codec.upper()} 完整解码校验的 NVIDIA GPU",
        )
    last_error = ""
    for gpu_index, decode_label in attempts:
        command = build_validation_decode_command(output, ffmpeg, gpu_index)
        if task_key is None:
            return_code, stderr_text = run_validation_decode(command, controller)
        else:
            return_code, stderr_text = run_validation_decode(
                command, controller, task_key
            )
        if return_code is None:
            return False, f"无法运行完整解码检查：{stderr_text}"
        if controller is not None and (
            controller.cancelled
            or (task_key is not None and controller.task_cancelled(task_key))
        ):
            return False, "有效性检查已取消"
        if return_code == 0:
            copy_detail = "；已保留原文件副本" if source_copy else ""
            return True, f"{decode_label} / NVDEC 可完整解码视频流；{duration_detail}{copy_detail}"
        last_error = error_tail(stderr_text) or "FFmpeg 解码失败"
        if len(attempts) > 1:
            warn(
                f"{output.name}：GPU {gpu_index} 完整解码校验失败，"
                "正在尝试下一块可用 GPU"
            )
    return False, f"所有可用 GPU 均无法完整解码视频流：{last_error}"


def make_tasks(
    inputs: Sequence[Path],
    output_dir: Path | None,
    codec: str,
    container: str,
    ffprobe: str,
    ffmpeg: str,
    overwrite: bool,
    controller: ProcessController | None = None,
    expected_resolution: tuple[int, int] | None = None,
    expected_fps: float | None = None,
    validation_gpus: Sequence[GpuCapability] = (),
) -> list[VideoTask]:
    if output_dir is not None:
        output_dir.mkdir(parents=True, exist_ok=True)
    tasks: list[VideoTask] = []
    outputs: set[Path] = set()
    input_count = len(inputs)
    for source_index, source in enumerate(inputs):
        emit_event(
            "probe_progress",
            progress=source_index / input_count if input_count else 0.0,
            current=source_index + 1,
            total=input_count,
            filename=source.name,
            stage="正在探测源视频",
        )
        info = probe_video(source, ffprobe)
        output = output_for(source, output_dir, codec, container)
        output.parent.mkdir(parents=True, exist_ok=True)
        if output in outputs:
            raise TranscodeError(f"多个输入会生成同一个输出：{output}")
        outputs.add(output)
        skip_reason: str | None = None
        overwrite_existing = False
        replacement_reason: str | None = None
        if output.exists() and not overwrite:
            validation_gpu = next(
                (gpu for gpu in validation_gpus if gpu.can_decode(codec)), None
            )
            validation_mode = (
                f"GPU {validation_gpu.index} / NVDEC"
                if validation_gpu is not None
                else "无可用 GPU"
            )
            emit_event(
                "probe_progress",
                progress=(source_index + 0.5) / input_count if input_count else 0.0,
                current=source_index + 1,
                total=input_count,
                filename=source.name,
                stage=f"正在校验已有输出（{validation_mode} 解码）",
            )
            valid, validation_detail = validate_existing_output(
                output,
                info,
                ffprobe,
                ffmpeg,
                codec,
                expected_resolution,
                expected_fps,
                controller,
                source,
                validation_gpus,
            )
            if valid:
                skip_reason = f"已有输出有效；{validation_detail}"
            else:
                overwrite_existing = True
                replacement_reason = validation_detail
        tasks.append(
            VideoTask(
                source=source,
                output=output,
                info=info,
                skip_reason=skip_reason,
                overwrite_existing=overwrite_existing,
                replacement_reason=replacement_reason,
            )
        )
        emit_event(
            "probe_progress",
            progress=(source_index + 1) / input_count if input_count else 1.0,
            current=source_index + 1,
            total=input_count,
            filename=source.name,
            stage="探测完成",
        )
    return tasks


def make_hlm_tasks(
    document: dict,
    manifest_path: str | Path,
    output_dir: Path | None,
    codec: str,
    container: str,
    ffprobe: str,
    ffmpeg: str,
    overwrite: bool,
    controller: ProcessController | None = None,
    expected_resolution: tuple[int, int] | None = None,
    expected_fps: float | None = None,
    validation_gpus: Sequence[GpuCapability] = (),
    selected_job_ids: Sequence[str] = (),
) -> list[VideoTask]:
    """Create one precise source-video encode task for every selected HLM job."""

    source = hlm_source_path(document, manifest_path)
    source_info = probe_video(source, ffprobe)
    processing = document["processing"]
    all_jobs = processing["jobs"]
    selected = {str(item) for item in selected_job_ids}
    known_job_ids = {str(job.get("id") or "") for job in all_jobs}
    unknown = selected - known_job_ids
    if unknown:
        raise TranscodeError(f"HLM 中不存在任务：{', '.join(sorted(unknown))}")
    jobs = [job for job in all_jobs if not selected or str(job.get("id") or "") in selected]
    if not jobs:
        raise TranscodeError("没有选中的 HLM 任务")

    highlight_ids = {
        str(item.get("id")) for item in document["highlights"] if item.get("id")
    }
    directory = output_dir or source.parent
    directory.mkdir(parents=True, exist_ok=True)
    tasks: list[VideoTask] = []
    outputs: set[Path] = set()
    seen_job_ids: set[str] = set()
    for job_index, job in enumerate(jobs):
        job_id = str(job.get("id") or "").strip()
        if not job_id or job_id in seen_job_ids:
            raise TranscodeError(f"HLM 任务 id 缺失或重复：{job_id or '(空)'}")
        seen_job_ids.add(job_id)
        linked_ids = job.get("highlight_ids")
        if not isinstance(linked_ids, list) or not linked_ids:
            raise TranscodeError(f"HLM 任务 {job_id} 缺少 highlight_ids")
        missing_highlights = {str(item) for item in linked_ids} - highlight_ids
        if missing_highlights:
            raise TranscodeError(
                f"HLM 任务 {job_id} 引用了不存在的 HL：{', '.join(sorted(missing_highlights))}"
            )
        try:
            start = float(job["start"])
            end = float(job["end"])
        except (KeyError, TypeError, ValueError) as exc:
            raise TranscodeError(f"HLM 任务 {job_id} 的 start/end 无效") from exc
        if not math.isfinite(start) or not math.isfinite(end) or start < 0 or end <= start:
            raise TranscodeError(f"HLM 任务 {job_id} 的时间窗无效：{start:g}-{end:g}")
        if source_info.duration is not None:
            if start >= source_info.duration:
                raise TranscodeError(f"HLM 任务 {job_id} 起点超出原片时长")
            if end > source_info.duration + 0.1:
                raise TranscodeError(f"HLM 任务 {job_id} 终点超出原片时长")
            end = min(end, source_info.duration)
        duration = end - start
        recorded_duration = float(job.get("duration", duration) or duration)
        if abs(recorded_duration - duration) > 0.001:
            raise TranscodeError(f"HLM 任务 {job_id} 的 duration 与 start/end 不一致")
        output_stem = str(job.get("output_stem") or "").strip(" .")
        if (
            not output_stem
            or Path(output_stem).name != output_stem
            or re.search(r'[<>:"/\\|?*\x00-\x1f]', output_stem)
        ):
            raise TranscodeError(f"HLM 任务 {job_id} 的 output_stem 不是安全文件名")
        output = (directory / f"{output_stem}.{container}").resolve()
        if output == source:
            output = (directory / "compressed" / f"{output_stem}.{container}").resolve()
        output.parent.mkdir(parents=True, exist_ok=True)
        if output in outputs:
            raise TranscodeError(f"多个 HLM 任务会生成同一个输出：{output}")
        outputs.add(output)
        frame_count = (
            max(1, round(duration * source_info.fps))
            if source_info.fps and duration > 0
            else None
        )
        clip_info = replace(source_info, duration=duration, frame_count=frame_count)
        skip_reason: str | None = None
        overwrite_existing = False
        replacement_reason: str | None = None
        emit_event(
            "probe_progress",
            progress=job_index / len(jobs),
            current=job_index + 1,
            total=len(jobs),
            filename=output_stem,
            stage="正在建立 HLM 原片任务",
        )
        if output.exists() and not overwrite:
            valid, validation_detail = validate_existing_output(
                output,
                clip_info,
                ffprobe,
                ffmpeg,
                codec,
                expected_resolution,
                expected_fps,
                controller,
                None,
                validation_gpus,
            )
            if valid:
                skip_reason = f"已有输出有效；{validation_detail}"
            else:
                overwrite_existing = True
                replacement_reason = validation_detail
        tasks.append(
            VideoTask(
                source=source,
                output=output,
                info=clip_info,
                skip_reason=skip_reason,
                overwrite_existing=overwrite_existing,
                replacement_reason=replacement_reason,
                clip_start=start,
                clip_duration=duration,
                source_duration=source_info.duration,
                display_name=f"{output_stem} [{start:.3f}s-{end:.3f}s]",
                external_id=job_id,
            )
        )
        emit_event(
            "probe_progress",
            progress=(job_index + 1) / len(jobs),
            current=job_index + 1,
            total=len(jobs),
            filename=output_stem,
            stage="HLM 任务已就绪",
        )
    return tasks


def verify_ffmpeg_encoder(ffmpeg: str, codec: str) -> None:
    if shutil.which(ffmpeg) is None and not Path(ffmpeg).is_file():
        raise TranscodeError(f"找不到 FFmpeg：{ffmpeg}")
    encoder = f"{codec}_nvenc"
    completed = run_captured_text([ffmpeg, "-hide_banner", "-encoders"])
    if completed.returncode != 0 or not re.search(rf"\b{re.escape(encoder)}\b", completed.stdout):
        raise TranscodeError(f"当前 FFmpeg 没有 {encoder} 编码器")


def create_slots(gpus: Sequence[GpuCapability], codec: str) -> list[EncoderSlot]:
    eligible = [gpu for gpu in gpus if gpu.can_encode(codec)]
    if not eligible:
        details = "; ".join(
            f"GPU {gpu.index} {gpu.name}: {','.join(sorted(gpu.encode)) or '无'}" for gpu in gpus
        )
        raise TranscodeError(f"所选 {codec.upper()} 硬件编码不受任何可用 GPU 支持。配置情况：{details}")
    return [
        EncoderSlot(gpu=gpu, engine=engine)
        for gpu in eligible
        for engine in range(gpu.encoder_engines)
    ]


def scheduling_workload(task: VideoTask, settings: EncodeSettings | None = None) -> float:
    """Estimate 1080p-equivalent frames for longest-processing-time scheduling."""
    if settings is not None:
        workload = task_equivalent_frames(task, settings)
    else:
        frames = task.info.frame_count
        if not frames and task.info.duration and task.info.fps:
            frames = max(1, round(task.info.duration * task.info.fps))
        workload = (frames or 0) * (
            task.info.width * task.info.height / REFERENCE_PIXELS
        )
    return max(1.0, float(workload))


def schedule_tasks(
    tasks: Sequence[VideoTask],
    slots: list[EncoderSlot],
    settings: EncodeSettings | None = None,
) -> list[EncoderSlot]:
    """Use LPT workload balancing, with a small hardware-decode preference."""
    loads = {id(slot): 0.0 for slot in slots}
    weighted_tasks = sorted(
        ((scheduling_workload(task, settings), position, task) for position, task in enumerate(tasks)),
        key=lambda item: (-item[0], item[1]),
    )
    for workload, _position, task in weighted_tasks:
        chosen = min(
            slots,
            key=lambda slot: (
                loads[id(slot)] + workload * (
                    1.0 if slot.gpu.can_decode(task.info.codec) else 1.05
                ),
                loads[id(slot)],
                len(slot.tasks),
                slot.gpu.index,
                slot.engine,
            ),
        )
        chosen.tasks.append(task)
        loads[id(chosen)] += workload
    active = [slot for slot in slots if slot.tasks]
    cpu_count = os.cpu_count() or 1
    # On Windows, SetProcessAffinityMask addresses one processor group (at most pointer width).
    addressable = min(cpu_count, ctypes.sizeof(ctypes.c_size_t) * 8) if os.name == "nt" else cpu_count
    cores = list(range(addressable))
    for position, slot in enumerate(active):
        slot.slot_id = position
        slot.cpu_ids = tuple(cores[position::len(active)]) or (cores[position % len(cores)],)
    return active


def task_output_geometry(task: VideoTask, settings: EncodeSettings) -> tuple[int, int, float | None]:
    width, height = settings.resolution or (task.info.width, task.info.height)
    fps = settings.fps_value or task.info.fps
    return width, height, fps


def task_total_frames(task: VideoTask, settings: EncodeSettings) -> int | None:
    _, _, fps = task_output_geometry(task, settings)
    if settings.fps_value and task.info.duration:
        return max(1, round(task.info.duration * settings.fps_value))
    if task.info.frame_count:
        return task.info.frame_count
    if task.info.duration and fps:
        return max(1, round(task.info.duration * fps))
    return None


def task_equivalent_frames(task: VideoTask, settings: EncodeSettings) -> float:
    width, height, _ = task_output_geometry(task, settings)
    frames = task_total_frames(task, settings) or 0
    return frames * (width * height / REFERENCE_PIXELS)


def parallel_queue_eta(queues: Sequence[tuple[float, float]]) -> float | None:
    """Return the slowest queue ETA; unknown speed on unfinished work means unknown."""
    etas: list[float] = []
    for remaining_work, average_rate in queues:
        if remaining_work <= 0:
            continue
        if average_rate <= 0:
            return None
        etas.append(remaining_work / average_rate)
    return max(etas, default=0.0)


def update_recent_work_rate(
    samples: deque[tuple[float, float]],
    now: float,
    processed_work: float,
    fallback_rate: float = 0.0,
    window_seconds: float = 10.0,
) -> float:
    """Append a work sample and return its sliding-window average rate."""
    samples.append((now, processed_work))
    cutoff = now - window_seconds
    while len(samples) > 2 and samples[1][0] <= cutoff:
        samples.popleft()
    elapsed = now - samples[0][0]
    processed_delta = processed_work - samples[0][1]
    if elapsed >= 0.25:
        if processed_delta > 0:
            return processed_delta / elapsed
        return max(0.0, fallback_rate)
    return max(0.0, fallback_rate)


def responsive_layout_mode(width: int, height: int) -> str:
    """Return the GUI breakpoint mode for a window size."""
    safe_height = max(1, height)
    ratio = max(1, width) / safe_height
    if ratio >= 1.7 and height > 800:
        return "wide"
    if ratio < 1.0:
        return "portrait"
    return "normal"


def effective_rate_limit(task: VideoTask, settings: EncodeSettings) -> tuple[int | None, int | None]:
    """Return an explicit user rate cap; omission means genuinely unlimited."""
    del task  # Source average bitrate is informative only; it is not a safe VBR peak cap.
    maxrate = settings.maxrate
    if maxrate is None:
        return None, None
    return maxrate, settings.bufsize or maxrate


def build_ffmpeg_command(
    task: VideoTask,
    gpu: GpuCapability,
    settings: EncodeSettings,
    ffmpeg: str,
    hardware_decode: bool,
    force_overwrite: bool = False,
) -> list[str]:
    command = [ffmpeg, "-hide_banner", "-nostdin", "-loglevel", "warning"]
    command.append(
        "-y"
        if settings.overwrite or force_overwrite or task.overwrite_existing
        else "-n"
    )
    if hardware_decode:
        command += [
            "-hwaccel", "cuda",
            "-hwaccel_device", str(gpu.index),
            "-hwaccel_output_format", "cuda",
        ]
    if task.clip_start is not None:
        command += ["-ss", f"{task.clip_start:.6f}"]
    command += ["-i", str(task.source)]
    if task.clip_duration is not None:
        command += ["-t", f"{task.clip_duration:.6f}"]
    command += ["-map", "0:v:0", "-map", "0:a?" ]
    if settings.copy_subtitles:
        command += ["-map", "0:s?"]
    command += ["-map_metadata", "0", "-map_chapters", "0"]

    if settings.resolution:
        width, height = settings.resolution
        if hardware_decode:
            command += ["-vf", f"scale_cuda={width}:{height}"]
        else:
            command += ["-vf", f"scale={width}:{height}:flags=lanczos"]

    target_fps = settings.fps_value or task.info.fps
    if settings.fps_text:
        command += ["-r", settings.fps_text, "-fps_mode", "cfr"]
    else:
        command += ["-fps_mode", "passthrough"]

    command += [
        "-c:v", f"{settings.codec}_nvenc",
        "-gpu", str(gpu.index),
        "-preset", settings.preset,
        "-tune", "hq",
        "-rc", "vbr",
        "-b:v", "0",
        "-cq", f"{settings.cq:g}",
        "-multipass", settings.multipass,
        "-rc-lookahead", str(settings.lookahead),
        "-temporal-aq", "1",
        "-bf", "4",
        "-b_ref_mode", "middle",
    ]
    if task.output.suffix.lower() == ".mp4" and settings.codec == "hevc":
        command += ["-tag:v", "hvc1"]
    maxrate, bufsize = effective_rate_limit(task, settings)
    if maxrate is not None:
        command += ["-maxrate", format_bitrate(maxrate), "-bufsize", format_bitrate(bufsize)]
    if target_fps:
        command += ["-g", str(max(1, round(target_fps * 2)))]
    command += ["-c:a", "copy"]
    if settings.copy_subtitles:
        command += ["-c:s", "mov_text" if task.output.suffix.lower() == ".mp4" else "copy"]
    if task.output.suffix.lower() == ".mp4":
        command += ["-movflags", "+faststart"]
    command += [
        "-max_muxing_queue_size", "4096",
        "-stats_period", "0.5",
        "-progress", "pipe:1",
        "-nostats",
        str(task.output),
    ]
    return command


def command_text(command: Sequence[str]) -> str:
    return subprocess.list2cmdline(command) if os.name == "nt" else shlex.join(command)


def timestamp_to_seconds(value: str | None) -> float | None:
    if not value:
        return None
    try:
        hours, minutes, seconds = value.split(":", 2)
        return int(hours) * 3600 + int(minutes) * 60 + float(seconds)
    except (TypeError, ValueError):
        return None


def set_process_affinity(process: subprocess.Popen[str], cpu_ids: Sequence[int]) -> str | None:
    if not cpu_ids:
        return None
    try:
        if os.name == "nt":
            process_set_information = 0x0200
            kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
            kernel32.OpenProcess.argtypes = [ctypes.c_uint32, ctypes.c_int, ctypes.c_uint32]
            kernel32.OpenProcess.restype = ctypes.c_void_p
            kernel32.SetProcessAffinityMask.argtypes = [ctypes.c_void_p, ctypes.c_size_t]
            kernel32.SetProcessAffinityMask.restype = ctypes.c_int
            kernel32.CloseHandle.argtypes = [ctypes.c_void_p]
            kernel32.CloseHandle.restype = ctypes.c_int
            handle = kernel32.OpenProcess(process_set_information, False, process.pid)
            if not handle:
                raise ctypes.WinError()
            try:
                mask = sum(1 << cpu for cpu in cpu_ids)
                if not kernel32.SetProcessAffinityMask(handle, ctypes.c_size_t(mask)):
                    raise ctypes.WinError()
            finally:
                kernel32.CloseHandle(handle)
        elif hasattr(os, "sched_setaffinity"):
            os.sched_setaffinity(process.pid, set(cpu_ids))
        else:
            return "当前系统不提供进程 CPU 亲和性 API"
    except (OSError, AttributeError) as exc:
        return str(exc)
    return None


def run_ffmpeg(
    command: Sequence[str],
    label: str,
    cpu_ids: Sequence[int],
    affinity_enabled: bool,
    controller: ProcessController,
    progress_callback=None,
    task_key: str | None = None,
) -> tuple[int, str]:
    if controller.cancelled or (
        task_key is not None and controller.task_cancelled(task_key)
    ):
        return 130, "任务已取消"
    log_path: Path | None = None
    process: subprocess.Popen[str] | None = None
    try:
        with tempfile.NamedTemporaryFile(
            mode="w", encoding="utf-8", suffix=".nvtranscode.log", delete=False
        ) as error_log:
            log_path = Path(error_log.name)
            popen_options: dict[str, object] = {}
            if os.name == "nt":
                popen_options["creationflags"] = windows_creation_flags(
                    new_process_group=True
                )
            else:
                popen_options["start_new_session"] = True
            process = subprocess.Popen(
                list(command),
                stdout=subprocess.PIPE,
                stderr=error_log,
                text=True,
                encoding="utf-8",
                errors="replace",
                **popen_options,
            )
            if task_key is None:
                controller.register(process)
            else:
                controller.register(process, task_key)
            if affinity_enabled:
                affinity_error = set_process_affinity(process, cpu_ids)
                if affinity_error:
                    warn(f"{label} 无法设置 CPU 亲和性：{affinity_error}")
            values: dict[str, str] = {}
            last_report = 0.0
            assert process.stdout is not None
            for raw_line in process.stdout:
                key, separator, value = raw_line.strip().partition("=")
                if separator:
                    values[key] = value
                if key == "progress" and value in {"continue", "end"}:
                    try:
                        frame = int(values.get("frame", "0"))
                    except ValueError:
                        frame = 0
                    try:
                        encoding_fps = float(values.get("fps", "0"))
                    except ValueError:
                        encoding_fps = 0.0
                    try:
                        speed = float(values.get("speed", "0x").rstrip("x"))
                    except ValueError:
                        speed = 0.0
                    metrics = {
                        "frame": frame,
                        "fps": encoding_fps,
                        "speed": speed,
                        "out_time": timestamp_to_seconds(values.get("out_time")),
                        "finished": value == "end",
                    }
                    if progress_callback is not None:
                        progress_callback(metrics)
                    if value == "continue" and time.monotonic() - last_report >= 5:
                        say(
                            f"[进度] {label}  时间 {values.get('out_time', '?')}  "
                            f"fps {values.get('fps', '?')}  speed {values.get('speed', '?')}"
                        )
                        last_report = time.monotonic()
            return_code = process.wait()
        try:
            stderr_text = log_path.read_text(encoding="utf-8", errors="replace")
        except OSError:
            stderr_text = ""
        if controller.cancelled or (
            task_key is not None and controller.task_cancelled(task_key)
        ):
            return 130, "任务已取消"
        return return_code, stderr_text
    except OSError as exc:
        return 127, str(exc)
    finally:
        if process is not None:
            controller.unregister(process)
        if log_path is not None:
            try:
                log_path.unlink(missing_ok=True)
            except OSError:
                pass


def error_tail(text: str, lines: int = 18) -> str:
    cleaned = [line for line in text.splitlines() if line.strip()]
    return "\n".join(cleaned[-lines:])


def format_file_size(value: int) -> str:
    units = ("B", "KB", "MB", "GB", "TB")
    size = float(value)
    for unit in units:
        if abs(size) < 1000 or unit == units[-1]:
            return f"{size:.2f} {unit}"
        size /= 1000
    return f"{size:.2f} TB"


def task_input_size(task: VideoTask) -> int:
    """Return exact size, or a duration-proportional estimate for an HLM clip."""

    try:
        size = task.source.stat().st_size
    except OSError:
        return 0
    if task.is_clip and task.clip_duration and task.source_duration:
        return max(1, round(size * min(1.0, task.clip_duration / task.source_duration)))
    return size


def write_compression_report(
    tasks: Sequence[VideoTask],
    results: Sequence[tuple[VideoTask, bool, str]],
    settings: EncodeSettings,
    explicit_output_dir: Path | None,
    started_at: datetime,
    finished_at: datetime,
) -> Path:
    result_by_task = {task: (success, detail) for task, success, detail in results}
    successful_input = 0
    successful_output = 0
    lines = [
        "NVENC 视频压制报告",
        "=" * 72,
        f"开始时间：{started_at:%Y-%m-%d %H:%M:%S}",
        f"结束时间：{finished_at:%Y-%m-%d %H:%M:%S}",
        f"总耗时：{finished_at - started_at}",
        f"输出编码：{settings.codec.upper()}",
        f"CQ：{settings.cq:g}",
        f"最大码率：{format_bitrate(settings.maxrate) if settings.maxrate else '无限制'}",
        "",
        "逐文件尺寸变化",
        "-" * 72,
    ]
    for task in tasks:
        success, detail = result_by_task.get(task, (False, "未执行"))
        input_size = task_input_size(task)
        output_size = task.output.stat().st_size if success and task.output.exists() else 0
        lines.append(f"输入：{task.source}{' · ' + task.label if task.is_clip else ''}")
        lines.append(f"输出：{task.output}")
        if success:
            change = input_size - output_size
            percent = change / input_size * 100 if input_size else 0.0
            successful_input += input_size
            successful_output += output_size
            direction = "减少" if change >= 0 else "增加"
            lines.append(
                f"结果：成功｜{format_file_size(input_size)} -> {format_file_size(output_size)}｜"
                f"{direction} {format_file_size(abs(change))}（{abs(percent):.2f}%）"
                f"{'｜' + detail if detail else ''}"
            )
        else:
            if detail == "未执行":
                status = "未执行"
            elif detail.startswith("跳过："):
                status = "跳过"
            else:
                status = "取消" if "取消" in detail else "失败"
            lines.append(f"结果：{status}｜{detail}")
        lines.append("")

    saved = successful_input - successful_output
    saved_gb = saved / 1_000_000_000
    percent = saved / successful_input * 100 if successful_input else 0.0
    cancelled_count = sum(
        1 for _, success, detail in results if not success and "取消" in detail
    )
    skipped_count = sum(
        1 for _, success, detail in results
        if not success and detail.startswith("跳过：")
    )
    failed_count = sum(
        1 for _, success, detail in results
        if not success and "取消" not in detail and not detail.startswith("跳过：")
    )
    unexecuted_count = max(0, len(tasks) - len(results))
    lines += [
        "总体统计",
        "-" * 72,
        f"成功文件原始总尺寸：{format_file_size(successful_input)}",
        f"成功文件输出总尺寸：{format_file_size(successful_output)}",
        f"尺寸变化：{'压缩减少' if saved >= 0 else '编码增加'} {abs(saved_gb):.3f} GB（{abs(percent):.2f}%）",
        f"成功：{sum(1 for _, success, _ in results if success)}",
        f"失败：{failed_count}",
        f"取消：{cancelled_count}",
        f"跳过：{skipped_count}",
        f"未执行：{unexecuted_count}",
    ]

    if explicit_output_dir is not None:
        report_dir = explicit_output_dir
    else:
        output_parents = {task.output.parent for task in tasks}
        report_dir = next(iter(output_parents)) if len(output_parents) == 1 else Path.cwd()
    report_dir.mkdir(parents=True, exist_ok=True)
    report_path = report_dir / f"nvtranscode_report_{finished_at:%Y%m%d_%H%M%S}.txt"
    report_path.write_text("\n".join(lines) + "\n", encoding="utf-8-sig")
    return report_path


def remove_partial_output(task: VideoTask) -> None:
    try:
        task.output.unlink(missing_ok=True)
    except OSError as exc:
        warn(f"无法删除已取消任务的残留输出 {task.output}：{exc}")


def capture_file_timestamps(path: Path) -> FileTimestamps:
    """Capture timestamps before transcoding so the source values cannot drift."""
    stat_result = path.stat()
    return FileTimestamps(
        accessed_ns=stat_result.st_atime_ns,
        modified_ns=stat_result.st_mtime_ns,
        created_ns=stat_result.st_ctime_ns if os.name == "nt" else None,
    )


def _set_windows_creation_time(path: Path, created_ns: int) -> None:
    """Set a file's Windows creation time without changing its other timestamps."""
    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    create_file = kernel32.CreateFileW
    create_file.argtypes = (
        wintypes.LPCWSTR,
        wintypes.DWORD,
        wintypes.DWORD,
        wintypes.LPVOID,
        wintypes.DWORD,
        wintypes.DWORD,
        wintypes.HANDLE,
    )
    create_file.restype = wintypes.HANDLE
    set_file_time = kernel32.SetFileTime
    set_file_time.argtypes = (
        wintypes.HANDLE,
        ctypes.POINTER(wintypes.FILETIME),
        ctypes.POINTER(wintypes.FILETIME),
        ctypes.POINTER(wintypes.FILETIME),
    )
    set_file_time.restype = wintypes.BOOL
    close_handle = kernel32.CloseHandle
    close_handle.argtypes = (wintypes.HANDLE,)
    close_handle.restype = wintypes.BOOL

    file_write_attributes = 0x0100
    open_existing = 3
    handle = create_file(
        str(path), file_write_attributes, 0, None, open_existing, 0, None
    )
    if handle == wintypes.HANDLE(-1).value:
        raise ctypes.WinError(ctypes.get_last_error())
    try:
        windows_ticks = created_ns // 100 + 116_444_736_000_000_000
        creation_time = wintypes.FILETIME(
            windows_ticks & 0xFFFFFFFF,
            windows_ticks >> 32,
        )
        if not set_file_time(handle, ctypes.byref(creation_time), None, None):
            raise ctypes.WinError(ctypes.get_last_error())
    finally:
        close_handle(handle)


def apply_file_timestamps(path: Path, timestamps: FileTimestamps) -> None:
    """Restore source access/modified time and, on Windows, creation time."""
    os.utime(path, ns=(timestamps.accessed_ns, timestamps.modified_ns))
    if os.name == "nt" and timestamps.created_ns is not None:
        _set_windows_creation_time(path, timestamps.created_ns)


def preserve_source_when_output_is_larger(
    task: VideoTask,
    settings: EncodeSettings,
) -> tuple[bool, str]:
    """Atomically replace a larger encode with the source when byte-copying is safe."""
    if task.is_clip:
        return False, ""
    try:
        input_size = task.source.stat().st_size
        encoded_size = task.output.stat().st_size
    except OSError:
        return False, ""
    if encoded_size <= input_size:
        return False, ""
    if task.source.suffix.casefold() != task.output.suffix.casefold():
        warn(
            f"{task.source.name}：编码输出比源文件大，但容器不同，"
            "为避免扩展名与实际容器不符，保留编码输出"
        )
        return False, ""
    if settings.resolution is not None or settings.fps_text is not None:
        warn(
            f"{task.source.name}：编码输出比源文件大，但任务要求转换"
            "分辨率或帧率，保留编码输出"
        )
        return False, ""

    temporary_path: Path | None = None
    try:
        with tempfile.NamedTemporaryFile(
            prefix=f".{task.output.name}.",
            suffix=".source-copy",
            dir=task.output.parent,
            delete=False,
        ) as temporary:
            temporary_path = Path(temporary.name)
        shutil.copy2(task.source, temporary_path)
        os.replace(temporary_path, task.output)
        temporary_path = None
    except OSError as exc:
        warn(f"{task.source.name}：编码结果更大，但复制原文件失败：{exc}")
        return False, ""
    finally:
        if temporary_path is not None:
            try:
                temporary_path.unlink(missing_ok=True)
            except OSError:
                pass

    detail = (
        "编码输出更大，已保留原文件（"
        f"{format_file_size(encoded_size)} -> {format_file_size(input_size)}）"
    )
    return True, detail


def run_slot(
    slot: EncoderSlot,
    settings: EncodeSettings,
    ffmpeg: str,
    affinity_enabled: bool,
    software_fallback: bool,
    controller: ProcessController,
    ffprobe: str = "ffprobe",
    validation_gpus: Sequence[GpuCapability] = (),
) -> list[tuple[VideoTask, bool, str]]:
    results: list[tuple[VideoTask, bool, str]] = []
    queue_equivalent_frames = sum(task_equivalent_frames(task, settings) for task in slot.tasks)
    completed_equivalent_frames = 0.0
    for task_index, task in enumerate(slot.tasks):
        task_key = str(task.output)
        if controller.cancelled or controller.task_cancelled(task_key):
            detail = "批处理已取消" if controller.cancelled else "任务已取消 / 停止"
            results.append((task, False, detail))
            emit_event(
                "task_done",
                slot_id=slot.slot_id,
                task_index=task_index,
                status="cancelled",
                source=str(task.source),
                output=str(task.output),
            )
            continue
        hardware_decode = slot.gpu.can_decode(task.info.codec)
        label = f"GPU{slot.gpu.index}/E{slot.slot_id} {task.label}"
        try:
            source_timestamps = capture_file_timestamps(task.source)
        except OSError as exc:
            source_timestamps = None
            warn(f"{label}：无法读取源文件时间戳：{exc}")
        if not hardware_decode:
            warn(
                f"{label}：GPU 不支持 {task.info.codec.upper()} 硬件解码；"
                "将使用 CPU 解码，但输出仍由 NVENC 编码"
            )
        say(
            f"[开始] {label} -> {task.output.name} | "
            f"{'GPU' if hardware_decode else 'CPU'} 解码 | CPU {','.join(map(str, slot.cpu_ids))}"
        )
        total_frames = task_total_frames(task, settings)
        width, height, target_fps = task_output_geometry(task, settings)
        equivalent_frames = task_equivalent_frames(task, settings)
        emit_event(
            "task_start",
            slot_id=slot.slot_id,
            gpu_index=slot.gpu.index,
            engine=slot.engine,
            task_index=task_index,
            queue_total=len(slot.tasks),
            queue_equivalent_frames=queue_equivalent_frames,
            completed_equivalent_frames=completed_equivalent_frames,
            source=str(task.source),
            output=str(task.output),
            filename=task.label,
            total_frames=total_frames,
            equivalent_frames=equivalent_frames,
            width=width,
            height=height,
            target_fps=target_fps,
            duration=task.info.duration,
        )

        def progress_handler(metrics: dict[str, object]) -> None:
            frame = int(metrics.get("frame") or 0)
            out_time = metrics.get("out_time")
            processed_frames = frame
            if processed_frames <= 0 and out_time and target_fps:
                processed_frames = round(float(out_time) * target_fps)
            progress = min(1.0, processed_frames / total_frames) if total_frames else 0.0
            measured_fps = float(metrics.get("fps") or 0)
            speed = float(metrics.get("speed") or 0)
            if measured_fps <= 0 and speed > 0 and target_fps:
                measured_fps = speed * target_fps
            eta_seconds = None
            if total_frames and measured_fps > 0:
                eta_seconds = max(0.0, total_frames - processed_frames) / measured_fps
            equivalent_fps = measured_fps * (width * height / REFERENCE_PIXELS)
            emit_event(
                "task_progress",
                slot_id=slot.slot_id,
                task_index=task_index,
                progress=progress,
                frame=processed_frames,
                total_frames=total_frames,
                encoding_fps=measured_fps,
                speed=speed,
                eta_seconds=eta_seconds,
                equivalent_fps=equivalent_fps,
                equivalent_frames=equivalent_frames,
                completed_equivalent_frames=completed_equivalent_frames,
                queue_equivalent_frames=queue_equivalent_frames,
                finished=bool(metrics.get("finished")),
            )

        command = build_ffmpeg_command(task, slot.gpu, settings, ffmpeg, hardware_decode)
        code, stderr_text = run_ffmpeg(
            command,
            label,
            slot.cpu_ids,
            affinity_enabled,
            controller,
            progress_handler,
            task_key,
        )
        if (
            code != 0
            and hardware_decode
            and software_fallback
            and not controller.cancelled
            and not controller.task_cancelled(task_key)
        ):
            warn(f"{label}：硬件解码路径失败，自动改用 CPU 解码重试")
            try:
                task.output.unlink(missing_ok=True)
            except OSError:
                pass
            retry = build_ffmpeg_command(
                task, slot.gpu, settings, ffmpeg, hardware_decode=False, force_overwrite=True
            )
            code, stderr_text = run_ffmpeg(
                retry,
                label,
                slot.cpu_ids,
                affinity_enabled,
                controller,
                progress_handler,
                task_key,
            )
        if controller.cancelled or controller.task_cancelled(task_key):
            remove_partial_output(task)
            detail = "批处理已取消" if controller.cancelled else "任务已取消 / 停止"
            say(f"[取消] {label}：{detail}")
            results.append((task, False, detail))
            emit_event(
                "task_done",
                slot_id=slot.slot_id,
                task_index=task_index,
                status="cancelled",
                source=str(task.source),
                output=str(task.output),
            )
            continue
        if code == 0:
            kept_source, result_detail = preserve_source_when_output_is_larger(
                task, settings
            )
            emit_event(
                "task_verification_start",
                slot_id=slot.slot_id,
                task_index=task_index,
                source=str(task.source),
                output=str(task.output),
                filename=task.label,
            )
            say(f"[校验] {label}：正在使用 GPU / NVDEC 完整解码最终视频流")
            valid_output, validation_detail = validate_existing_output(
                task.output,
                task.info,
                ffprobe,
                ffmpeg,
                settings.codec,
                settings.resolution,
                settings.fps_value,
                controller,
                None if task.is_clip else task.source,
                validation_gpus or (slot.gpu,),
                task_key,
            )
            if controller.cancelled or controller.task_cancelled(task_key):
                remove_partial_output(task)
                say(f"[取消] {label}：输出校验已取消")
                detail = "批处理已取消" if controller.cancelled else "任务已取消 / 停止"
                results.append((task, False, detail))
                emit_event(
                    "task_done",
                    slot_id=slot.slot_id,
                    task_index=task_index,
                    status="cancelled",
                    source=str(task.source),
                    output=str(task.output),
                )
                continue
            if not valid_output:
                remove_partial_output(task)
                detail = f"编码完成，但最终输出未通过 GPU 校验：{validation_detail}"
                say(f"[失败] {label}\n{detail}")
                results.append((task, False, detail))
                completed_equivalent_frames += equivalent_frames
                emit_event(
                    "task_done",
                    slot_id=slot.slot_id,
                    task_index=task_index,
                    status="failed",
                    source=str(task.source),
                    output=str(task.output),
                    error=detail,
                    completed_equivalent_frames=completed_equivalent_frames,
                    queue_equivalent_frames=queue_equivalent_frames,
                )
                continue
            result_detail = (
                f"{result_detail}；{validation_detail}"
                if result_detail
                else validation_detail
            )
            if source_timestamps is not None:
                try:
                    apply_file_timestamps(task.output, source_timestamps)
                except OSError as exc:
                    warn(f"{label}：无法保留源文件的创建/修改时间：{exc}")
                    timestamp_detail = f"无法保留源文件时间戳：{exc}"
                    result_detail = (
                        f"{result_detail}；{timestamp_detail}"
                        if result_detail else timestamp_detail
                    )
            if kept_source:
                say(f"[保留原文件] {label}：{result_detail} -> {task.output}")
            else:
                say(f"[完成] {label} -> {task.output}")
            results.append((task, True, result_detail))
            completed_equivalent_frames += equivalent_frames
            emit_event(
                "task_done",
                slot_id=slot.slot_id,
                task_index=task_index,
                status="success",
                source=str(task.source),
                output=str(task.output),
                input_size=task.source.stat().st_size if task.source.exists() else None,
                output_size=task.output.stat().st_size if task.output.exists() else None,
                kept_source=kept_source,
                detail=result_detail,
                completed_equivalent_frames=completed_equivalent_frames,
                queue_equivalent_frames=queue_equivalent_frames,
            )
        else:
            detail = error_tail(stderr_text) or f"FFmpeg 退出码 {code}"
            say(f"[失败] {label}\n{detail}")
            results.append((task, False, detail))
            completed_equivalent_frames += equivalent_frames
            emit_event(
                "task_done",
                slot_id=slot.slot_id,
                task_index=task_index,
                status="failed",
                source=str(task.source),
                output=str(task.output),
                error=detail,
                completed_equivalent_frames=completed_equivalent_frames,
                queue_equivalent_frames=queue_equivalent_frames,
            )
    return results


def doctor(ffmpeg: str, ffprobe: str, nvidia_smi: str, gpus: Sequence[GpuCapability]) -> None:
    for executable, label in ((ffmpeg, "ffmpeg"), (ffprobe, "ffprobe"), (nvidia_smi, "nvidia-smi")):
        if shutil.which(executable) is None and not Path(executable).is_file():
            raise TranscodeError(f"找不到 {label}：{executable}")
    detected = detect_nvidia_gpus(nvidia_smi)
    usable = validate_configured_gpus(gpus, detected, None)
    say("环境检查通过：")
    for gpu in usable:
        say(
            f"  GPU {gpu.index}: {gpu.name} | 编码={','.join(sorted(gpu.encode)) or '无'} "
            f"| 解码={','.join(sorted(gpu.decode)) or '无'} | 并行槽={gpu.encoder_engines}"
        )
    for codec in ("hevc", "av1"):
        try:
            verify_ffmpeg_encoder(ffmpeg, codec)
            say(f"  FFmpeg: {codec}_nvenc 可用")
        except TranscodeError as exc:
            warn(str(exc))


class DetailedHelpFormatter(
    argparse.ArgumentDefaultsHelpFormatter,
    argparse.RawDescriptionHelpFormatter,
):
    """Keep examples readable while still displaying argument defaults."""


def build_parser() -> argparse.ArgumentParser:
    script_dir = Path(__file__).resolve().parent
    parser = argparse.ArgumentParser(
        description="""按 GPU 编解码能力和 NVENC 编码核心数量，并行压制 HEVC/AV1 视频。

码率模式：NVENC CQ-VBR（类似 CRF）
  * 使用 -rc vbr -cq N -b:v 0，不设置目标平均码率，也不设置最低码率。
  * CQ 越低质量越高、文件通常越大；CQ 越高压缩越强。
  * 只有显式传入 --maxrate 才设置峰值限制；省略时不会传 maxrate/bufsize。
  * 源平均码率只用于显示，绝不会拿来限制 VBR 视频的瞬时峰值。

默认行为：输出 HEVC；保持源分辨率、帧率和时间戳；复制音频、字幕和元数据。""",
        epilog="""常用示例：
  # 默认 HEVC、CQ 23、保持分辨率和帧率
  python CodecFoundry.pyw --cli a.mp4 b.mkv

  # AV1，质量更高；显式限制峰值为 20M
  python CodecFoundry.pyw --cli videos --recursive --codec av1 --cq 19 --maxrate 20M

  # HEVC，输出 1080p/30fps，统一放入 compressed 目录
  python CodecFoundry.pyw --cli a.mp4 b.mp4 --resolution 1920x1080 --fps 30 \\
      --output-dir compressed

  # 只查看 GPU/CPU 调度及最终 FFmpeg 命令
  python CodecFoundry.pyw --cli videos --recursive --dry-run

码率说明：
  源码率仅供计划信息显示。VBR 的平均码率不能代表复杂场景的瞬时需求，因此不会自动
  变成 maxrate。显式设置 --maxrate 后，--bufsize 默认等于 maxrate，即约一秒 VBV
  窗口。过低的 maxrate 可能使编码器无法达到指定 CQ 质量。

中断说明：
  Ctrl+C 会设置全局取消状态并终止全部活动 FFmpeg 进程组，不会继续队列中的下一个视频。

输出与覆盖：
  输出保留输入文件名，不再添加 _hevc/_av1；扩展名由 --container 决定。若输出会与源
  文件成为同一路径，则自动写入源目录下的 compressed 子目录，绝不原地覆盖源文件。
  已有输出会先比较编码、分辨率、帧率和时长，再用 NVIDIA GPU / NVDEC 完整解码视频流：
  检查通过才跳过；损坏、无法硬件解码或时长不一致时自动覆盖重做。每次编码结束后还会对
  最终输出执行同样的 GPU 校验，校验失败的文件不会报告成功。--overwrite 只跳过已有输出
  预检，不会跳过编码后的最终校验。校验绝不回退到 CPU 软件视频解码。
  若源文件含 MP4 不兼容的音频或位图字幕，可改用 --container mkv。

进度与报告：
  GUI 分别显示每个 E 槽的当前文件、队列和总进度/ETA。队列及总 ETA 按 1080p 等效帧
  估算：帧数 × 输出像素数 / 1920 / 1080。每个槽使用最近 10 秒的平均速率，总 ETA 取
  所有槽队列 ETA 的最大值，进度每 0.5 秒更新。任务按等效工作量而非文件数分配。
  批处理完成或取消后都会生成 UTF-8 TXT 报告，包含逐文件状态、尺寸变化和总计 GB。
  GUI 每秒检查一次窗口比例；超宽窗口在右侧显示完整任务状态列表。关闭窗口时会先停止
  任务、等待资源释放、保存 GUI 日志，再退出。""",
        formatter_class=DetailedHelpFormatter,
    )
    parser.add_argument("inputs", nargs="*", help="一个或多个视频文件/目录；目录默认只扫描一层")
    parser.add_argument("--hlm", type=Path, help="读取 FlashCut HLM，并直接从其中记录的原片建立 HL 队列")
    parser.add_argument(
        "--hlm-job", action="append", default=[],
        help="只运行指定 HLM 任务 id；可重复传入，默认运行全部任务",
    )
    parser.add_argument("--version", action="version", version=f"%(prog)s {CODECFOUNDRY_VERSION}")
    parser.add_argument(
        "--codec", choices=("hevc", "nvhevc", "av1"), default="hevc",
        help="输出编码；nvhevc 是 hevc 的别名，可省略（默认 HEVC）",
    )
    parser.add_argument(
        "--cq", type=float, default=23.0,
        help="CQ-VBR 目标质量，0=NVENC 自动；数值越低质量越高、体积通常越大",
    )
    parser.add_argument(
        "--maxrate", type=parse_bitrate,
        help="峰值码率上限，如 12M；只有显式传入才启用，省略表示无限制",
    )
    parser.add_argument(
        "--bufsize", type=parse_bitrate,
        help="VBV 缓冲大小；有有效 maxrate 时默认等于 maxrate（约一秒窗口）",
    )
    parser.add_argument(
        "--fps", type=parse_fps,
        help="输出帧率，例如 30、59.94、60000/1001；不传则保持源帧率和时间戳",
    )
    parser.add_argument(
        "--resolution", type=parse_resolution,
        help="输出宽x高，例如 1920x1080；不传则保持，不自动维持宽高比",
    )
    parser.add_argument(
        "--preset", choices=tuple(f"p{i}" for i in range(1, 8)), default="p7",
        help="NVENC 速度/质量预设；p1 最快，p7 最慢且质量最高",
    )
    parser.add_argument("--lookahead", type=int, default=20, help="码率控制前瞻帧数，范围 0-32")
    parser.add_argument(
        "--multipass", choices=("disabled", "qres", "fullres"), default="fullres",
        help="NVENC 每帧分析：关闭、四分之一分辨率首遍、全分辨率双遍",
    )
    parser.add_argument(
        "--gpu-config", type=Path, default=script_dir / "gpus.json",
        help="GPU 编码/解码能力与编码核心数量 JSON",
    )
    parser.add_argument("--gpu", type=int, action="append", help="仅使用此 GPU index；可重复传入")
    parser.add_argument("--output-dir", type=Path, help="统一输出目录；不传则与各输入同目录")
    parser.add_argument(
        "--container", choices=("mkv", "mp4"), default="mp4",
        help="输出容器；MP4 更通用，MKV 更适合原样保留各种音频/字幕流",
    )
    parser.add_argument("--no-subtitles", action="store_true", help="不映射、不复制字幕流")
    parser.add_argument("--recursive", action="store_true", help="递归扫描输入目录中的视频")
    parser.add_argument("--overwrite", action="store_true", help="允许覆盖已有输出文件")
    parser.add_argument(
        "--no-cpu-affinity", action="store_true",
        help="关闭并行 FFmpeg 进程的互斥逻辑核心组分配",
    )
    parser.add_argument(
        "--no-software-fallback", action="store_true",
        help="声明支持的硬件解码实际失败时，不自动切换 CPU 解码重试",
    )
    parser.add_argument("--dry-run", action="store_true", help="探测并显示调度/命令，但不执行编码")
    parser.add_argument(
        "--debug-progress", action="store_true",
        help="GUI 调试：即使不足四个活动槽也强制显示 E0-E3 进度卡",
    )
    parser.add_argument("--doctor", action="store_true", help="仅检查 FFmpeg、NVIDIA GPU 和能力配置")
    parser.add_argument("--ffmpeg", default="ffmpeg", help="FFmpeg 可执行文件名或路径")
    parser.add_argument("--ffprobe", default="ffprobe", help="ffprobe 可执行文件名或路径")
    parser.add_argument("--nvidia-smi", default="nvidia-smi", help="nvidia-smi 可执行文件名或路径")
    return parser


def main(
    argv: Sequence[str] | None = None,
    controller: ProcessController | None = None,
) -> int:
    parser = build_parser()
    raw_argv = list(argv) if argv is not None else sys.argv[1:]
    args = parser.parse_args(raw_argv)

    def option_present(name: str) -> bool:
        return any(argument == name or argument.startswith(f"{name}=") for argument in raw_argv)

    controller = controller or ProcessController()
    workers: list[threading.Thread] = []
    previous_signal_handlers: dict[int, object] = {}

    if threading.current_thread() is threading.main_thread():
        def handle_cancel_signal(signum, _frame) -> None:
            first_request = not controller.cancelled
            controller.cancel_event.set()
            if first_request:
                say("\n[取消] 收到 Ctrl+C，正在停止全部 FFmpeg 进程和待处理任务……")
            # Keep the signal handler itself short and safe. A second key press skips
            # the graceful wait and immediately reaches the force-kill fallback.
            grace = 1.0 if first_request else 0.0
            threading.Thread(
                target=controller.cancel,
                kwargs={"grace_seconds": grace},
                daemon=True,
            ).start()

        signals = [signal.SIGINT]
        if os.name == "nt" and hasattr(signal, "SIGBREAK"):
            signals.append(signal.SIGBREAK)
        for handled_signal in signals:
            previous_signal_handlers[handled_signal] = signal.getsignal(handled_signal)
            signal.signal(handled_signal, handle_cancel_signal)
    try:
        gpus = load_gpu_config(args.gpu_config.resolve())
        if args.doctor:
            doctor(args.ffmpeg, args.ffprobe, args.nvidia_smi, gpus)
            return 0
        if args.hlm and args.inputs:
            parser.error("--hlm 不能与普通视频/目录输入混用")
        if args.hlm_job and not args.hlm:
            parser.error("--hlm-job 必须与 --hlm 一起使用")
        hlm_document: dict | None = None
        if args.hlm:
            args.hlm = args.hlm.expanduser().resolve()
            hlm_document = load_hlm_document(args.hlm)
            defaults = hlm_processing_defaults(hlm_document, args.hlm)
            if not option_present("--codec"):
                args.codec = defaults["codec"]
            if not option_present("--container"):
                args.container = defaults["container"]
            if not option_present("--output-dir"):
                args.output_dir = defaults["output_dir"]
            if not option_present("--overwrite"):
                args.overwrite = defaults["overwrite"]
            if not option_present("--no-subtitles"):
                args.no_subtitles = not defaults["copy_subtitles"]
        elif not args.inputs:
            parser.error("至少需要一个输入文件/目录或 --hlm（也可使用 --doctor）")
        if not 0 <= args.cq <= 51:
            parser.error("--cq 必须在 0 到 51 之间")
        if not 0 <= args.lookahead <= 32:
            parser.error("--lookahead 必须在 0 到 32 之间")
        codec = "hevc" if args.codec == "nvhevc" else args.codec
        verify_ffmpeg_encoder(args.ffmpeg, codec)
        detected = detect_nvidia_gpus(args.nvidia_smi)
        selected = set(args.gpu) if args.gpu is not None else None
        usable_gpus = validate_configured_gpus(gpus, detected, selected)
        slots = create_slots(usable_gpus, codec)

        output_dir = args.output_dir.expanduser().resolve() if args.output_dir else None
        fps_text, fps_value = args.fps if args.fps else (None, None)
        settings = EncodeSettings(
            codec=codec,
            maxrate=args.maxrate,
            bufsize=args.bufsize,
            fps_text=fps_text,
            fps_value=fps_value,
            resolution=args.resolution,
            cq=args.cq,
            preset=args.preset,
            lookahead=args.lookahead,
            multipass=args.multipass,
            overwrite=args.overwrite,
            copy_subtitles=not args.no_subtitles,
        )
        if hlm_document is not None:
            all_tasks = make_hlm_tasks(
                hlm_document,
                args.hlm,
                output_dir,
                codec,
                args.container,
                args.ffprobe,
                args.ffmpeg,
                args.overwrite,
                controller,
                settings.resolution,
                settings.fps_value,
                usable_gpus,
                args.hlm_job,
            )
        else:
            inputs = expand_inputs(args.inputs, args.recursive)
            all_tasks = make_tasks(
                inputs,
                output_dir,
                codec,
                args.container,
                args.ffprobe,
                args.ffmpeg,
                args.overwrite,
                controller,
                settings.resolution,
                settings.fps_value,
                usable_gpus,
            )
        skipped_tasks = [task for task in all_tasks if task.skip_reason]
        tasks = [task for task in all_tasks if not task.skip_reason]
        for task in skipped_tasks:
            say(
                f"[跳过] {task.label}：{task.skip_reason}：{task.output}；"
                "使用 --overwrite 可重新压制"
            )
        for task in all_tasks:
            if task.overwrite_existing:
                warn(
                    f"[重做] {task.label}：已有输出未通过有效性检查，"
                    f"将自动覆盖：{task.replacement_reason}"
                )
        active_slots = schedule_tasks(tasks, slots, settings)

        def task_plan_item(task: VideoTask) -> dict[str, object]:
            width, height, output_fps = task_output_geometry(task, settings)
            return {
                "source": str(task.source),
                "output": str(task.output),
                "filename": task.label,
                "source_container": task.source.suffix.lstrip(".") or "未知",
                "source_width": task.info.width,
                "source_height": task.info.height,
                "source_fps": task.info.fps,
                "input_codec": task.info.codec,
                "input_size": task_input_size(task) or None,
                "total_frames": task_total_frames(task, settings),
                "equivalent_frames": task_equivalent_frames(task, settings),
                "width": width,
                "height": height,
                "fps": output_fps,
                "duration": task.info.duration,
                "external_id": task.external_id,
            }

        plan_slots = []
        for slot in active_slots:
            plan_slots.append(
                {
                    "slot_id": slot.slot_id,
                    "gpu_index": slot.gpu.index,
                    "engine": slot.engine,
                    "cpu_ids": list(slot.cpu_ids),
                    "queue_equivalent_frames": sum(
                        task_equivalent_frames(task, settings) for task in slot.tasks
                    ),
                    "tasks": [task_plan_item(task) for task in slot.tasks],
                }
            )
        emit_event(
            "plan",
            slots=plan_slots,
            display_slots=max(4 if args.debug_progress else 0, len(active_slots)),
            total_equivalent_frames=sum(
                task_equivalent_frames(task, settings) for task in tasks
            ),
            task_count=len(tasks),
            skipped_count=len(skipped_tasks),
            skipped_tasks=[task_plan_item(task) for task in skipped_tasks],
        )
        emit_event("status", text=f"任务已安排，正在启动 {len(active_slots)} 个编码槽……")

        say(
            f"计划：编码 {len(tasks)} 个，跳过 {len(skipped_tasks)} 个，"
            f"{len(active_slots)}/{len(slots)} 个并行 NVENC 槽，"
            f"{codec.upper()} CQ-VBR CQ={args.cq:g}，目标/最低码率未设置"
        )
        for slot in active_slots:
            queue_work = sum(task_equivalent_frames(task, settings) for task in slot.tasks)
            say(
                f"  E{slot.slot_id} | GPU {slot.gpu.index} 编码核心 {slot.engine + 1} | "
                f"CPU {','.join(map(str, slot.cpu_ids))} | {len(slot.tasks)} 个任务 | "
                f"1080p 等效 {queue_work:,.0f} 帧"
            )
            for task in slot.tasks:
                decode_mode = "GPU" if slot.gpu.can_decode(task.info.codec) else "CPU（将警告）"
                maxrate, _ = effective_rate_limit(task, settings)
                source_rate = format_bitrate(task.info.bitrate) if task.info.bitrate else "未知"
                rate_limit = format_bitrate(maxrate) if maxrate else "无限制"
                say(
                    f"    {task.source.name} [{task.info.codec.upper()} -> {codec.upper()}，"
                    f"{decode_mode} 解码，源码率 {source_rate}，有效 maxrate {rate_limit}]"
                )
                if args.dry_run:
                    command = build_ffmpeg_command(
                        task, slot.gpu, settings, args.ffmpeg, slot.gpu.can_decode(task.info.codec)
                    )
                    say(f"    $ {command_text(command)}")
        if args.dry_run:
            return 0

        started_at = datetime.now()
        result_queue: queue.Queue[list[tuple[VideoTask, bool, str]]] = queue.Queue()
        def worker(slot: EncoderSlot) -> None:
            result_queue.put(
                run_slot(
                    slot,
                    settings,
                    args.ffmpeg,
                    not args.no_cpu_affinity,
                    not args.no_software_fallback,
                    controller,
                    args.ffprobe,
                    usable_gpus,
                )
            )

        for slot in active_slots:
            thread = threading.Thread(target=worker, args=(slot,), daemon=True)
            workers.append(thread)
            thread.start()
        # Timed joins keep the main thread responsive to one Ctrl+C on Windows.
        while any(thread.is_alive() for thread in workers):
            for thread in workers:
                thread.join(timeout=0.2)

        results: list[tuple[VideoTask, bool, str]] = [
            (task, False, f"跳过：{task.skip_reason}") for task in skipped_tasks
        ]
        while not result_queue.empty():
            results.extend(result_queue.get())
        succeeded = [item for item in results if item[1]]
        cancelled = [item for item in results if not item[1] and "取消" in item[2]]
        skipped = [item for item in results if not item[1] and item[2].startswith("跳过：")]
        failed = [
            item for item in results
            if not item[1] and "取消" not in item[2] and not item[2].startswith("跳过：")
        ]
        report_path: Path | None = None
        try:
            report_path = write_compression_report(
                all_tasks,
                results,
                settings,
                output_dir,
                started_at,
                datetime.now(),
            )
            say(f"压缩报告：{report_path}")
            emit_event("report", path=str(report_path))
        except OSError as exc:
            warn(f"无法写入压缩报告：{exc}")
        say(
            f"汇总：成功 {len(succeeded)}，失败 {len(failed)}，"
            f"取消 {len(cancelled)}，跳过 {len(skipped)}"
        )
        emit_event(
            "batch_done",
            success=len(succeeded),
            failed=len(failed),
            cancelled=len(cancelled),
            skipped=len(skipped),
            report_path=str(report_path) if report_path else None,
        )
        if controller.cancelled:
            if report_path:
                say("批处理已全部取消，没有继续启动后续视频；压缩报告已写入。")
            else:
                say("批处理已全部取消，没有继续启动后续视频；压缩报告写入失败。")
            return 130
        return 1 if failed else 0
    except TranscodeError as exc:
        if OUTPUT_CALLBACK is not None or sys.stderr is None:
            say(f"错误：{exc}")
        else:
            print(f"错误：{exc}", file=sys.stderr)
        return 2
    except KeyboardInterrupt:
        say("\n[取消] 收到 Ctrl+C，正在停止全部 FFmpeg 进程和待处理任务……")
        try:
            controller.cancel()
        except KeyboardInterrupt:
            controller.cancel(grace_seconds=0)
        deadline = time.monotonic() + 5
        for thread in workers:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                break
            thread.join(timeout=remaining)
        say("[取消] 全部编码任务已停止。")
        return 130
    finally:
        for handled_signal, previous_handler in previous_signal_handlers.items():
            signal.signal(handled_signal, previous_handler)


from PySide6.QtCore import QPoint, Qt, QTimer, QUrl, Signal, QObject
from PySide6.QtGui import QColor, QDesktopServices, QFont, QIcon, QPalette
from PySide6.QtWidgets import (
    QAbstractItemView,
    QApplication,
    QCheckBox,
    QComboBox,
    QDialog,
    QFileDialog,
    QFrame,
    QGridLayout,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QListWidget,
    QMainWindow,
    QMenu,
    QMessageBox,
    QPlainTextEdit,
    QProgressBar,
    QPushButton,
    QScrollArea,
    QSpinBox,
    QSplitter,
    QToolButton,
    QVBoxLayout,
    QWidget,
)


APP_NAME = "CodecFoundry"
APP_VERSION = CODECFOUNDRY_VERSION
APP_TITLE = f"{APP_NAME} v{APP_VERSION}"
APP_ICON_PATH = Path(__file__).resolve().parent / "zxtdu-a9102-001.dll"
AUTHOR_NAME = "Bilibili@O-TREE"
AUTHOR_URL = "https://space.bilibili.com/668497683/"
AUTHOR_GITHUB_URL = "https://github.com/YCTS-otree/"
_DATA_DIR_OVERRIDE = os.environ.get("CODECFOUNDRY_DATA_DIR")
_ROAMING_ROOT = Path(os.environ.get("APPDATA") or (Path.home() / "AppData" / "Roaming"))
APP_DATA_DIR = (
    Path(_DATA_DIR_OVERRIDE).expanduser()
    if _DATA_DIR_OVERRIDE
    else _ROAMING_ROOT / APP_NAME
)
APP_LOG_DIR = APP_DATA_DIR / "logs"
APP_LOG_PATH = APP_LOG_DIR / "CodecFoundry.log"
APP_SETTINGS_PATH = APP_DATA_DIR / "settings.json"
DEFAULT_PREFERENCES: dict[str, object] = {
    "codec": "HEVC（默认）",
    "cq": "23",
    "maxrate": "",
    "bufsize": "",
    "resolution": "",
    "fps": "",
    "preset": "p7",
    "multipass": "fullres",
    "lookahead": "30",
    "container": "mp4",
    "output_dir": "",
    "recursive": False,
    "overwrite": False,
    "copy_subtitles": True,
    "cpu_affinity": True,
    "debug_progress": False,
    "logging_enabled": False,
    "log_max_entries": 8,
    "log_max_size_mb": 8,
    "window_width": 1180,
    "window_height": 900,
    "window_maximized": False,
}
COLOR_BG = "#1e1e1e"
COLOR_PANEL = "#252526"
COLOR_ENTRY = "#2e2e2e"
COLOR_TOOLBAR = "#3c3c3c"
COLOR_TEXT = "#f2f2f2"
COLOR_MUTED = "#a8a8a8"
COLOR_BLUE = "#0e639c"
COLOR_BLUE_HOVER = "#1177bb"
COLOR_ORANGE = "#cc6633"
COLOR_PURPLE = "#68217a"
COLOR_RED = "#c42b1c"
COLOR_WAITING = "#7089a8"
COLOR_DONE = "#42a85a"
COLOR_RUNNING = "#ef4444"
COLOR_FAILED = "#e58a45"


def load_application_icon() -> QIcon | None:
    """Load the optional source-tree ICO without overriding an EXE resource icon."""
    if not APP_ICON_PATH.is_file():
        return None
    icon = QIcon(str(APP_ICON_PATH))
    return None if icon.isNull() else icon


APP_STYLESHEET = f"""
QWidget {{
    color: {COLOR_TEXT};
    font-family: "Microsoft YaHei UI", "Microsoft YaHei", sans-serif;
    font-size: 10pt;
}}
QMainWindow, QDialog, QWidget#windowSurface, QWidget#body, QWidget#contentHost {{
    background: {COLOR_BG};
}}
QWidget#titleBar {{
    background: {COLOR_TOOLBAR};
    border-bottom: 1px solid #505050;
}}
QLabel#appMark {{
    background: {COLOR_BLUE};
    color: white;
    border-radius: 4px;
    font-weight: 700;
    padding: 2px 5px;
}}
QLabel#windowTitle {{ color: #d8d8d8; font-size: 9pt; }}
QToolButton#menuButton, QPushButton#windowButton {{
    background: transparent;
    color: #d0d0d0;
    border: none;
    padding: 0 12px;
}}
QToolButton#menuButton:hover, QPushButton#windowButton:hover {{ background: #505050; }}
QPushButton#closeButton {{ background: transparent; border: none; color: #d0d0d0; }}
QPushButton#closeButton:hover {{ background: #e81123; color: white; }}
QMenu {{
    background: {COLOR_TOOLBAR};
    color: #e6e6e6;
    border: 1px solid #555555;
    padding: 5px 0;
}}
QMenu::item {{ padding: 7px 34px 7px 22px; }}
QMenu::item:selected {{ background: #094771; }}
QMenu::separator {{ height: 1px; background: #555555; margin: 5px 9px; }}
QLabel#brand {{ color: #d8d8d8; font-size: 24pt; font-weight: 700; }}
QLabel#tagline {{ color: #a0a0a0; font-size: 11pt; padding: 1px 6px 7px 6px; }}
QFrame#headerDivider {{ background: #4a4a4a; border: none; }}
QLabel#sectionTitle {{ color: white; font-size: 11pt; font-weight: 600; }}
QFrame#panel, QWidget#panel {{
    background: {COLOR_PANEL};
    border: 1px solid #343434;
    border-radius: 5px;
}}
QLineEdit, QComboBox, QSpinBox, QListWidget, QPlainTextEdit {{
    background: {COLOR_ENTRY};
    color: {COLOR_TEXT};
    border: 1px solid #484848;
    border-radius: 3px;
    selection-background-color: {COLOR_BLUE};
    padding: 5px 7px;
}}
QLineEdit:focus, QComboBox:focus, QSpinBox:focus, QListWidget:focus, QPlainTextEdit:focus {{
    border-color: {COLOR_BLUE_HOVER};
}}
QComboBox::drop-down {{ border: none; width: 24px; }}
QComboBox QAbstractItemView {{
    background: {COLOR_ENTRY};
    color: {COLOR_TEXT};
    selection-background-color: {COLOR_BLUE};
}}
QCheckBox {{ color: #d0d0d0; spacing: 7px; }}
QCheckBox::indicator {{ width: 15px; height: 15px; }}
QCheckBox::indicator:unchecked {{ background: {COLOR_ENTRY}; border: 1px solid #666666; }}
QCheckBox::indicator:checked {{ background: {COLOR_BLUE}; border: 1px solid {COLOR_BLUE_HOVER}; }}
QPushButton#primaryButton, QPushButton#smallButton {{
    background: {COLOR_BLUE};
    color: white;
    border: none;
    border-radius: 3px;
    padding: 7px 14px;
}}
QPushButton#primaryButton:hover, QPushButton#smallButton:hover {{ background: {COLOR_BLUE_HOVER}; }}
QPushButton#dangerButton {{
    background: {COLOR_RED};
    color: white;
    border: none;
    border-radius: 3px;
    padding: 7px 14px;
}}
QPushButton#dangerButton:hover {{ background: #e81123; }}
QPushButton#taskCancelButton, QPushButton#taskStopButton,
QPushButton#taskRestartButton, QPushButton#taskOpenButton {{
    color: white;
    border: none;
    border-radius: 3px;
    padding: 5px 12px;
    min-width: 72px;
}}
QPushButton#taskCancelButton {{ background: {COLOR_ORANGE}; }}
QPushButton#taskCancelButton:hover {{ background: #e1773e; }}
QPushButton#taskStopButton {{ background: {COLOR_RED}; }}
QPushButton#taskStopButton:hover {{ background: #e81123; }}
QPushButton#taskRestartButton, QPushButton#taskOpenButton {{ background: {COLOR_BLUE}; }}
QPushButton#taskRestartButton:hover, QPushButton#taskOpenButton:hover {{ background: {COLOR_BLUE_HOVER}; }}
QPushButton:disabled {{ background: #3b3b3b; color: #777777; }}
QProgressBar {{
    background: #181818;
    border: 1px solid #343434;
    border-radius: 3px;
    min-height: 12px;
    max-height: 12px;
}}
QProgressBar#overallProgress {{ min-height: 15px; max-height: 15px; }}
QProgressBar#orangeProgress::chunk {{ background: {COLOR_ORANGE}; border-radius: 2px; }}
QProgressBar#blueProgress::chunk {{ background: {COLOR_BLUE}; border-radius: 2px; }}
QProgressBar#overallProgress::chunk {{ background: {COLOR_ORANGE}; border-radius: 2px; }}
QFrame#engineViewport {{
    background: #171717;
    border: 1px solid #3f3f3f;
    border-radius: 4px;
}}
QFrame#engineViewport QScrollArea, QFrame#engineViewport QWidget#engineHost {{
    background: #171717;
}}
QScrollArea {{ background: transparent; border: none; }}
QScrollBar:vertical {{ background: #202020; width: 10px; margin: 0; }}
QScrollBar::handle:vertical {{ background: #565656; min-height: 28px; border-radius: 4px; }}
QScrollBar::add-line:vertical, QScrollBar::sub-line:vertical {{ height: 0; }}
QSplitter::handle {{ background: #353535; }}
QLabel#statusBar {{ background: {COLOR_PURPLE}; color: white; padding: 0 9px; font-size: 8pt; }}
QLabel#hint {{ color: {COLOR_MUTED}; font-size: 9pt; }}
QLabel#cardTitle {{ font-weight: 600; }}
"""


class BackendBridge(QObject):
    log_message = Signal(str)
    backend_event = Signal(str, object)
    finished = Signal(int)


class AppLogManager:
    """Live rotating application log stored under the user's Roaming profile."""

    def __init__(self, enabled: bool, max_entries: int, max_size_mb: int) -> None:
        self.logger = logging.getLogger(f"CodecFoundry.{id(self)}")
        self.logger.setLevel(logging.INFO)
        self.logger.propagate = False
        self.handler: RotatingFileHandler | None = None
        self.enabled = False
        self.last_error: str | None = None
        self.configure(enabled, max_entries, max_size_mb)

    def configure(self, enabled: bool, max_entries: int, max_size_mb: int) -> None:
        self.close()
        self.enabled = bool(enabled)
        self.last_error = None
        if not self.enabled:
            return
        try:
            APP_LOG_DIR.mkdir(parents=True, exist_ok=True)
            handler = RotatingFileHandler(
                APP_LOG_PATH,
                maxBytes=max(1, int(max_size_mb)) * 1024 * 1024,
                backupCount=max(1, int(max_entries)),
                encoding="utf-8",
                delay=True,
            )
            handler.setFormatter(logging.Formatter("%(asctime)s | %(message)s"))
            self.logger.addHandler(handler)
            self.handler = handler
        except OSError as exc:
            self.enabled = False
            self.last_error = str(exc)

    def write(self, message: str) -> None:
        if self.enabled and self.handler is not None:
            self.logger.info(str(message).rstrip())

    def close(self) -> None:
        if self.handler is None:
            return
        self.logger.removeHandler(self.handler)
        self.handler.close()
        self.handler = None


class DropInputList(QListWidget):
    """Input list that accepts local video files and directories."""

    paths_dropped = Signal(object)

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.setAcceptDrops(True)

    def dragEnterEvent(self, event) -> None:
        if event.mimeData().hasUrls() and any(
            url.isLocalFile() for url in event.mimeData().urls()
        ):
            event.acceptProposedAction()
            return
        event.ignore()

    def dragMoveEvent(self, event) -> None:
        if event.mimeData().hasUrls():
            event.acceptProposedAction()
            return
        event.ignore()

    def dropEvent(self, event) -> None:
        paths = [url.toLocalFile() for url in event.mimeData().urls() if url.isLocalFile()]
        if paths:
            self.paths_dropped.emit(paths)
            event.acceptProposedAction()
            return
        event.ignore()


class TitleBar(QWidget):
    """Frameless title/menu bar inspired by CPVC's compact dark toolbar."""

    def __init__(self, window: "CodecFoundryWindow") -> None:
        super().__init__(window)
        self.host_window = window
        self._drag_origin: QPoint | None = None
        self._press_global: QPoint | None = None
        self._drag_moved = False
        self._native_system_move = False
        self.setObjectName("titleBar")
        self.setFixedHeight(40)

        layout = QHBoxLayout(self)
        layout.setContentsMargins(7, 0, 0, 0)
        layout.setSpacing(0)

        mark = QLabel("CF")
        mark.setObjectName("appMark")
        mark.setAlignment(Qt.AlignmentFlag.AlignCenter)
        mark.setFixedSize(29, 25)
        layout.addWidget(mark)
        layout.addSpacing(5)

        self._add_menu_button(layout, "文件", self._file_menu())
        self._add_action_button(layout, "设置", self.host_window.show_settings)
        self._add_menu_button(layout, "帮助", self._help_menu())

        title = QLabel(APP_TITLE)
        title.setObjectName("windowTitle")
        title.setAlignment(Qt.AlignmentFlag.AlignCenter)
        title.setAttribute(Qt.WidgetAttribute.WA_TransparentForMouseEvents)
        layout.addWidget(title, 1)

        self.minimize_button = self._window_button("—", "最小化", "windowButton")
        self.maximize_button = self._window_button("□", "最大化 / 还原", "windowButton")
        self.close_button = self._window_button("×", "安全退出", "closeButton")
        self.minimize_button.clicked.connect(window.showMinimized)
        self.maximize_button.clicked.connect(window.toggle_maximized)
        self.close_button.clicked.connect(window.close)
        layout.addWidget(self.minimize_button)
        layout.addWidget(self.maximize_button)
        layout.addWidget(self.close_button)

    def _window_button(self, text: str, tooltip: str, name: str) -> QPushButton:
        button = QPushButton(text)
        button.setObjectName(name)
        button.setToolTip(tooltip)
        button.setFixedSize(52, 39)
        button.setFont(QFont("Microsoft YaHei UI", 12))
        return button

    def _add_menu_button(self, layout: QHBoxLayout, text: str, menu: QMenu) -> None:
        button = QToolButton()
        button.setObjectName("menuButton")
        button.setText(text)
        button.setPopupMode(QToolButton.ToolButtonPopupMode.InstantPopup)
        button.setMenu(menu)
        button.setFixedHeight(39)
        layout.addWidget(button)

    def _add_action_button(self, layout: QHBoxLayout, text: str, callback) -> None:
        button = QToolButton()
        button.setObjectName("menuButton")
        button.setText(text)
        button.setFixedHeight(39)
        button.setCursor(Qt.CursorShape.PointingHandCursor)
        button.clicked.connect(callback)
        layout.addWidget(button)

    def _file_menu(self) -> QMenu:
        menu = QMenu(self)
        menu.addAction("添加视频…", self.host_window.add_files)
        menu.addAction("添加目录…", self.host_window.add_directory)
        menu.addAction("选择输出目录…", self.host_window.choose_output)
        menu.addSeparator()
        menu.addAction("退出", self.host_window.close)
        return menu

    def _help_menu(self) -> QMenu:
        menu = QMenu(self)
        menu.addAction("详细帮助", self.host_window.show_help)
        menu.addAction("关于 CodecFoundry", self.host_window.show_about)
        return menu

    def mousePressEvent(self, event) -> None:
        if event.button() == Qt.MouseButton.LeftButton:
            global_position = event.globalPosition().toPoint()
            self._press_global = global_position
            self._drag_origin = global_position - self.host_window.frameGeometry().topLeft()
            self._drag_moved = False
            self._native_system_move = False
            event.accept()

    def mouseMoveEvent(self, event) -> None:
        if self._drag_origin is not None and event.buttons() & Qt.MouseButton.LeftButton:
            global_position = event.globalPosition().toPoint()
            if self._press_global is not None and (
                global_position - self._press_global
            ).manhattanLength() < QApplication.startDragDistance():
                event.accept()
                return
            self._drag_moved = True
            window_handle = self.host_window.windowHandle()
            if window_handle is not None:
                try:
                    if window_handle.startSystemMove():
                        # Windows now owns the move loop and provides native Aero
                        # Snap, including maximize-on-release at the screen top.
                        self._native_system_move = True
                        self._drag_origin = None
                        event.accept()
                        return
                except RuntimeError:
                    # Some non-native/offscreen platforms do not implement it.
                    pass
            if self.host_window.isMaximized():
                self.host_window.set_maximized(False)
                self._drag_origin = QPoint(self.host_window.width() // 2, 20)
            self.host_window.move(global_position - self._drag_origin)
            event.accept()

    def mouseReleaseEvent(self, event) -> None:
        should_snap = (
            event.button() == Qt.MouseButton.LeftButton
            and self._drag_moved
            and not self._native_system_move
            and self._is_at_screen_top(event.globalPosition().toPoint())
        )
        self._drag_origin = None
        self._press_global = None
        self._drag_moved = False
        self._native_system_move = False
        if should_snap and not self.host_window.isMaximized():
            self.host_window.set_maximized(True)
            event.accept()
            return
        super().mouseReleaseEvent(event)

    @staticmethod
    def _is_at_screen_top(global_position: QPoint) -> bool:
        screen = QApplication.screenAt(global_position) or QApplication.primaryScreen()
        if screen is None:
            return False
        return abs(global_position.y() - screen.availableGeometry().top()) <= 10

    def mouseDoubleClickEvent(self, event) -> None:
        if event.button() == Qt.MouseButton.LeftButton:
            self.host_window.toggle_maximized()
            event.accept()


class SettingsDialog(QDialog):
    """Independent application-settings window."""

    def __init__(self, host: "CodecFoundryWindow") -> None:
        super().__init__(host)
        self.host = host
        self.setWindowTitle(f"{APP_NAME} 设置")
        if not host.windowIcon().isNull():
            self.setWindowIcon(host.windowIcon())
        self.setWindowFlag(Qt.WindowType.Tool, True)
        self.setWindowModality(Qt.WindowModality.NonModal)
        self.setAttribute(Qt.WidgetAttribute.WA_DeleteOnClose, True)
        self.setMinimumWidth(560)

        layout = QVBoxLayout(self)
        layout.setContentsMargins(22, 18, 22, 18)
        layout.setSpacing(12)

        title = QLabel("个性化设置")
        title.setObjectName("sectionTitle")
        description = QLabel(
            "设置会自动保存到 Roaming 配置目录，并在下次启动时恢复。"
        )
        description.setObjectName("hint")
        layout.addWidget(title)
        layout.addWidget(description)

        general_panel = QFrame()
        general_panel.setObjectName("panel")
        general_layout = QVBoxLayout(general_panel)
        general_layout.setContentsMargins(14, 12, 14, 12)
        self.debug_check = QCheckBox("调试模式：显示 E0-E3")
        self.debug_check.setChecked(host.debug_progress_enabled)
        general_layout.addWidget(self.debug_check)
        layout.addWidget(general_panel)

        log_panel = QFrame()
        log_panel.setObjectName("panel")
        log_layout = QGridLayout(log_panel)
        log_layout.setContentsMargins(14, 12, 14, 12)
        log_layout.setHorizontalSpacing(12)
        log_layout.setVerticalSpacing(10)
        self.logging_check = QCheckBox("开启软件主日志")
        self.logging_check.setChecked(host.logging_enabled)
        self.max_entries_spin = QSpinBox()
        self.max_entries_spin.setRange(1, 100)
        self.max_entries_spin.setValue(host.log_max_entries)
        self.max_entries_spin.setSuffix(" 个")
        self.max_size_spin = QSpinBox()
        self.max_size_spin.setRange(1, 1024)
        self.max_size_spin.setValue(host.log_max_size_mb)
        self.max_size_spin.setSuffix(" MB")
        log_layout.addWidget(self.logging_check, 0, 0, 1, 2)
        log_layout.addWidget(QLabel("最大历史日志条目"), 1, 0)
        log_layout.addWidget(self.max_entries_spin, 1, 1)
        log_layout.addWidget(QLabel("单个日志文件最大尺寸"), 2, 0)
        log_layout.addWidget(self.max_size_spin, 2, 1)
        log_layout.setColumnStretch(0, 1)
        self.logging_check.toggled.connect(self._sync_log_controls)
        self._sync_log_controls(self.logging_check.isChecked())
        layout.addWidget(log_panel)

        storage_label = QLabel(f"配置目录：{APP_DATA_DIR}\n主日志：{APP_LOG_PATH}")
        storage_label.setObjectName("hint")
        storage_label.setWordWrap(True)
        storage_label.setTextInteractionFlags(
            Qt.TextInteractionFlag.TextSelectableByMouse
        )
        layout.addWidget(storage_label)

        actions = QHBoxLayout()
        open_logs = QPushButton("打开日志文件夹")
        open_logs.setObjectName("primaryButton")
        open_logs.clicked.connect(host.open_log_folder)
        defaults = QPushButton("恢复此页默认")
        defaults.setObjectName("smallButton")
        defaults.clicked.connect(self._restore_defaults)
        cancel = QPushButton("取消")
        cancel.setObjectName("smallButton")
        cancel.clicked.connect(self.reject)
        save = QPushButton("保存设置")
        save.setObjectName("primaryButton")
        save.clicked.connect(self._save)
        actions.addWidget(open_logs)
        actions.addWidget(defaults)
        actions.addStretch(1)
        actions.addWidget(cancel)
        actions.addWidget(save)
        layout.addLayout(actions)

    def _sync_log_controls(self, enabled: bool) -> None:
        self.max_entries_spin.setEnabled(enabled)
        self.max_size_spin.setEnabled(enabled)

    def _restore_defaults(self) -> None:
        self.debug_check.setChecked(bool(DEFAULT_PREFERENCES["debug_progress"]))
        self.logging_check.setChecked(bool(DEFAULT_PREFERENCES["logging_enabled"]))
        self.max_entries_spin.setValue(int(DEFAULT_PREFERENCES["log_max_entries"]))
        self.max_size_spin.setValue(int(DEFAULT_PREFERENCES["log_max_size_mb"]))

    def _save(self) -> None:
        self.host.apply_settings_dialog(self)
        self.accept()


class CodecFoundryWindow(QMainWindow):
    def __init__(self) -> None:
        super().__init__()
        self.preferences = self._load_preferences()
        self.debug_progress_enabled = bool(self.preferences["debug_progress"])
        self.logging_enabled = bool(self.preferences["logging_enabled"])
        self.log_max_entries = int(self.preferences["log_max_entries"])
        self.log_max_size_mb = int(self.preferences["log_max_size_mb"])
        try:
            APP_DATA_DIR.mkdir(parents=True, exist_ok=True)
            APP_LOG_DIR.mkdir(parents=True, exist_ok=True)
            storage_error = None
        except OSError as exc:
            storage_error = str(exc)
        self.app_logger = AppLogManager(
            self.logging_enabled,
            self.log_max_entries,
            self.log_max_size_mb,
        )
        self.settings_window: SettingsDialog | None = None
        self.setWindowTitle(APP_TITLE)
        source_icon = load_application_icon()
        if source_icon is not None:
            self.setWindowIcon(source_icon)
        self.setWindowFlags(Qt.WindowType.Window | Qt.WindowType.FramelessWindowHint)
        self.setAcceptDrops(True)
        self.setMinimumSize(900, 680)
        self.resize(
            int(self.preferences["window_width"]),
            int(self.preferences["window_height"]),
        )

        self.bridge = BackendBridge(self)
        self.bridge.log_message.connect(self._log)
        self.bridge.backend_event.connect(self._handle_backend_event)
        self.bridge.finished.connect(self._finish)

        self.worker: threading.Thread | None = None
        self.controller: ProcessController | None = None
        self.closing = False
        self.close_finalized = False
        self.log_lines: list[str] = []
        self.log_saved_path: Path | None = None
        self.report_path: str | None = None
        self.batch_skipped_count = 0
        self.total_equivalent_frames = 0.0
        self.active_slot_files: dict[int, str] = {}
        self.slot_widgets: dict[int, dict[str, QWidget]] = {}
        self.slot_states: dict[int, dict[str, object]] = {}
        self.task_widgets: dict[str, dict[str, QWidget]] = {}
        self.task_records: dict[str, dict[str, object]] = {}
        self.hlm_path: Path | None = None
        self.last_batch_args: list[str] = []
        self.pending_restarts: dict[str, dict[str, object]] = {}
        self.restart_batch_active = False
        self.restart_batch_keys: set[str] = set()
        self.batch_cancelled_count = 0

        self._build_ui()
        self._apply_preferences()
        self._apply_responsive_layout()

        self.close_timer = QTimer(self)
        self.close_timer.setInterval(100)
        self.close_timer.timeout.connect(self._finish_close_when_ready)
        self._log(f"[启动] {APP_TITLE} · 配置目录：{APP_DATA_DIR}")
        if storage_error or self.app_logger.last_error:
            self._log(
                f"[警告] 无法初始化 Roaming 配置/日志目录："
                f"{storage_error or self.app_logger.last_error}"
            )

    @staticmethod
    def _load_preferences() -> dict[str, object]:
        preferences = dict(DEFAULT_PREFERENCES)
        try:
            loaded = json.loads(APP_SETTINGS_PATH.read_text(encoding="utf-8"))
            if isinstance(loaded, dict):
                for key in preferences:
                    if key in loaded:
                        preferences[key] = loaded[key]
        except (OSError, json.JSONDecodeError, UnicodeError):
            pass

        def limited_int(key: str, default: int, minimum: int, maximum: int) -> int:
            try:
                value = int(preferences.get(key) or default)
            except (TypeError, ValueError):
                value = default
            return max(minimum, min(maximum, value))

        preferences["log_max_entries"] = limited_int(
            "log_max_entries", 8, 1, 100
        )
        preferences["log_max_size_mb"] = limited_int(
            "log_max_size_mb", 8, 1, 1024
        )
        preferences["window_width"] = limited_int(
            "window_width", 1180, 900, 7680
        )
        preferences["window_height"] = limited_int(
            "window_height", 900, 680, 4320
        )
        return preferences

    def _build_ui(self) -> None:
        surface = QWidget()
        surface.setObjectName("windowSurface")
        self.setCentralWidget(surface)
        root = QVBoxLayout(surface)
        root.setContentsMargins(1, 1, 1, 1)
        root.setSpacing(0)
        self.title_bar = TitleBar(self)
        root.addWidget(self.title_bar)

        body = QWidget()
        body.setObjectName("body")
        body_layout = QVBoxLayout(body)
        body_layout.setContentsMargins(0, 0, 0, 0)
        body_layout.setSpacing(0)
        root.addWidget(body, 1)

        header = QWidget()
        header.setMinimumHeight(102)
        header.setMaximumHeight(118)
        header_layout = QVBoxLayout(header)
        header_layout.setContentsMargins(36, 14, 36, 8)
        header_layout.setSpacing(0)
        brand = QLabel(APP_NAME)
        brand.setObjectName("brand")
        brand.setAlignment(Qt.AlignmentFlag.AlignCenter)
        tagline = QLabel("NVIDIA GPU HEVC / AV1 · 多编码核心调度 · CQ-VBR")
        tagline.setObjectName("tagline")
        tagline.setAlignment(Qt.AlignmentFlag.AlignCenter)
        tagline.setMinimumHeight(29)
        tagline.setWordWrap(False)
        divider = QFrame()
        divider.setObjectName("headerDivider")
        divider.setFixedHeight(1)
        header_layout.addWidget(brand)
        header_layout.addWidget(tagline)
        header_layout.addWidget(divider)
        body_layout.addWidget(header)

        self.main_splitter = QSplitter(Qt.Orientation.Horizontal)
        self.main_splitter.setChildrenCollapsible(False)
        self.main_splitter.setHandleWidth(1)
        body_layout.addWidget(self.main_splitter, 1)

        self.left_shell = QWidget()
        self.left_shell.setObjectName("contentHost")
        left_layout = QVBoxLayout(self.left_shell)
        left_layout.setContentsMargins(0, 0, 0, 0)
        left_layout.setSpacing(0)
        self.main_splitter.addWidget(self.left_shell)

        self.content_scroll = QScrollArea()
        self.content_scroll.setWidgetResizable(True)
        self.content_scroll.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        content = QWidget()
        content.setObjectName("contentHost")
        content.setMinimumWidth(760)
        self.content_layout = QVBoxLayout(content)
        self.content_layout.setContentsMargins(36, 8, 30, 12)
        self.content_layout.setSpacing(12)
        self.content_scroll.setWidget(content)
        left_layout.addWidget(self.content_scroll, 1)

        self._build_inputs()
        self._build_parameters()
        self._build_progress()

        bottom = QWidget()
        bottom.setObjectName("contentHost")
        self.bottom_layout = QVBoxLayout(bottom)
        self.bottom_layout.setContentsMargins(36, 7, 30, 10)
        self.bottom_layout.setSpacing(8)
        self._build_log_panel()
        self._build_actions()
        left_layout.addWidget(bottom, 0)

        self._build_task_sidebar()
        self.main_splitter.addWidget(self.task_sidebar)
        self.main_splitter.setStretchFactor(0, 3)
        self.main_splitter.setStretchFactor(1, 1)

        self.status_label = QLabel("就绪 · 默认 HEVC / CQ 23 / 保持分辨率与帧率")
        self.status_label.setObjectName("statusBar")
        self.status_label.setFixedHeight(self._status_bar_height())
        body_layout.addWidget(self.status_label)

    def _apply_preferences(self) -> None:
        values = self.preferences
        self.codec_combo.setCurrentText(str(values["codec"]))
        self.cq_edit.setText(str(values["cq"]))
        self.maxrate_edit.setText(str(values["maxrate"]))
        self.bufsize_edit.setText(str(values["bufsize"]))
        self.resolution_edit.setText(str(values["resolution"]))
        self.fps_edit.setText(str(values["fps"]))
        self.preset_combo.setCurrentText(str(values["preset"]))
        self.multipass_combo.setCurrentText(str(values["multipass"]))
        self.lookahead_edit.setText(str(values["lookahead"]))
        self.container_combo.setCurrentText(str(values["container"]))
        self.output_edit.setText(str(values["output_dir"]))
        self.recursive_check.setChecked(bool(values["recursive"]))
        self.overwrite_check.setChecked(bool(values["overwrite"]))
        self.subtitle_check.setChecked(bool(values["copy_subtitles"]))
        self.affinity_check.setChecked(bool(values["cpu_affinity"]))
        self._toggle_debug_slots(self.debug_progress_enabled)
        if bool(values["window_maximized"]):
            QTimer.singleShot(0, lambda: self.set_maximized(True))

    def _collect_preferences(self) -> dict[str, object]:
        normal_size = self.normalGeometry().size() if self.isMaximized() else self.size()
        return {
            "codec": self.codec_combo.currentText(),
            "cq": self.cq_edit.text().strip(),
            "maxrate": self.maxrate_edit.text().strip(),
            "bufsize": self.bufsize_edit.text().strip(),
            "resolution": self.resolution_edit.text().strip(),
            "fps": self.fps_edit.text().strip(),
            "preset": self.preset_combo.currentText(),
            "multipass": self.multipass_combo.currentText(),
            "lookahead": self.lookahead_edit.text().strip(),
            "container": self.container_combo.currentText(),
            "output_dir": self.output_edit.text().strip(),
            "recursive": self.recursive_check.isChecked(),
            "overwrite": self.overwrite_check.isChecked(),
            "copy_subtitles": self.subtitle_check.isChecked(),
            "cpu_affinity": self.affinity_check.isChecked(),
            "debug_progress": self.debug_progress_enabled,
            "logging_enabled": self.logging_enabled,
            "log_max_entries": self.log_max_entries,
            "log_max_size_mb": self.log_max_size_mb,
            "window_width": max(900, normal_size.width()),
            "window_height": max(680, normal_size.height()),
            "window_maximized": self.isMaximized(),
        }

    def _save_preferences(self) -> bool:
        try:
            APP_DATA_DIR.mkdir(parents=True, exist_ok=True)
            temporary_path = APP_SETTINGS_PATH.with_suffix(".json.tmp")
            temporary_path.write_text(
                json.dumps(self._collect_preferences(), ensure_ascii=False, indent=2)
                + "\n",
                encoding="utf-8",
            )
            temporary_path.replace(APP_SETTINGS_PATH)
            return True
        except OSError as exc:
            self._log(f"[警告] 无法保存个性化设置：{exc}")
            return False

    def _section_header(self, text: str, controls: list[QWidget] | None = None) -> QWidget:
        row = QWidget()
        layout = QHBoxLayout(row)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(7)
        title = QLabel(text)
        title.setObjectName("sectionTitle")
        layout.addWidget(title)
        layout.addStretch(1)
        for control in controls or []:
            layout.addWidget(control)
        return row

    @staticmethod
    def _small_button(text: str) -> QPushButton:
        button = QPushButton(text)
        button.setObjectName("smallButton")
        button.setCursor(Qt.CursorShape.PointingHandCursor)
        button.setMinimumHeight(30)
        return button

    def _build_inputs(self) -> None:
        add_video = self._small_button("添加视频")
        add_folder = self._small_button("添加目录")
        remove = self._small_button("移除")
        clear = self._small_button("清空")
        add_video.clicked.connect(self.add_files)
        add_folder.clicked.connect(self.add_directory)
        remove.clicked.connect(self.remove_selected)
        clear.clicked.connect(self.clear_inputs)
        self.content_layout.addWidget(
            self._section_header("输入视频 / 目录（支持拖放）", [add_video, add_folder, remove, clear])
        )

        self.input_list = DropInputList()
        self.input_list.paths_dropped.connect(self._handle_dropped_paths)
        self.input_list.setSelectionMode(QAbstractItemView.SelectionMode.ExtendedSelection)
        self.input_list.setToolTip("可将视频文件或文件夹直接拖到此处")
        self.input_list.setMinimumHeight(82)
        self.input_list.setMaximumHeight(118)
        self.content_layout.addWidget(self.input_list)

        output_row = QWidget()
        row = QHBoxLayout(output_row)
        row.setContentsMargins(0, 0, 0, 0)
        row.setSpacing(8)
        label = QLabel("输出目录")
        label.setMinimumWidth(68)
        self.output_edit = QLineEdit()
        self.output_edit.setPlaceholderText("留空则输出到源目录")
        browse = self._small_button("浏览")
        browse.clicked.connect(self.choose_output)
        row.addWidget(label)
        row.addWidget(self.output_edit, 1)
        row.addWidget(browse)
        self.content_layout.addWidget(output_row)
        hint = QLabel("留空：输出到源目录；若与源文件同名同扩展名，则使用 compressed 子目录")
        hint.setObjectName("hint")
        hint.setContentsMargins(76, 0, 0, 0)
        self.content_layout.addWidget(hint)

    def _build_parameters(self) -> None:
        panel = QFrame()
        panel.setObjectName("panel")
        grid = QGridLayout(panel)
        grid.setContentsMargins(16, 13, 16, 13)
        grid.setHorizontalSpacing(14)
        grid.setVerticalSpacing(7)

        self.codec_combo = QComboBox()
        self.codec_combo.addItems(["HEVC（默认）", "AV1"])
        self.cq_edit = QLineEdit("23")
        self.maxrate_edit = QLineEdit()
        self.maxrate_edit.setPlaceholderText("如 20M")
        self.bufsize_edit = QLineEdit()
        self.bufsize_edit.setPlaceholderText("默认=maxrate")
        self.resolution_edit = QLineEdit()
        self.resolution_edit.setPlaceholderText("保持原分辨率")
        self.fps_edit = QLineEdit()
        self.fps_edit.setPlaceholderText("保持原帧率")
        self.preset_combo = QComboBox()
        self.preset_combo.addItems([f"p{i}" for i in range(1, 8)])
        self.preset_combo.setCurrentText("p7")
        self.multipass_combo = QComboBox()
        self.multipass_combo.addItems(["disabled", "qres", "fullres"])
        self.multipass_combo.setCurrentText("fullres")

        fields = [
            ("编码", self.codec_combo),
            ("CQ 质量", self.cq_edit),
            ("最大码率", self.maxrate_edit),
            ("VBV 缓冲", self.bufsize_edit),
            ("分辨率", self.resolution_edit),
            ("帧率", self.fps_edit),
            ("Preset", self.preset_combo),
            ("多遍分析", self.multipass_combo),
        ]
        for index, (label_text, widget) in enumerate(fields):
            row, column = divmod(index, 4)
            field_column = column * 2
            label = QLabel(label_text)
            label.setObjectName("hint")
            grid.addWidget(label, row * 2, field_column)
            grid.addWidget(widget, row * 2 + 1, field_column, 1, 2)
            grid.setColumnStretch(field_column + 1, 1)

        advanced = QWidget()
        advanced_layout = QHBoxLayout(advanced)
        advanced_layout.setContentsMargins(0, 4, 0, 0)
        advanced_layout.setSpacing(8)
        advanced_hint = QLabel("CQ 越低质量越高 · 最大码率留空 = 无限制")
        advanced_hint.setObjectName("hint")
        self.lookahead_edit = QLineEdit("30")
        self.lookahead_edit.setFixedWidth(58)
        self.container_combo = QComboBox()
        self.container_combo.addItems(["mp4", "mkv"])
        self.container_combo.setFixedWidth(74)
        advanced_layout.addWidget(advanced_hint, 1)
        advanced_layout.addWidget(QLabel("Lookahead"))
        advanced_layout.addWidget(self.lookahead_edit)
        advanced_layout.addWidget(QLabel("容器"))
        advanced_layout.addWidget(self.container_combo)
        grid.addWidget(advanced, 4, 0, 1, 8)

        checks = QWidget()
        checks_layout = QHBoxLayout(checks)
        checks_layout.setContentsMargins(0, 2, 0, 0)
        checks_layout.setSpacing(18)
        self.recursive_check = QCheckBox("递归扫描目录")
        self.overwrite_check = QCheckBox("覆盖已有输出")
        self.subtitle_check = QCheckBox("复制字幕")
        self.subtitle_check.setChecked(True)
        self.affinity_check = QCheckBox("CPU 核心分组")
        self.affinity_check.setChecked(True)
        for checkbox in (
            self.recursive_check,
            self.overwrite_check,
            self.subtitle_check,
            self.affinity_check,
        ):
            checks_layout.addWidget(checkbox)
        checks_layout.addStretch(1)
        grid.addWidget(checks, 5, 0, 1, 8)
        self.content_layout.addWidget(panel)

    def _build_progress(self) -> None:
        self.overall_detail = QLabel("总进度 0.0% · 等待任务")
        self.overall_detail.setObjectName("hint")
        self.content_layout.addWidget(
            self._section_header("编码引擎进度", [self.overall_detail])
        )
        self.overall_progress = QProgressBar()
        self.overall_progress.setObjectName("overallProgress")
        self.overall_progress.setRange(0, 1000)
        self.overall_progress.setTextVisible(False)
        self.content_layout.addWidget(self.overall_progress)

        engine_viewport = QFrame()
        engine_viewport.setObjectName("engineViewport")
        engine_viewport.setMinimumHeight(164)
        engine_layout = QVBoxLayout(engine_viewport)
        engine_layout.setContentsMargins(7, 7, 7, 7)
        engine_layout.setSpacing(0)
        self.slot_scroll = QScrollArea()
        self.slot_scroll.setWidgetResizable(True)
        self.slot_scroll.setFrameShape(QFrame.Shape.NoFrame)
        self.slot_scroll.setMinimumHeight(0)
        self.slot_host = QWidget()
        self.slot_host.setObjectName("engineHost")
        self.slot_layout = QGridLayout(self.slot_host)
        self.slot_layout.setContentsMargins(0, 0, 0, 0)
        self.slot_layout.setSpacing(7)
        self.slot_scroll.setWidget(self.slot_host)
        engine_layout.addWidget(self.slot_scroll)
        self.engine_viewport = engine_viewport
        self.content_layout.addWidget(engine_viewport, 1)

    def _build_log_panel(self) -> None:
        clear = self._small_button("清空日志")
        clear.clicked.connect(self.clear_log)
        self.bottom_layout.addWidget(self._section_header("运行信息", [clear]))
        self.log_edit = QPlainTextEdit()
        self.log_edit.setReadOnly(True)
        self.log_edit.setMinimumHeight(96)
        self.log_edit.setMaximumHeight(150)
        self.log_edit.setMaximumBlockCount(10000)
        self.log_edit.setFont(QFont("Consolas", 9))
        self.bottom_layout.addWidget(self.log_edit)

    def _build_actions(self) -> None:
        row = QWidget()
        layout = QHBoxLayout(row)
        layout.setContentsMargins(0, 0, 0, 0)
        help_button = QPushButton("详细帮助")
        help_button.setObjectName("primaryButton")
        help_button.clicked.connect(self.show_help)
        self.stop_button = QPushButton("停止全部")
        self.stop_button.setObjectName("dangerButton")
        self.stop_button.clicked.connect(self.stop_all)
        self.stop_button.setEnabled(False)
        self.start_button = QPushButton("开始压制")
        self.start_button.setObjectName("primaryButton")
        self.start_button.setMinimumWidth(180)
        self.start_button.clicked.connect(self.start)
        layout.addWidget(help_button)
        layout.addStretch(1)
        layout.addWidget(self.stop_button)
        layout.addWidget(self.start_button)
        self.bottom_layout.addWidget(row)

    def _build_task_sidebar(self) -> None:
        sidebar = QWidget()
        sidebar.setMinimumWidth(300)
        sidebar.setMaximumWidth(600)
        layout = QVBoxLayout(sidebar)
        layout.setContentsMargins(10, 8, 20, 12)
        title = QLabel("编码队列")
        title.setObjectName("sectionTitle")
        layout.addWidget(title)
        scroll = QScrollArea()
        scroll.setWidgetResizable(True)
        self.task_host = QWidget()
        self.task_host.setObjectName("contentHost")
        self.task_layout = QVBoxLayout(self.task_host)
        self.task_layout.setContentsMargins(0, 0, 0, 0)
        self.task_layout.setSpacing(6)
        self.task_layout.addStretch(1)
        scroll.setWidget(self.task_host)
        layout.addWidget(scroll, 1)
        self.task_sidebar = sidebar

    def toggle_maximized(self) -> None:
        self.set_maximized(not self.isMaximized())

    def set_maximized(self, maximized: bool) -> None:
        if maximized:
            self.showMaximized()
        else:
            self.showNormal()
        self._sync_maximize_button()

    def _sync_maximize_button(self) -> None:
        if not hasattr(self, "title_bar"):
            return
        if self.isMaximized():
            self.title_bar.maximize_button.setText("❐")
            self.title_bar.maximize_button.setToolTip("还原")
        else:
            self.title_bar.maximize_button.setText("□")
            self.title_bar.maximize_button.setToolTip("最大化")

    def resizeEvent(self, event) -> None:
        super().resizeEvent(event)
        self._apply_responsive_layout()
        self._layout_slot_cards()
        self._sync_maximize_button()
        if hasattr(self, "status_label"):
            self.status_label.setFixedHeight(self._status_bar_height())

    def _status_bar_height(self) -> int:
        """Scale from a compact 1080p bar to the existing comfortable 4K size."""
        screen = self.screen() or QApplication.primaryScreen()
        if screen is None:
            return 18
        physical_height = screen.availableGeometry().height() * screen.devicePixelRatio()
        return self._status_bar_height_for_physical_screen(physical_height)

    @staticmethod
    def _status_bar_height_for_physical_screen(physical_height: float) -> int:
        scale = max(0.0, min(1.0, (physical_height - 1080.0) / 1080.0))
        return round(18 + 9 * scale)

    def _task_sidebar_width(self) -> int:
        """Scale the queue from roughly 300 px at 1080p to 600 px at 4K."""

        screen = self.screen() or QApplication.primaryScreen()
        if screen is None:
            return 300
        physical_height = screen.availableGeometry().height() * screen.devicePixelRatio()
        return self._task_sidebar_width_for_physical_screen(physical_height)

    @staticmethod
    def _task_sidebar_width_for_physical_screen(physical_height: float) -> int:
        scale = max(0.0, min(1.0, (physical_height - 1080.0) / 1080.0))
        return round(300 + 300 * scale)

    def _apply_responsive_layout(self) -> None:
        if not hasattr(self, "task_sidebar"):
            return
        show_sidebar = self.width() >= 1380 and self.height() >= 760
        self.task_sidebar.setVisible(show_sidebar)
        sidebar_width = self._task_sidebar_width()
        self.task_sidebar.setMinimumWidth(sidebar_width)
        self.task_sidebar.setMaximumWidth(sidebar_width)
        left_margin = 36 if self.width() >= 980 else 22
        self.content_layout.setContentsMargins(left_margin, 8, 30, 12)
        self.bottom_layout.setContentsMargins(left_margin, 7, 30, 10)

    def dragEnterEvent(self, event) -> None:
        if event.mimeData().hasUrls() and any(
            url.isLocalFile() for url in event.mimeData().urls()
        ):
            event.acceptProposedAction()
            return
        event.ignore()

    def dragMoveEvent(self, event) -> None:
        if event.mimeData().hasUrls():
            event.acceptProposedAction()
            return
        event.ignore()

    def dropEvent(self, event) -> None:
        paths = [url.toLocalFile() for url in event.mimeData().urls() if url.isLocalFile()]
        if paths:
            self._handle_dropped_paths(paths)
            event.acceptProposedAction()
            return
        event.ignore()

    def _handle_dropped_paths(self, paths: Sequence[str]) -> None:
        accepted: list[str] = []
        hlm_files: list[Path] = []
        rejected = 0
        for raw_path in paths:
            path = Path(raw_path).expanduser()
            if path.is_file() and path.suffix.lower() == ".hlm":
                hlm_files.append(path)
            elif path.is_dir() or (path.is_file() and path.suffix.lower() in VIDEO_EXTENSIONS):
                accepted.append(str(path))
            else:
                rejected += 1
        if hlm_files:
            if len(hlm_files) > 1 or accepted:
                QMessageBox.warning(self, APP_TITLE, "一次只能导入一份 HLM，且不能与普通输入混用。")
            else:
                try:
                    self.import_hlm(hlm_files[0])
                except TranscodeError as exc:
                    QMessageBox.critical(self, APP_TITLE, str(exc))
            return
        if accepted:
            self._append_inputs(accepted)
        if rejected:
            self._set_status(
                f"已拖入 {len(accepted)} 个视频/目录；忽略 {rejected} 个非视频项目"
            )

    def add_files(self) -> None:
        paths, _ = QFileDialog.getOpenFileNames(
            self,
            "选择视频或 FlashCut HLM",
            "",
            "视频 / HLM (*.mp4 *.mkv *.mov *.avi *.webm *.ts *.m2ts *.wmv *.m4v *.mpeg *.mpg *.HLM);;所有文件 (*.*)",
        )
        if any(Path(path).suffix.lower() == ".hlm" for path in paths):
            self._handle_dropped_paths(paths)
        else:
            self._append_inputs(paths)

    def import_hlm(self, path: str | Path) -> None:
        manifest = Path(path).expanduser().resolve()
        document = load_hlm_document(manifest)
        source = hlm_source_path(document, manifest)
        defaults = hlm_processing_defaults(document, manifest)
        self.input_list.clear()
        for job in document["processing"]["jobs"]:
            job_id = str(job.get("id") or "")
            stem = str(job.get("output_stem") or job_id)
            start = float(job.get("start", 0) or 0)
            end = float(job.get("end", 0) or 0)
            self.input_list.addItem(f"{stem}  ·  原片 {source.name}  ·  {start:.3f}s-{end:.3f}s")
            self.input_list.item(self.input_list.count() - 1).setData(
                Qt.ItemDataRole.UserRole,
                job_id,
            )
        self.hlm_path = manifest
        self.codec_combo.setCurrentText("AV1" if defaults["codec"] == "av1" else "HEVC（默认）")
        self.container_combo.setCurrentText(str(defaults["container"]))
        output_dir = defaults["output_dir"]
        self.output_edit.setText(str(output_dir) if output_dir else "")
        self.overwrite_check.setChecked(bool(defaults["overwrite"]))
        self.subtitle_check.setChecked(bool(defaults["copy_subtitles"]))
        self._set_status(f"已从 {manifest.name} 载入 {self.input_list.count()} 个 HL 视频任务")
        self._log(f"[HLM] 已载入：{manifest}；原片：{source}")

    def add_directory(self) -> None:
        path = QFileDialog.getExistingDirectory(self, "选择视频目录")
        if path:
            self._append_inputs([path])

    def _append_inputs(self, paths: Sequence[str]) -> None:
        if paths and self.hlm_path is not None:
            self.input_list.clear()
            self.hlm_path = None
        existing = {self.input_list.item(i).text() for i in range(self.input_list.count())}
        for path in paths:
            normalized = str(Path(path).expanduser().resolve())
            if normalized not in existing:
                self.input_list.addItem(normalized)
                existing.add(normalized)
        self._set_status(f"已选择 {self.input_list.count()} 个输入项")

    def remove_selected(self) -> None:
        for item in self.input_list.selectedItems():
            self.input_list.takeItem(self.input_list.row(item))
        self._set_status(f"已选择 {self.input_list.count()} 个输入项")

    def clear_inputs(self) -> None:
        self.input_list.clear()
        self.hlm_path = None
        self._set_status("输入列表已清空")

    def choose_output(self) -> None:
        path = QFileDialog.getExistingDirectory(self, "选择输出目录")
        if path:
            self.output_edit.setText(str(Path(path).resolve()))

    def clear_log(self) -> None:
        self.log_edit.clear()
        self.log_lines.clear()

    def show_settings(self) -> None:
        if self.settings_window is not None and self.settings_window.isVisible():
            self.settings_window.raise_()
            self.settings_window.activateWindow()
            return
        dialog = SettingsDialog(self)
        self.settings_window = dialog
        dialog.destroyed.connect(lambda: setattr(self, "settings_window", None))
        dialog.show()
        dialog.raise_()
        dialog.activateWindow()

    def apply_settings_dialog(self, dialog: SettingsDialog) -> None:
        previous_debug = self.debug_progress_enabled
        self.debug_progress_enabled = dialog.debug_check.isChecked()
        self.logging_enabled = dialog.logging_check.isChecked()
        self.log_max_entries = dialog.max_entries_spin.value()
        self.log_max_size_mb = dialog.max_size_spin.value()
        self.app_logger.configure(
            self.logging_enabled,
            self.log_max_entries,
            self.log_max_size_mb,
        )
        if self.debug_progress_enabled != previous_debug:
            self._toggle_debug_slots(self.debug_progress_enabled)
        saved = self._save_preferences()
        if self.logging_enabled and self.app_logger.last_error:
            QMessageBox.warning(
                self,
                APP_TITLE,
                f"日志设置已保存，但无法打开主日志：\n{self.app_logger.last_error}",
            )
        self._log(
            f"[设置] 日志={'开启' if self.logging_enabled else '关闭'} · "
            f"最多 {self.log_max_entries} 个 · 单文件 {self.log_max_size_mb} MB"
        )
        self._set_status("个性化设置已保存" if saved else "设置已应用，但配置文件保存失败")

    def open_log_folder(self) -> None:
        try:
            APP_LOG_DIR.mkdir(parents=True, exist_ok=True)
        except OSError as exc:
            QMessageBox.critical(self, APP_TITLE, f"无法创建日志文件夹：\n{exc}")
            return
        if not QDesktopServices.openUrl(QUrl.fromLocalFile(str(APP_LOG_DIR))):
            QMessageBox.warning(self, APP_TITLE, f"无法打开日志文件夹：\n{APP_LOG_DIR}")

    def reset_defaults(self) -> None:
        if self.worker and self.worker.is_alive():
            QMessageBox.information(self, APP_TITLE, "任务运行中，不能重置参数。")
            return
        self.codec_combo.setCurrentIndex(0)
        self.cq_edit.setText("23")
        self.maxrate_edit.clear()
        self.bufsize_edit.clear()
        self.resolution_edit.clear()
        self.fps_edit.clear()
        self.preset_combo.setCurrentText("p7")
        self.multipass_combo.setCurrentText("fullres")
        self.lookahead_edit.setText("30")
        self.container_combo.setCurrentText("mp4")
        self.recursive_check.setChecked(False)
        self.overwrite_check.setChecked(False)
        self.subtitle_check.setChecked(True)
        self.affinity_check.setChecked(True)
        self.debug_progress_enabled = False
        self._toggle_debug_slots(False)
        self._save_preferences()
        self._set_status("已恢复默认参数")

    def _validate_and_build_args(self) -> list[str] | None:
        if self.hlm_path is not None:
            inputs = ["--hlm", str(self.hlm_path)]
            for index in range(self.input_list.count()):
                job_id = self.input_list.item(index).data(Qt.ItemDataRole.UserRole)
                if job_id:
                    inputs.extend(["--hlm-job", str(job_id)])
        else:
            inputs = [self.input_list.item(i).text() for i in range(self.input_list.count())]
        if not inputs or (self.hlm_path is not None and self.input_list.count() == 0):
            QMessageBox.warning(self, APP_TITLE, "请先添加至少一个视频、目录或 HLM 任务。")
            return None
        try:
            cq = float(self.cq_edit.text().strip())
            if not math.isfinite(cq) or not 0 <= cq <= 51:
                raise ValueError("CQ 必须在 0 到 51 之间")
            lookahead = int(self.lookahead_edit.text().strip())
            if not 0 <= lookahead <= 32:
                raise ValueError("Lookahead 必须在 0 到 32 之间")
            if self.maxrate_edit.text().strip():
                parse_bitrate(self.maxrate_edit.text().strip())
            if self.bufsize_edit.text().strip():
                if not self.maxrate_edit.text().strip():
                    raise ValueError("设置 VBV 缓冲前必须先设置最大码率")
                parse_bitrate(self.bufsize_edit.text().strip())
            if self.resolution_edit.text().strip():
                parse_resolution(self.resolution_edit.text().strip())
            if self.fps_edit.text().strip():
                parse_fps(self.fps_edit.text().strip())
        except (ValueError, argparse.ArgumentTypeError) as exc:
            QMessageBox.critical(self, APP_TITLE, f"参数错误：{exc}")
            return None

        args = inputs + [
            "--codec", "av1" if self.codec_combo.currentText() == "AV1" else "hevc",
            "--cq", f"{cq:g}",
            "--preset", self.preset_combo.currentText(),
            "--multipass", self.multipass_combo.currentText(),
            "--lookahead", str(lookahead),
            "--container", self.container_combo.currentText(),
        ]
        for option, value in (
            ("--maxrate", self.maxrate_edit.text().strip()),
            ("--bufsize", self.bufsize_edit.text().strip()),
            ("--resolution", self.resolution_edit.text().strip()),
            ("--fps", self.fps_edit.text().strip()),
            ("--output-dir", self.output_edit.text().strip()),
        ):
            if value:
                args.extend([option, value])
        if self.recursive_check.isChecked():
            args.append("--recursive")
        if self.overwrite_check.isChecked():
            args.append("--overwrite")
        if not self.subtitle_check.isChecked():
            args.append("--no-subtitles")
        if not self.affinity_check.isChecked():
            args.append("--no-cpu-affinity")
        if self.debug_progress_enabled:
            args.append("--debug-progress")
        return args

    def start(self) -> None:
        if self.worker and self.worker.is_alive():
            return
        args = self._validate_and_build_args()
        if args is None:
            return
        self._save_preferences()
        self.last_batch_args = list(args)
        self.pending_restarts.clear()
        self.restart_batch_active = False
        self.restart_batch_keys.clear()
        self._launch_backend(args, restart=False)

    def _launch_backend(self, args: Sequence[str], *, restart: bool) -> None:
        self.controller = ProcessController()
        self.start_button.setEnabled(False)
        self.stop_button.setEnabled(True)
        self.report_path = None
        self.batch_skipped_count = 0
        self.batch_cancelled_count = 0
        self.total_equivalent_frames = 0.0
        self.active_slot_files.clear()
        self.slot_states.clear()
        self._ensure_slot_cards(4 if self.debug_progress_enabled else 0)
        self._reset_overall_progress("正在探测视频…")
        self._set_status("正在探测视频并安排编码任务……")
        self._log("=" * 72)
        self._log("重新开始所选任务" if restart else "开始新的批处理")

        def run_backend() -> None:
            set_output_callback(self.bridge.log_message.emit)
            set_event_callback(self.bridge.backend_event.emit)
            try:
                code = main(list(args), controller=self.controller)
            except SystemExit as exc:
                code = int(exc.code or 0)
            except BaseException as exc:
                self.bridge.log_message.emit(
                    f"GUI 后台异常：{type(exc).__name__}: {exc}"
                )
                code = 2
            finally:
                set_output_callback(None)
                set_event_callback(None)
            self.bridge.finished.emit(code)

        self.worker = threading.Thread(target=run_backend, daemon=True, name="CodecFoundryBackend")
        self.worker.start()

    def _launch_pending_restarts(self) -> None:
        if self.closing or not self.pending_restarts:
            return
        if self.worker and self.worker.is_alive():
            QTimer.singleShot(50, self._launch_pending_restarts)
            return
        if not self.last_batch_args or "--codec" not in self.last_batch_args:
            self._set_status("无法恢复原批次参数，请重新开始整个批次")
            return
        records = list(self.pending_restarts.items())
        self.pending_restarts.clear()
        option_index = self.last_batch_args.index("--codec")
        option_args = list(self.last_batch_args[option_index:])
        if "--overwrite" not in option_args:
            option_args.append("--overwrite")
        external_ids = [
            str(record.get("external_id") or "") for _, record in records
        ]
        if all(external_ids) and "--hlm" in self.last_batch_args:
            hlm_index = self.last_batch_args.index("--hlm")
            input_args = ["--hlm", self.last_batch_args[hlm_index + 1]]
            for external_id in external_ids:
                input_args.extend(["--hlm-job", external_id])
        else:
            input_args = list(dict.fromkeys(
                str(record.get("source") or "") for _, record in records
            ))
        self.restart_batch_keys = {key for key, _ in records}
        self.restart_batch_active = True
        self._launch_backend(input_args + option_args, restart=True)

    def stop_all(self) -> None:
        if not self.controller or not self.worker or not self.worker.is_alive():
            return
        for task_key in tuple(self.pending_restarts):
            record = self.task_records.get(task_key, {})
            self._update_task_card(
                task_key,
                "cancelled",
                int(record.get("slot_id") or 0),
            )
        self.pending_restarts.clear()
        self.stop_button.setEnabled(False)
        self._set_status("正在停止全部 FFmpeg 进程，请稍候……")
        self._log("[取消] 用户请求停止全部任务")
        threading.Thread(target=self.controller.cancel, daemon=True).start()

    def _log(self, message: str) -> None:
        text = str(message).rstrip()
        self.log_lines.extend(text.splitlines() or [""])
        self.app_logger.write(text)
        self.log_edit.appendPlainText(text)
        scrollbar = self.log_edit.verticalScrollBar()
        scrollbar.setValue(scrollbar.maximum())

    def _set_status(self, text: str) -> None:
        self.status_label.setText(f"  {text}")

    def _reset_overall_progress(self, suffix: str = "等待任务") -> None:
        self.overall_progress.setValue(0)
        self.overall_detail.setText(f"总进度 0.0% · {suffix}")

    @staticmethod
    def _new_slot_state() -> dict[str, object]:
        return {
            "task_index": -1,
            "queue_total": 0,
            "queue_equivalent": 0.0,
            "completed_equivalent": 0.0,
            "current_equivalent": 0.0,
            "progress": 0.0,
            "equivalent_fps": 0.0,
            "rate_samples": deque(),
            "active": False,
            "cancelled": False,
            "filename": "尚未分配视频",
        }

    def _toggle_debug_slots(self, checked: bool) -> None:
        if self.worker and self.worker.is_alive():
            if checked:
                self._ensure_slot_cards(max(4, len(self.slot_widgets)))
            return
        self.slot_states.clear()
        self._ensure_slot_cards(4 if checked else 0)
        self._reset_overall_progress()

    def _ensure_slot_cards(self, count: int) -> None:
        count = max(0, count)
        for slot_id in sorted(tuple(self.slot_widgets), reverse=True):
            if slot_id >= count:
                self.slot_widgets[slot_id]["frame"].deleteLater()
                del self.slot_widgets[slot_id]
                self.slot_states.pop(slot_id, None)
        for slot_id in range(count):
            if slot_id in self.slot_widgets:
                continue
            frame = QFrame()
            frame.setObjectName("panel")
            layout = QVBoxLayout(frame)
            layout.setContentsMargins(11, 8, 11, 8)
            layout.setSpacing(4)
            title = QLabel(f"E{slot_id} · 等待任务")
            title.setObjectName("cardTitle")
            filename = QLabel("尚未分配视频")
            filename.setObjectName("hint")
            filename.setTextInteractionFlags(Qt.TextInteractionFlag.TextSelectableByMouse)
            current = QProgressBar()
            current.setObjectName("orangeProgress")
            current.setRange(0, 1000)
            current.setTextVisible(False)
            current_text = QLabel("当前 0.0% · fps -- · ETA --")
            current_text.setObjectName("hint")
            queued = QProgressBar()
            queued.setObjectName("blueProgress")
            queued.setRange(0, 1000)
            queued.setTextVisible(False)
            queue_text = QLabel("队列 0/0 · 0.0% · ETA --")
            queue_text.setObjectName("hint")
            for widget in (title, filename, current, current_text, queued, queue_text):
                layout.addWidget(widget)
            self.slot_widgets[slot_id] = {
                "frame": frame,
                "title": title,
                "filename": filename,
                "current": current,
                "current_text": current_text,
                "queue": queued,
                "queue_text": queue_text,
            }
            self.slot_states.setdefault(slot_id, self._new_slot_state())
        self._layout_slot_cards()

    def _layout_slot_cards(self) -> None:
        if not hasattr(self, "slot_layout"):
            return
        while self.slot_layout.count():
            self.slot_layout.takeAt(0)
        available = self.slot_scroll.viewport().width() if hasattr(self, "slot_scroll") else 0
        columns = 2 if available >= 760 else 1
        for slot_id, widgets in sorted(self.slot_widgets.items()):
            self.slot_layout.addWidget(
                widgets["frame"], slot_id // columns, slot_id % columns
            )
        for column in range(columns):
            self.slot_layout.setColumnStretch(column, 1)
        self.slot_layout.setRowStretch(max(1, (len(self.slot_widgets) + columns - 1) // columns), 1)

    def _clear_task_cards(self) -> None:
        for widgets in self.task_widgets.values():
            widgets["frame"].deleteLater()
        self.task_widgets.clear()
        self.task_records.clear()

    def _populate_task_sidebar(
        self,
        slots: list[dict[str, object]],
        skipped_tasks: list[dict[str, object]],
    ) -> None:
        self._clear_task_cards()
        ordered: list[tuple[int, dict[str, object]]] = []
        max_queue = max((len(slot.get("tasks", [])) for slot in slots), default=0)
        for queue_index in range(max_queue):
            for slot in sorted(slots, key=lambda item: int(item.get("slot_id") or 0)):
                tasks = list(slot.get("tasks", []))
                if queue_index < len(tasks):
                    ordered.append((int(slot.get("slot_id") or 0), dict(tasks[queue_index])))
        for slot_id, task in ordered:
            self._create_task_card(slot_id, task, "waiting")
        for task in skipped_tasks:
            self._create_task_card(-1, dict(task), "skipped")

    def _create_task_card(self, slot_id: int, record: dict[str, object], status: str) -> None:
        source = str(record.get("source") or "")
        task_key = str(record.get("output") or source)
        record["slot_id"] = slot_id
        record["status"] = status
        frame = QFrame()
        frame.setObjectName("panel")
        layout = QVBoxLayout(frame)
        layout.setContentsMargins(9, 7, 9, 7)
        layout.setSpacing(2)
        top = QWidget()
        top_layout = QHBoxLayout(top)
        top_layout.setContentsMargins(0, 0, 0, 0)
        filename = QLabel(str(record.get("filename") or Path(source).name))
        filename.setObjectName("cardTitle")
        filename.setWordWrap(True)
        status_label = QLabel()
        engine = QLabel(f"E{slot_id}" if slot_id >= 0 else "—")
        engine.setObjectName("hint")
        top_layout.addWidget(filename, 1)
        top_layout.addWidget(status_label)
        top_layout.addWidget(engine)
        path_label = QLabel(source)
        path_label.setObjectName("hint")
        path_label.setWordWrap(True)
        source_fps = record.get("source_fps")
        fps_text = f"{float(source_fps):g}" if source_fps else "未知"
        meta = QLabel(
            f"{str(record.get('source_container') or '未知').upper()} · "
            f"{record.get('source_width', '?')}x{record.get('source_height', '?')} · "
            f"{fps_text} fps · {str(record.get('input_codec') or '未知').upper()} · "
            f"{format_file_size(int(record.get('input_size') or 0))}"
        )
        meta.setObjectName("hint")
        meta.setWordWrap(True)
        after = QLabel("")
        after.setWordWrap(True)
        after.hide()
        action_row = QWidget()
        action_layout = QHBoxLayout(action_row)
        action_layout.setContentsMargins(0, 3, 0, 0)
        action_layout.addStretch(1)
        action = QPushButton()
        action.clicked.connect(
            lambda _checked=False, key=task_key: self._handle_task_action(key)
        )
        action_layout.addWidget(action)
        layout.addWidget(top)
        layout.addWidget(path_label)
        layout.addWidget(meta)
        layout.addWidget(after)
        layout.addWidget(action_row)
        self.task_layout.insertWidget(max(0, self.task_layout.count() - 1), frame)
        self.task_widgets[task_key] = {
            "frame": frame,
            "filename": filename,
            "status": status_label,
            "engine": engine,
            "after": after,
            "action": action,
        }
        self.task_records[task_key] = record
        self._update_task_card(task_key, status, slot_id)

    def _resolve_task_key(
        self,
        identifier: str,
        payload: dict[str, object] | None = None,
    ) -> str:
        output = str((payload or {}).get("output") or "")
        if output in self.task_records:
            return output
        if identifier in self.task_records:
            return identifier
        matches = [
            key
            for key, record in self.task_records.items()
            if str(record.get("source") or "") == identifier
        ]
        return matches[0] if len(matches) == 1 else identifier

    def _update_task_card(
        self,
        identifier: str,
        status: str,
        slot_id: int,
        payload: dict[str, object] | None = None,
    ) -> None:
        task_key = self._resolve_task_key(identifier, payload)
        widgets = self.task_widgets.get(task_key)
        record = self.task_records.get(task_key)
        if not widgets or record is None:
            return
        labels = {
            "waiting": ("等待中", COLOR_WAITING),
            "running": ("进行中", COLOR_RUNNING),
            "verifying": ("GPU 校验中", COLOR_ORANGE),
            "success": ("已完成", COLOR_DONE),
            "failed": ("失败", COLOR_FAILED),
            "cancelled": ("已停止", COLOR_FAILED),
            "stopping": ("停止中", COLOR_FAILED),
            "skipped": ("已验证 / 跳过", COLOR_WAITING),
        }
        text, color = labels.get(status, (status, COLOR_MUTED))
        widgets["status"].setText(text)
        widgets["status"].setStyleSheet(f"color: {color}; font-weight: 600;")
        widgets["engine"].setText(f"E{slot_id}" if slot_id >= 0 else "—")
        record["status"] = status
        record["slot_id"] = slot_id
        action = widgets["action"]
        action_states = {
            "waiting": ("取消", "taskCancelButton", True),
            "running": ("停止", "taskStopButton", True),
            "verifying": ("停止", "taskStopButton", True),
            "stopping": ("停止中", "taskStopButton", False),
            "cancelled": ("重新开始", "taskRestartButton", True),
            "failed": ("重新开始", "taskRestartButton", True),
            "success": ("打开", "taskOpenButton", True),
            "skipped": ("打开", "taskOpenButton", True),
        }
        action_text, object_name, enabled = action_states.get(
            status, ("重新开始", "taskRestartButton", True)
        )
        action.setText(action_text)
        action.setObjectName(object_name)
        action.setEnabled(enabled)
        action.style().unpolish(action)
        action.style().polish(action)
        if status != "success":
            widgets["after"].hide()
        if status == "success" and payload:
            input_size = int(payload.get("input_size") or record.get("input_size") or 0)
            output_size = int(payload.get("output_size") or 0)
            change = output_size - input_size
            ratio = (input_size - output_size) / input_size * 100 if input_size else 0.0
            if payload.get("kept_source"):
                after_text = f"编码结果更大，保留原文件 · {format_file_size(output_size)} · GPU 校验通过"
            else:
                arrow = "↓" if change <= 0 else "↑"
                after_text = (
                    f"压制后 {format_file_size(output_size)} · {arrow} "
                    f"{format_file_size(abs(change))} · 压缩率 {ratio:.2f}% · GPU 校验通过"
                )
            widgets["after"].setText(after_text)
            widgets["after"].setStyleSheet(f"color: {COLOR_DONE if change <= 0 else COLOR_FAILED};")
            widgets["after"].show()

    def _handle_task_action(self, task_key: str) -> None:
        record = self.task_records.get(task_key)
        if record is None:
            return
        status = str(record.get("status") or "")
        slot_id = int(record.get("slot_id") or 0)
        if status == "waiting":
            if task_key in self.pending_restarts:
                self.pending_restarts.pop(task_key, None)
                self._update_task_card(task_key, "cancelled", slot_id)
            elif self.controller is not None and self.worker and self.worker.is_alive():
                self._update_task_card(task_key, "cancelled", slot_id)
                threading.Thread(
                    target=self.controller.cancel_task,
                    args=(task_key,),
                    daemon=True,
                ).start()
            else:
                self._update_task_card(task_key, "cancelled", slot_id)
            self._log(f"[取消任务] {record.get('filename', task_key)}")
            return
        if status in {"running", "verifying"}:
            if self.controller is None:
                return
            self._update_task_card(task_key, "stopping", slot_id)
            self._set_status(f"正在停止任务：{record.get('filename', task_key)}")
            self._log(f"[停止任务] {record.get('filename', task_key)}")
            threading.Thread(
                target=self.controller.cancel_task,
                args=(task_key,),
                daemon=True,
            ).start()
            return
        if status in {"cancelled", "failed"}:
            self.pending_restarts[task_key] = dict(record)
            self._update_task_card(task_key, "waiting", slot_id)
            self._set_status(f"已加入重新开始队列：{record.get('filename', task_key)}")
            self._log(f"[重新开始] {record.get('filename', task_key)}")
            if not self.worker or not self.worker.is_alive():
                QTimer.singleShot(0, self._launch_pending_restarts)
            return
        if status in {"success", "skipped"}:
            self._open_task_output(record)

    def _open_task_output(self, record: dict[str, object]) -> None:
        output = Path(str(record.get("output") or ""))
        if not output.is_file():
            QMessageBox.warning(self, APP_TITLE, f"输出文件不存在：\n{output}")
            return
        if not QDesktopServices.openUrl(QUrl.fromLocalFile(str(output))):
            QMessageBox.warning(self, APP_TITLE, f"无法打开输出文件：\n{output}")

    @staticmethod
    def _format_duration(seconds: object) -> str:
        try:
            value = max(0, round(float(seconds)))
        except (TypeError, ValueError):
            return "--"
        hours, remainder = divmod(value, 3600)
        minutes, secs = divmod(remainder, 60)
        return f"{hours:02d}:{minutes:02d}:{secs:02d}"

    def _handle_backend_event(self, event_type: str, raw_payload: object) -> None:
        payload = dict(raw_payload) if isinstance(raw_payload, dict) else {}
        if event_type == "probe_progress":
            progress = max(0.0, min(1.0, float(payload.get("progress") or 0)))
            current = int(payload.get("current") or 0)
            total = int(payload.get("total") or 0)
            stage = str(payload.get("stage") or "正在探测")
            filename = str(payload.get("filename") or "未知文件")
            self.overall_progress.setValue(round(progress * 1000))
            self.overall_detail.setText(
                f"探测进度 {progress * 100:.1f}% · 文件 {current}/{total} · {stage}"
            )
            self._set_status(f"{stage} · {filename}")
            return
        if event_type == "status":
            self._set_status(str(payload.get("text") or ""))
            return
        if event_type == "report":
            self.report_path = str(payload.get("path") or "")
            self._set_status(f"压缩报告已保存：{self.report_path}")
            return
        if event_type == "batch_done":
            self.batch_cancelled_count = int(payload.get("cancelled") or 0)
            return
        if event_type == "plan":
            slots = list(payload.get("slots", []))
            self.batch_skipped_count = int(payload.get("skipped_count") or 0)
            self.total_equivalent_frames = float(payload.get("total_equivalent_frames") or 0)
            display_slots = int(payload.get("display_slots") or 0)
            self.slot_states.clear()
            self._ensure_slot_cards(display_slots)
            skipped_tasks = list(payload.get("skipped_tasks", []))
            if self.restart_batch_active:
                for slot_plan in slots:
                    slot_id = int(slot_plan.get("slot_id") or 0)
                    for raw_task in list(slot_plan.get("tasks", [])):
                        task = dict(raw_task)
                        task_key = str(task.get("output") or task.get("source") or "")
                        if task_key in self.task_records:
                            self.task_records[task_key].update(task)
                            self.task_records[task_key]["slot_id"] = slot_id
                            self._update_task_card(task_key, "waiting", slot_id)
                        else:
                            self._create_task_card(slot_id, task, "waiting")
                for raw_task in skipped_tasks:
                    task = dict(raw_task)
                    task_key = str(task.get("output") or task.get("source") or "")
                    if task_key in self.task_records:
                        self.task_records[task_key].update(task)
                        self._update_task_card(task_key, "skipped", -1)
                    else:
                        self._create_task_card(-1, task, "skipped")
            else:
                self._populate_task_sidebar(slots, skipped_tasks)
            for slot_id, widgets in self.slot_widgets.items():
                self.slot_states[slot_id] = self._new_slot_state()
                widgets["title"].setText(f"E{slot_id} · 等待任务")
                widgets["filename"].setText("尚未分配视频")
                widgets["current"].setValue(0)
                widgets["queue"].setValue(0)
            for slot_plan in slots:
                slot_id = int(slot_plan.get("slot_id") or 0)
                state = self.slot_states.setdefault(slot_id, self._new_slot_state())
                state["queue_total"] = len(slot_plan.get("tasks", []))
                state["queue_equivalent"] = float(slot_plan.get("queue_equivalent_frames") or 0)
                widgets = self.slot_widgets.get(slot_id)
                if widgets:
                    widgets["title"].setText(
                        f"E{slot_id} · GPU {slot_plan.get('gpu_index', '?')} / "
                        f"NVENC {int(slot_plan.get('engine') or 0) + 1}"
                    )
                    widgets["queue_text"].setText(
                        f"队列 0/{state['queue_total']} · 0.0% · ETA --"
                    )
            self._reset_overall_progress(
                f"已安排 {int(payload.get('task_count') or 0)} 个视频 · "
                f"跳过 {self.batch_skipped_count} 个"
            )
            return
        if "slot_id" not in payload:
            return
        slot_id = int(payload["slot_id"])
        if slot_id not in self.slot_widgets:
            self._ensure_slot_cards(slot_id + 1)
        state = self.slot_states.setdefault(slot_id, self._new_slot_state())
        widgets = self.slot_widgets[slot_id]

        if event_type == "task_start":
            state.update(
                {
                    "task_index": int(payload.get("task_index") or 0),
                    "queue_total": int(payload.get("queue_total") or 0),
                    "queue_equivalent": float(payload.get("queue_equivalent_frames") or 0),
                    "completed_equivalent": float(payload.get("completed_equivalent_frames") or 0),
                    "current_equivalent": float(payload.get("equivalent_frames") or 0),
                    "progress": 0.0,
                    "active": True,
                    "cancelled": False,
                    "filename": str(payload.get("filename") or "未知文件"),
                }
            )
            self._record_queue_sample(state)
            self.active_slot_files[slot_id] = str(state["filename"])
            task_key = str(payload.get("output") or payload.get("source") or "")
            self._update_task_card(task_key, "running", slot_id, payload)
            widgets["filename"].setText(str(state["filename"]))
            widgets["current"].setValue(0)
            widgets["current_text"].setText("当前 0.0% · fps -- · ETA --")
            self._update_slot_queue(slot_id)
            self._update_active_status()
            self._update_overall_progress()
            return
        if event_type == "task_progress":
            progress = max(0.0, min(1.0, float(payload.get("progress") or 0)))
            state["progress"] = progress
            self._record_queue_sample(state, max(0.0, float(payload.get("equivalent_fps") or 0)))
            encoding_fps = float(payload.get("encoding_fps") or 0)
            widgets["current"].setValue(round(progress * 1000))
            widgets["current_text"].setText(
                f"当前 {progress * 100:.1f}% · fps {encoding_fps:.1f} · "
                f"ETA {self._format_duration(payload.get('eta_seconds'))}"
            )
            self._update_slot_queue(slot_id)
            self._update_overall_progress()
            return
        if event_type == "task_verification_start":
            widgets["current"].setValue(1000)
            widgets["current_text"].setText("编码完成 · GPU / NVDEC 全量解码校验中…")
            task_key = str(payload.get("output") or payload.get("source") or "")
            self._update_task_card(task_key, "verifying", slot_id, payload)
            self._set_status(f"GPU 校验最终输出 · {payload.get('filename', '')}")
            return
        if event_type == "task_done":
            status = str(payload.get("status") or "failed")
            task_key = str(payload.get("output") or payload.get("source") or "")
            self._update_task_card(task_key, status, slot_id, payload)
            state["active"] = False
            state["cancelled"] = status == "cancelled"
            self.active_slot_files.pop(slot_id, None)
            if status in {"success", "failed"}:
                state["progress"] = 1.0
                state["completed_equivalent"] = float(
                    payload.get("completed_equivalent_frames")
                    or float(state.get("completed_equivalent") or 0)
                    + float(state.get("current_equivalent") or 0)
                )
                widgets["current"].setValue(1000)
            label = {"success": "已完成并通过校验", "failed": "失败", "cancelled": "已停止"}.get(status, status)
            widgets["current_text"].setText(f"当前任务：{label}")
            widgets["filename"].setText(f"{state['filename']} · {label}")
            state["current_equivalent"] = 0.0
            state["progress"] = 0.0
            self._update_slot_queue(slot_id)
            self._update_active_status()
            self._update_overall_progress()

    @staticmethod
    def _queue_processed(state: dict[str, object]) -> float:
        total = float(state.get("queue_equivalent") or 0)
        completed = float(state.get("completed_equivalent") or 0)
        current = float(state.get("current_equivalent") or 0)
        progress = float(state.get("progress") or 0)
        return min(total, completed + current * progress) if total else 0.0

    def _record_queue_sample(self, state: dict[str, object], fallback_rate: float = 0.0) -> None:
        samples = state.get("rate_samples")
        if not isinstance(samples, deque):
            samples = deque()
            state["rate_samples"] = samples
        state["equivalent_fps"] = update_recent_work_rate(
            samples,
            time.monotonic(),
            self._queue_processed(state),
            fallback_rate,
            window_seconds=10.0,
        )

    def _update_slot_queue(self, slot_id: int) -> None:
        state = self.slot_states[slot_id]
        widgets = self.slot_widgets[slot_id]
        total = float(state.get("queue_equivalent") or 0)
        processed = self._queue_processed(state)
        percentage = processed / total if total else 0.0
        rate = float(state.get("equivalent_fps") or 0)
        remaining = max(0.0, total - processed)
        eta = self._format_duration(remaining / rate) if remaining > 0 and rate > 0 else ("00:00:00" if remaining <= 0 else "--")
        widgets["queue"].setValue(round(percentage * 1000))
        task_index = int(state.get("task_index", -1))
        task_number = min(int(state.get("queue_total") or 0), task_index + 1)
        widgets["queue_text"].setText(
            f"队列 {max(0, task_number)}/{int(state.get('queue_total') or 0)} · "
            f"{percentage * 100:.1f}% · ETA {eta}"
        )

    def _update_overall_progress(self) -> None:
        processed = 0.0
        combined_rate = 0.0
        queue_metrics: list[tuple[float, float]] = []
        for state in self.slot_states.values():
            queue_processed = self._queue_processed(state)
            processed += queue_processed
            remaining = max(0.0, float(state.get("queue_equivalent") or 0) - queue_processed)
            rate = float(state.get("equivalent_fps") or 0)
            if remaining > 0:
                if rate > 0 and not state.get("cancelled"):
                    combined_rate += rate
                queue_metrics.append((remaining, rate if not state.get("cancelled") else 0.0))
        percentage = min(1.0, processed / self.total_equivalent_frames) if self.total_equivalent_frames else 0.0
        eta_value = parallel_queue_eta(queue_metrics)
        eta = self._format_duration(eta_value) if eta_value is not None else "--"
        self.overall_progress.setValue(round(percentage * 1000))
        self.overall_detail.setText(
            f"总进度 {percentage * 100:.1f}% · 10秒等效 {combined_rate:.1f} fps · ETA {eta}"
        )

    def _update_active_status(self) -> None:
        if self.active_slot_files:
            active = " | ".join(
                f"E{key} {name}" for key, name in sorted(self.active_slot_files.items())
            )
            self._set_status(f"正在编码 · {active}")
        else:
            self._set_status("当前编码已结束，正在汇总结果……")

    def _finish(self, code: int) -> None:
        finished_restart_batch = self.restart_batch_active
        self.restart_batch_active = False
        self.restart_batch_keys.clear()
        has_pending_restarts = bool(self.pending_restarts) and not self.closing
        self.start_button.setEnabled(not self.closing and not has_pending_restarts)
        self.stop_button.setEnabled(False)
        if code == 0 and self.batch_cancelled_count:
            self._set_status(f"批处理结束 · 已停止 {self.batch_cancelled_count} 个任务")
        elif code == 0:
            if self.total_equivalent_frames > 0:
                self.overall_progress.setValue(1000)
                self.overall_detail.setText("总进度 100.0% · 全部输出均已通过 GPU 校验")
            else:
                self._reset_overall_progress("无需编码 · 已有输出均验证有效")
            suffix = f" · 报告：{self.report_path}" if self.report_path else ""
            self._set_status(f"全部任务已完成并验证{suffix}")
        elif code == 130:
            self._set_status("全部任务已取消")
        else:
            self._set_status(f"批处理结束，退出码 {code}；请查看运行信息")
        if self.closing:
            self._finish_close_when_ready()
        elif has_pending_restarts:
            QTimer.singleShot(0, self._launch_pending_restarts)
        elif finished_restart_batch:
            self.start_button.setEnabled(True)

    def show_help(self) -> None:
        dialog = QDialog(self)
        dialog.setWindowTitle("CodecFoundry 详细帮助")
        dialog.resize(860, 680)
        layout = QVBoxLayout(dialog)
        text = QPlainTextEdit()
        text.setReadOnly(True)
        text.setPlainText(build_parser().format_help())
        text.setFont(QFont("Microsoft YaHei UI", 10))
        close_button = QPushButton("关闭")
        close_button.setObjectName("primaryButton")
        close_button.clicked.connect(dialog.accept)
        layout.addWidget(text, 1)
        layout.addWidget(close_button, 0, Qt.AlignmentFlag.AlignRight)
        dialog.exec()

    def show_about(self) -> None:
        dialog = QDialog(self)
        dialog.setWindowTitle(f"关于 {APP_NAME}")
        if not self.windowIcon().isNull():
            dialog.setWindowIcon(self.windowIcon())
        dialog.setWindowFlag(Qt.WindowType.Tool, True)
        dialog.setWindowFlag(Qt.WindowType.WindowStaysOnTopHint, True)
        dialog.setModal(True)
        dialog.setFixedSize(510, 520)
        layout = QVBoxLayout(dialog)
        layout.setContentsMargins(28, 18, 28, 18)
        layout.setSpacing(8)

        title = QLabel(APP_NAME)
        title.setObjectName("brand")
        title.setAlignment(Qt.AlignmentFlag.AlignCenter)
        subtitle = QLabel("NVIDIA VIDEO TRANSCODING WORKBENCH")
        subtitle.setObjectName("hint")
        subtitle.setAlignment(Qt.AlignmentFlag.AlignCenter)
        divider = QFrame()
        divider.setObjectName("headerDivider")
        divider.setFixedHeight(2)
        layout.addWidget(title)
        layout.addWidget(subtitle)
        layout.addWidget(divider)

        identity = QHBoxLayout()
        author_caption = QLabel("作者：")
        author_link = QLabel(
            f'<a href="{AUTHOR_URL}" style="color:#9aa7ff; '
            f'text-decoration:underline;">{AUTHOR_NAME}</a>'
        )
        author_link.setOpenExternalLinks(False)
        author_link.setCursor(Qt.CursorShape.PointingHandCursor)
        author_link.linkActivated.connect(
            lambda url: QDesktopServices.openUrl(QUrl(url))
        )
        version = QLabel(f"版本：{APP_VERSION}")
        author_caption.setStyleSheet("color: #bfbfbf;")
        version.setStyleSheet("color: #bfbfbf;")
        identity.addWidget(author_caption)
        identity.addWidget(author_link)
        identity.addStretch(1)
        identity.addWidget(version)
        layout.addLayout(identity)

        changes_title = QLabel("更新内容")
        changes_title.setObjectName("sectionTitle")
        changes = QLabel(
            f"v{APP_VERSION}（当前版本）\n"
            "  1. 使用 PySide6 重构为前后端单文件应用\n"
            "  2. 支持 NVIDIA HEVC / AV1 与多编码核心调度\n"
            "  3. 已有输出和新编码输出均执行 GPU / NVDEC 全量校验\n"
            "  4. 提供 CQ-VBR、实时进度、任务队列和安全退出保护\n"
            "  5. 支持直接拖入视频文件或文件夹"
        )
        changes.setWordWrap(True)
        changes.setAlignment(Qt.AlignmentFlag.AlignTop | Qt.AlignmentFlag.AlignLeft)
        changes.setStyleSheet("color: #bfbfbf; line-height: 1.5;")
        layout.addWidget(changes_title)
        layout.addWidget(changes, 1)

        banner_cn = QLabel("技术宅照亮世界")
        banner_cn.setAlignment(Qt.AlignmentFlag.AlignCenter)
        banner_cn.setStyleSheet(
            "background: #353535; color: #bdbdbd; font-size: 14pt; "
            "font-style: italic; font-weight: 700; padding: 7px;"
        )
        banner_en = QLabel("TECH OTAKUS ILLUMINATE THE WORLD")
        banner_en.setAlignment(Qt.AlignmentFlag.AlignCenter)
        banner_en.setStyleSheet(
            "background: #3e3e3e; color: #ababab; font-size: 10pt; "
            "font-style: italic; font-weight: 700; padding: 5px;"
        )
        layout.addWidget(banner_cn)
        layout.addWidget(banner_en)

        github_button = QPushButton("探索更多免费好用的工具")
        github_button.setObjectName("primaryButton")
        github_button.setCursor(Qt.CursorShape.PointingHandCursor)
        github_button.setToolTip(AUTHOR_GITHUB_URL)
        github_button.clicked.connect(
            lambda _checked=False: QDesktopServices.openUrl(QUrl(AUTHOR_GITHUB_URL))
        )
        layout.addWidget(github_button)

        footer = QHBoxLayout()
        warning = QLabel("未经许可 严禁商用 禁止用于非法用途！")
        warning.setObjectName("hint")
        close_button = QPushButton("返回")
        close_button.setObjectName("primaryButton")
        close_button.setMinimumWidth(170)
        close_button.clicked.connect(dialog.accept)
        footer.addWidget(warning)
        footer.addStretch(1)
        footer.addWidget(close_button)
        layout.addLayout(footer)
        dialog.exec()

    def _save_session_log(self) -> Path | None:
        self._save_preferences()
        if not self.logging_enabled:
            return None
        self.app_logger.write(f"[退出] {APP_TITLE} 已完成资源释放")
        return APP_LOG_PATH if APP_LOG_PATH.exists() else None

    def _set_safe_exit_topmost(self, enabled: bool) -> None:
        """Temporarily keep the main window above unrelated applications."""
        window_state = self.windowState()
        was_visible = self.isVisible()
        self.setWindowFlag(Qt.WindowType.WindowStaysOnTopHint, enabled)
        self.setWindowState(window_state)
        if enabled or was_visible:
            self.show()
            self.raise_()
            self.activateWindow()

    def closeEvent(self, event) -> None:
        if self.close_finalized:
            event.accept()
            return
        if self.closing:
            event.ignore()
            return
        running = bool(self.worker and self.worker.is_alive())
        was_topmost = bool(
            self.windowFlags() & Qt.WindowType.WindowStaysOnTopHint
        )
        self._set_safe_exit_topmost(True)
        box = QMessageBox(self)
        box.setWindowTitle("退出保护")
        if not self.windowIcon().isNull():
            box.setWindowIcon(self.windowIcon())
        box.setWindowFlag(Qt.WindowType.WindowStaysOnTopHint, True)
        box.setIcon(QMessageBox.Icon.Warning if running else QMessageBox.Icon.Question)
        if running:
            box.setText("编码或校验任务仍在运行。")
            box.setInformativeText("退出会先停止全部 FFmpeg 进程、等待后台释放资源并保存日志。")
            confirm = box.addButton("停止全部并退出", QMessageBox.ButtonRole.AcceptRole)
        else:
            box.setText("确定退出 CodecFoundry 吗？")
            box.setInformativeText("退出前会保存本次运行日志。")
            confirm = box.addButton("安全退出", QMessageBox.ButtonRole.AcceptRole)
        cancel = box.addButton("取消", QMessageBox.ButtonRole.RejectRole)
        box.setDefaultButton(cancel)
        box.show()
        box.raise_()
        box.activateWindow()
        box.exec()
        if box.clickedButton() is not confirm:
            if not was_topmost:
                self._set_safe_exit_topmost(False)
            event.ignore()
            return
        event.ignore()
        self._begin_safe_exit(running)

    def _begin_safe_exit(self, running: bool) -> None:
        """Hide first, then perform process cleanup and log persistence."""
        self.closing = True
        self.start_button.setEnabled(False)
        if self.settings_window is not None:
            self.settings_window.close()
        # Make the UI disappear immediately; cleanup continues safely in the
        # existing event loop until every FFmpeg process and worker has stopped.
        self.hide()
        if running:
            self._set_status("正在停止任务、释放资源并保存日志……")
            self.stop_all()
            self.close_timer.start()
        else:
            self._finalize_close()

    def _finish_close_when_ready(self) -> None:
        if self.worker and self.worker.is_alive():
            return
        self.close_timer.stop()
        self._finalize_close()

    def _finalize_close(self) -> None:
        if self.close_finalized:
            return
        set_output_callback(None)
        set_event_callback(None)
        self.log_saved_path = self._save_session_log()
        self.app_logger.close()
        self.close_finalized = True
        self.close()
        # The main window is deliberately hidden before cleanup.  Qt does not
        # reliably emit lastWindowClosed when an already-hidden window closes,
        # so explicitly stop the event loop after every resource is released.
        application = QApplication.instance()
        if application is not None:
            application.quit()


def gui_main(argv: Sequence[str] | None = None) -> int:
    gui_arguments = list(argv) if argv is not None else sys.argv[1:]
    gui_parser = argparse.ArgumentParser(add_help=False)
    gui_parser.add_argument("--hlm", type=Path)
    gui_options, unknown = gui_parser.parse_known_args(gui_arguments)
    if unknown:
        raise TranscodeError(f"无法识别 GUI 启动参数：{' '.join(unknown)}")
    detach_windows_console_for_gui()
    if os.name == "nt":
        try:
            ctypes.windll.shell32.SetCurrentProcessExplicitAppUserModelID(
                f"O-TREE.CodecFoundry.{APP_VERSION}"
            )
        except (AttributeError, OSError):
            pass
    app = QApplication(sys.argv)
    app.setApplicationName(APP_NAME)
    app.setApplicationVersion(APP_VERSION)
    source_icon = load_application_icon()
    if source_icon is not None:
        app.setWindowIcon(source_icon)
    app.setStyle("Fusion")
    palette = QPalette()
    palette.setColor(QPalette.ColorRole.Window, QColor(COLOR_BG))
    palette.setColor(QPalette.ColorRole.WindowText, QColor(COLOR_TEXT))
    palette.setColor(QPalette.ColorRole.Base, QColor(COLOR_ENTRY))
    palette.setColor(QPalette.ColorRole.Text, QColor(COLOR_TEXT))
    palette.setColor(QPalette.ColorRole.Button, QColor(COLOR_PANEL))
    palette.setColor(QPalette.ColorRole.ButtonText, QColor(COLOR_TEXT))
    palette.setColor(QPalette.ColorRole.Highlight, QColor(COLOR_BLUE))
    app.setPalette(palette)
    app.setStyleSheet(APP_STYLESHEET)
    window = CodecFoundryWindow()
    window.show()
    if gui_options.hlm:
        try:
            window.import_hlm(gui_options.hlm)
        except TranscodeError as exc:
            QMessageBox.critical(window, APP_TITLE, f"无法载入 FlashCut HLM：\n{exc}")
    return app.exec()


if __name__ == "__main__":
    if "--cli" in sys.argv or "--version" in sys.argv:
        cli_args = [argument for argument in sys.argv[1:] if argument != "--cli"]
        raise SystemExit(main(cli_args))
    raise SystemExit(gui_main(sys.argv[1:]))
