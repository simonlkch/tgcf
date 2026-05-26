import os
import re
import signal
import subprocess
import sys
import time

import streamlit as st

from tgcf.config import CONFIG, read_config, write_config
from tgcf.web_ui.password import check_password
from tgcf.web_ui.utils import hide_st, switch_theme

CONFIG = read_config()


def _extract_progress(log_lines):
    """Extract latest download progress line produced by tqdm."""

    for line in reversed(log_lines):
        if "download msg" not in line:
            continue
        msg_match = re.search(r"download msg\s+(\d+):\s*(\d+)%", line)
        if not msg_match:
            continue
        size_match = re.search(r"\|\s*([0-9.]+[KMG]?)/([0-9.]+[KMG]?)", line)
        return {
            "message_id": int(msg_match.group(1)),
            "percent": int(msg_match.group(2)),
            "size": f"{size_match.group(1)}/{size_match.group(2)}" if size_match else "",
            "line": line.strip(),
        }
    return None


def _log_summary(log_lines):
    return {
        "total": len(log_lines),
        "warning": sum(1 for line in log_lines if "WARNING" in line),
        "error": sum(1 for line in log_lines if "ERROR" in line),
    }


def _extract_progress_history(log_lines, limit=200):
    history = []
    for line in log_lines:
        if "download msg" not in line:
            continue
        match = re.search(r"download msg\s+(\d+):\s*(\d+)%", line)
        if not match:
            continue
        history.append(
            {
                "message_id": int(match.group(1)),
                "percent": int(match.group(2)),
            }
        )
    return history[-limit:]


def _extract_recent_events(log_lines, max_rows=80):
    rows = []
    for line in log_lines:
        level = None
        if " ERROR " in line:
            level = "ERROR"
        elif " WARNING " in line:
            level = "WARNING"
        elif " INFO " in line:
            level = "INFO"
        if not level:
            continue
        rows.append(
            {
                "level": level,
                "message": line.strip(),
            }
        )
    return rows[-max_rows:]


def termination():
    st.code("process terminated!")
    os.rename("logs.txt", "old_logs.txt")
    with open("old_logs.txt", "r") as f:
        st.download_button(
            "Download last logs", data=f.read(), file_name="tgcf_logs.txt"
        )

    CONFIG = read_config()
    CONFIG.pid = 0
    write_config(CONFIG)
    st.button("Refresh page")


