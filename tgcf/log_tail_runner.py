"""Run tgcf while keeping logs.txt capped to the newest N lines."""

from __future__ import annotations

import argparse
import os
import queue
import re
import signal
import subprocess
import sys
import threading
import time
from collections import deque
from pathlib import Path
from typing import Deque, Optional


SHUTDOWN = False
PROGRESS_RE = re.compile(r"^((?:download|upload)[^:]{0,180}):\s*(\d{1,3})%", re.IGNORECASE)


def _request_shutdown(_signum=None, _frame=None) -> None:
    global SHUTDOWN
    SHUTDOWN = True


def _write_tail(path: Path, lines: Deque[str]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temp_path = path.with_suffix(path.suffix + ".tmp")
    data = "".join(lines)
    try:
        temp_path.write_text(data, encoding="utf-8", errors="replace")
        os.replace(temp_path, path)
    except OSError:
        try:
            path.write_text(data, encoding="utf-8", errors="replace")
        finally:
            try:
                temp_path.unlink(missing_ok=True)
            except OSError:
                pass


def _is_pid_running(pid: int) -> bool:
    if pid <= 0:
        return False
    try:
        os.kill(pid, 0)
        return True
    except OSError:
        return False


def _acquire_runner_lock(lock_file: Path) -> bool:
    lock_file.parent.mkdir(parents=True, exist_ok=True)
    try:
        pid = int(lock_file.read_text(encoding="utf-8").strip() or "0")
    except (FileNotFoundError, ValueError, OSError):
        pid = 0
    if pid and _is_pid_running(pid):
        return False
    try:
        lock_file.unlink(missing_ok=True)
    except OSError:
        pass
    try:
        fd = os.open(str(lock_file), os.O_CREAT | os.O_EXCL | os.O_WRONLY)
    except FileExistsError:
        return False
    with os.fdopen(fd, "w", encoding="utf-8") as file:
        file.write(str(os.getpid()))
    return True


def _release_runner_lock(lock_file: Path) -> None:
    try:
        if lock_file.read_text(encoding="utf-8").strip() == str(os.getpid()):
            lock_file.unlink(missing_ok=True)
    except OSError:
        pass


def _append_segment(
    lines: Deque[str],
    segment: str,
    progress_state: dict[str, tuple[int, float]],
) -> None:
    text = segment.strip()
    if not text:
        return

    progress_match = PROGRESS_RE.search(text)
    if progress_match:
        key = progress_match.group(1).strip().lower()
        percent = int(progress_match.group(2))
        now = time.time()
        previous = progress_state.get(key)
        if previous and previous[0] == percent and now - previous[1] < 1.0:
            return
        progress_state[key] = (percent, now)

    lines.append(text + "\n")


def _reader(pipe, out_queue: "queue.Queue[Optional[str]]") -> None:
    """Read child output without blocking the process supervisor loop."""

    current = []
    try:
        while True:
            char = pipe.read(1)
            if not char:
                break
            if char in "\r\n":
                if current:
                    out_queue.put("".join(current))
                    current.clear()
            else:
                current.append(char)
        if current:
            out_queue.put("".join(current))
    finally:
        out_queue.put(None)


def _stream_capped_logs(process: subprocess.Popen[str], log_file: Path, max_lines: int) -> int:
    lines: Deque[str] = deque(maxlen=max_lines)
    output_queue: "queue.Queue[Optional[str]]" = queue.Queue(maxsize=max(1000, max_lines * 2))
    progress_state: dict[str, tuple[int, float]] = {}
    reader_thread = threading.Thread(
        target=_reader,
        args=(process.stdout, output_queue),
        daemon=True,
    )
    reader_thread.start()
    last_write_at = 0.0
    write_interval = 1.0
    reader_done = False

    def flush(force: bool = False) -> None:
        nonlocal last_write_at
        now = time.time()
        if force or now - last_write_at >= write_interval:
            _write_tail(log_file, lines)
            last_write_at = now

    while not reader_done:
        if SHUTDOWN and process.poll() is None:
            process.terminate()
            try:
                process.wait(timeout=10)
            except subprocess.TimeoutExpired:
                process.kill()

        try:
            raw_line = output_queue.get(timeout=0.2)
        except queue.Empty:
            if process.poll() is not None and not reader_thread.is_alive():
                break
            flush()
            continue

        if raw_line is None:
            reader_done = True
            continue

        for segment in raw_line.replace("\r", "\n").split("\n"):
            _append_segment(lines, segment, progress_state)
        flush()

    reader_thread.join(timeout=2)
    if process.poll() is None:
        process.wait()
    flush(force=True)
    return process.returncode if process.returncode is not None else 0


def main(argv: Optional[list[str]] = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("mode", choices=("live", "past"))
    parser.add_argument("--log-file", default="logs.txt")
    parser.add_argument("--max-lines", type=int, default=1000)
    args = parser.parse_args(argv)

    signal.signal(signal.SIGTERM, _request_shutdown)
    signal.signal(signal.SIGINT, _request_shutdown)

    project_root = Path(__file__).resolve().parents[1]
    run_script = project_root / "run_tgcf.py"
    log_file = Path(args.log_file).resolve()
    lock_file = log_file.with_suffix(log_file.suffix + f".{args.mode}.lock")
    if not _acquire_runner_lock(lock_file):
        _write_tail(
            log_file,
            deque(
                [f"log_tail_runner already running for mode={args.mode}; refusing duplicate start\n"],
                maxlen=max(1, args.max_lines),
            ),
        )
        return 2
    _write_tail(log_file, deque(maxlen=max(1, args.max_lines)))

    env = os.environ.copy()
    env["PYTHONUNBUFFERED"] = "1"
    popen_kwargs = {
        "stdout": subprocess.PIPE,
        "stderr": subprocess.STDOUT,
        "stdin": subprocess.DEVNULL,
        "text": True,
        "encoding": "utf-8",
        "errors": "replace",
        "bufsize": 0,
        "env": env,
        "cwd": str(project_root),
    }
    if os.name == "nt":
        popen_kwargs["creationflags"] = subprocess.CREATE_NEW_PROCESS_GROUP
    else:
        popen_kwargs["start_new_session"] = True

    try:
        process = subprocess.Popen(
            [sys.executable, "-u", str(run_script), args.mode, "--loud"],
            **popen_kwargs,
        )
        return _stream_capped_logs(process, log_file, max(1, args.max_lines))
    finally:
        _release_runner_lock(lock_file)


if __name__ == "__main__":
    raise SystemExit(main())
