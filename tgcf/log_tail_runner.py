"""Run tgcf while keeping logs.txt capped to the newest N lines."""

from __future__ import annotations

import argparse
import os
import signal
import subprocess
import sys
import time
from collections import deque
from pathlib import Path
from typing import Deque, Optional


SHUTDOWN = False


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


def _append_segment(lines: Deque[str], segment: str) -> None:
    text = segment.strip()
    if text:
        lines.append(text + "\n")


def _stream_capped_logs(process: subprocess.Popen[str], log_file: Path, max_lines: int) -> int:
    lines: Deque[str] = deque(maxlen=max_lines)
    current = []
    last_write_at = 0.0
    write_interval = 0.25

    def flush(force: bool = False) -> None:
        nonlocal last_write_at
        now = time.time()
        if force or now - last_write_at >= write_interval:
            _write_tail(log_file, lines)
            last_write_at = now

    while process.poll() is None and not SHUTDOWN:
        char = process.stdout.read(1) if process.stdout else ""
        if not char:
            time.sleep(0.02)
            flush()
            continue
        if char in "\r\n":
            _append_segment(lines, "".join(current))
            current.clear()
            flush()
        else:
            current.append(char)

    if SHUTDOWN and process.poll() is None:
        process.terminate()
        try:
            process.wait(timeout=10)
        except subprocess.TimeoutExpired:
            process.kill()

    if current:
        _append_segment(lines, "".join(current))
    if process.stdout:
        for rest in process.stdout.read().replace("\r", "\n").split("\n"):
            _append_segment(lines, rest)
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

    process = subprocess.Popen(
        [sys.executable, "-u", str(run_script), args.mode, "--loud"],
        **popen_kwargs,
    )
    return _stream_capped_logs(process, log_file, max(1, args.max_lines))


if __name__ == "__main__":
    raise SystemExit(main())