st.set_page_config(
    page_title="Run",
    page_icon="🏃",
    layout="wide",
)
hide_st(st)
switch_theme(st,CONFIG)
if check_password(st):
    with st.expander("Current Runtime Summary", expanded=True):
        st.write(f"**Mode:** {'past' if CONFIG.mode == 1 else 'live'}")
        st.write(f"**Show Forwarded from:** {'Yes' if CONFIG.show_forwarded_from else 'No'}")
        st.write(f"**Live delete sync:** {'Yes' if CONFIG.live.delete_sync else 'No'}")
        st.write(f"**Album debounce:** {CONFIG.live.album_debounce_ms} ms")
        st.write(f"**Album atomic rollback:** {'Yes' if CONFIG.live.album_atomic else 'No'}")
        st.write(
            f"**Fallback re-upload when forward is blocked:** {'Yes' if CONFIG.live.forward_fallback_to_reupload else 'No'}"
        )
        st.write(f"**Retry on 429 / FloodWait:** {'Yes' if CONFIG.live.retry_on_429 else 'No'}")
        st.write(
            f"**Retry backoff base:** {CONFIG.live.retry_backoff_base_seconds} seconds"
        )
        st.write(
            f"**Max retries for non-429 errors:** {CONFIG.live.retry_max_attempts_for_non_429}"
        )
        st.write(
            f"**Max retries for 429 / FloodWait:** {CONFIG.live.retry_max_attempts_for_flood_wait}"
        )
        if CONFIG.mode == 1:
            st.write(f"**Past delay:** {CONFIG.past.delay} seconds")

    with st.expander("Configure Run"):
        CONFIG.show_forwarded_from = st.checkbox(
            "Show 'Forwarded from'", value=CONFIG.show_forwarded_from
        )
        mode = st.radio("Choose mode", ["live", "past"], index=CONFIG.mode)
        if mode == "past":
            CONFIG.mode = 1
            st.warning(
                "Only User Account can be used in Past mode. Telegram does not allow bot account to go through history of a chat!"
            )
            CONFIG.past.delay = st.slider(
                "Delay in seconds", 0, 100, value=CONFIG.past.delay
            )
        else:
            CONFIG.mode = 0
            CONFIG.live.delete_sync = st.checkbox(
                "Sync when a message is deleted", value=CONFIG.live.delete_sync
            )

        if st.button("Save"):
            write_config(CONFIG)

    check = False

    if CONFIG.pid == 0:
        check = st.button("Run", type="primary")

    if CONFIG.pid != 0:
        st.warning(
            "You must click stop and then re-run tgcf to apply changes in config."
        )
        # check if process is running using pid
        try:
            os.kill(CONFIG.pid, 0)
        except Exception as err:
            st.code("The process has stopped.")
            st.code(err)
            CONFIG.pid = 0
            write_config(CONFIG)
            time.sleep(1)
            st.rerun()

        stop = st.button("Stop", type="primary")
        if stop:
            try:
                os.kill(CONFIG.pid, signal.SIGTERM)
            except Exception as err:
                st.code(err)

                CONFIG.pid = 0
                write_config(CONFIG)
                st.button("Refresh Page")

            else:
                termination()

    if check:
        project_root = os.path.abspath(
            os.path.join(os.path.dirname(__file__), "..", "..", "..")
        )
        run_script = os.path.join(project_root, "run_tgcf.py")
        run_mode = "past" if CONFIG.mode == 1 else "live"
        with open("logs.txt", "w") as logs:
            popen_kwargs = {
                "stdout": logs,
                "stderr": subprocess.STDOUT,
                "stdin": subprocess.DEVNULL,
            }
            if os.name == "nt":
                popen_kwargs["creationflags"] = subprocess.CREATE_NEW_PROCESS_GROUP
            else:
                popen_kwargs["start_new_session"] = True

            process = subprocess.Popen(
                [sys.executable, run_script, run_mode, "--loud"],
                **popen_kwargs,
            )

        if process.poll() is None:
            CONFIG.pid = process.pid
            write_config(CONFIG)
        else:
            CONFIG.pid = 0
            write_config(CONFIG)
            st.error(f"tgcf exited immediately with code {process.returncode}. Check logs below.")
        time.sleep(2)

        st.rerun()

    try:
        with open("logs.txt", "r", encoding="utf8", errors="replace") as file:
            log_lines = file.readlines()

        summary = _log_summary(log_lines)
        metric_1, metric_2, metric_3, metric_4 = st.columns(4)
        metric_1.metric("Log lines", summary["total"])
        metric_2.metric("Warnings", summary["warning"])
        metric_3.metric("Errors", summary["error"])
        metric_4.metric("Process", "Running" if CONFIG.pid != 0 else "Stopped")

        progress = _extract_progress(log_lines)
        if progress:
            st.info(
                f"Message {progress['message_id']} transfer: {progress['percent']}% {progress['size']}"
            )
            st.progress(progress["percent"] / 100)

        progress_history = _extract_progress_history(log_lines)
        recent_events = _extract_recent_events(log_lines)

        tabs = st.tabs(["Overview", "Events", "Logs"])

        with tabs[0]:
            left, right = st.columns([2, 1])
            with left:
                if progress_history:
                    st.write("Transfer progress trend")
                    st.line_chart(
                        {
                            "percent": [item["percent"] for item in progress_history],
                        },
                        use_container_width=True,
                    )
                else:
                    st.info("No transfer progress data yet.")
            with right:
                if progress:
                    st.write("Latest transfer")
                    st.dataframe([progress], use_container_width=True)
                st.write("Health summary")
                st.dataframe([summary], use_container_width=True)

        with tabs[1]:
            if recent_events:
                st.dataframe(recent_events, use_container_width=True, height=520)
            else:
                st.info("No INFO/WARNING/ERROR events found yet.")

        with tabs[2]:
            ctl_left, ctl_mid, ctl_right, ctl_four = st.columns(4)
            with ctl_left:
                lines = st.slider(
                    "Lines of logs to show", min_value=100, max_value=5000, step=100, value=1000
                )
            with ctl_mid:
                keyword = st.text_input("Filter keyword", value="")
            with ctl_right:
                only_problems = st.checkbox("Only warnings/errors", value=False)
            with ctl_four:
                if st.button("Refresh logs"):
                    st.rerun()

            visible_lines = log_lines
            if keyword.strip():
                lowered = keyword.strip().lower()
                visible_lines = [line for line in visible_lines if lowered in line.lower()]
            if only_problems:
                visible_lines = [
                    line
                    for line in visible_lines
                    if "WARNING" in line or "ERROR" in line
                ]

            tail_text = "".join(visible_lines[-lines:])
            st.text_area(
                "Log output",
                value=tail_text,
                height=560,
                disabled=True,
            )
            st.caption(f"Showing {min(lines, len(visible_lines))} line(s)")
            st.download_button(
                "Download visible logs",
                data=tail_text,
                file_name="tgcf_visible_logs.txt",
            )

    except FileNotFoundError:
        st.write("No present logs found")
    st.button("Load more logs")
