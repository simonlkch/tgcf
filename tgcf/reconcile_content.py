"""Content-level reconciliation for source/destination Telegram channels.

This tool compares messages by content fingerprints instead of message IDs,
which is useful when destination message IDs differ from source IDs.
"""

from __future__ import annotations

import argparse
import asyncio
import bisect
import csv
import json
import math
import os
import re
from dataclasses import dataclass
from datetime import datetime, timezone
from difflib import SequenceMatcher
from typing import Dict, Iterable, List, Optional, Sequence, Tuple

from telethon import TelegramClient
from telethon.tl.patched import MessageService

from tgcf.config import CONFIG, Forward, get_SESSION


def _normalize_text(text: str) -> str:
    if not text:
        return ""
    text = text.replace("\r\n", "\n").replace("\r", "\n")
    text = re.sub(r"\s+", " ", text).strip().lower()
    return text


def _dt_to_iso(ts: Optional[datetime]) -> str:
    if ts is None:
        return ""
    if ts.tzinfo is None:
        ts = ts.replace(tzinfo=timezone.utc)
    return ts.isoformat()


def _safe_console_text(text: str) -> str:
    return (text or "").encode("ascii", errors="backslashreplace").decode("ascii")


def _epoch_seconds(ts: Optional[datetime]) -> float:
    if ts is None:
        return float("-inf")
    if ts.tzinfo is None:
        ts = ts.replace(tzinfo=timezone.utc)
    return ts.timestamp()


@dataclass
class MsgSnapshot:
    message_id: int
    date: Optional[datetime]
    text: str
    text_norm: str
    grouped_id: Optional[int]
    media_type: str
    mime_type: str
    size_bytes: int
    duration: int
    width: int
    height: int


@dataclass
class MatchResult:
    source: MsgSnapshot
    destination: Optional[MsgSnapshot]
    score: float
    text_score: float
    media_score: float
    time_score: float
    verdict: str


def _msg_media_type(msg) -> str:
    if getattr(msg, "photo", None):
        return "photo"
    if getattr(msg, "video", None):
        return "video"
    if getattr(msg, "document", None):
        return "document"
    if getattr(msg, "audio", None):
        return "audio"
    if getattr(msg, "voice", None):
        return "voice"
    if getattr(msg, "sticker", None):
        return "sticker"
    if getattr(msg, "gif", None):
        return "gif"
    if getattr(msg, "media", None):
        return type(msg.media).__name__.lower()
    return "none"


def _safe_int(v) -> int:
    try:
        return int(v or 0)
    except (TypeError, ValueError):
        return 0


def _snapshot_from_message(msg) -> MsgSnapshot:
    file_obj = getattr(msg, "file", None)
    text = (getattr(msg, "raw_text", None) or getattr(msg, "message", "") or "").strip()
    return MsgSnapshot(
        message_id=int(msg.id),
        date=getattr(msg, "date", None),
        text=text,
        text_norm=_normalize_text(text),
        grouped_id=getattr(msg, "grouped_id", None),
        media_type=_msg_media_type(msg),
        mime_type=(getattr(file_obj, "mime_type", "") or "").lower(),
        size_bytes=_safe_int(getattr(file_obj, "size", 0)),
        duration=_safe_int(getattr(file_obj, "duration", 0)),
        width=_safe_int(getattr(file_obj, "width", 0)),
        height=_safe_int(getattr(file_obj, "height", 0)),
    )


def _text_score(a: MsgSnapshot, b: MsgSnapshot) -> float:
    if not a.text_norm and not b.text_norm:
        return 1.0
    if not a.text_norm or not b.text_norm:
        return 0.0
    if a.text_norm == b.text_norm:
        return 1.0
    return SequenceMatcher(None, a.text_norm, b.text_norm).ratio()


def _ratio_with_tolerance(a: int, b: int, tolerance: float = 0.02) -> float:
    if a <= 0 and b <= 0:
        return 1.0
    if a <= 0 or b <= 0:
        return 0.0
    larger = max(a, b)
    diff = abs(a - b)
    allowed = max(1, int(larger * tolerance))
    if diff <= allowed:
        return 1.0
    return max(0.0, 1.0 - (diff / larger))


def _media_score(a: MsgSnapshot, b: MsgSnapshot) -> float:
    if a.media_type == "none" and b.media_type == "none":
        return 1.0
    if a.media_type == "none" or b.media_type == "none":
        return 0.0

    score = 0.0
    weight = 0.0

    score += (1.0 if a.media_type == b.media_type else 0.0) * 0.35
    weight += 0.35

    if a.mime_type or b.mime_type:
        score += (1.0 if a.mime_type == b.mime_type else 0.0) * 0.2
        weight += 0.2

    score += _ratio_with_tolerance(a.size_bytes, b.size_bytes, tolerance=0.03) * 0.25
    weight += 0.25

    score += _ratio_with_tolerance(a.duration, b.duration, tolerance=0.05) * 0.1
    weight += 0.1

    if (a.width > 0 and a.height > 0) or (b.width > 0 and b.height > 0):
        dims_equal = 1.0 if (a.width == b.width and a.height == b.height) else 0.0
        score += dims_equal * 0.1
        weight += 0.1

    if weight <= 0:
        return 0.0
    return score / weight


def _time_score(a: MsgSnapshot, b: MsgSnapshot, half_life_seconds: int = 6 * 3600) -> float:
    if not a.date or not b.date:
        return 0.5
    diff_seconds = abs((b.date - a.date).total_seconds())
    return math.exp(-diff_seconds / max(1, half_life_seconds))


def _combined_score(a: MsgSnapshot, b: MsgSnapshot) -> Tuple[float, float, float, float]:
    t_score = _text_score(a, b)
    # User-requested fallback: compare only caption + MIME type + blob size.
    mime_score = 1.0 if a.mime_type == b.mime_type else 0.0
    size_score = _ratio_with_tolerance(a.size_bytes, b.size_bytes, tolerance=0.03)
    m_score = (0.5 * mime_score) + (0.5 * size_score)
    tm_score = 0.0
    total = (0.5 * t_score) + (0.5 * m_score)
    return total, t_score, m_score, tm_score


def _verdict(score: float) -> str:
    if score >= 0.90:
        return "confirmed_matched"
    if score >= 0.75:
        return "likely_matched"
    return "suspected_missing"


async def _connect_client() -> TelegramClient:
    session = get_SESSION()
    client = TelegramClient(
        session,
        CONFIG.login.API_ID,
        CONFIG.login.API_HASH,
        sequential_updates=True,
    )
    if CONFIG.login.user_type == 0:
        await client.start(bot_token=CONFIG.login.BOT_TOKEN)
    else:
        await client.start()
    return client


async def _collect_snapshots(client: TelegramClient, entity, full_scan: bool, offset: int, end: int) -> List[MsgSnapshot]:
    snapshots: List[MsgSnapshot] = []
    iter_kwargs = {"reverse": True}
    if not full_scan:
        iter_kwargs["offset_id"] = max(0, offset)
        if end > 0:
            iter_kwargs["max_id"] = end + 1

    async for msg in client.iter_messages(entity, **iter_kwargs):
        if isinstance(msg, MessageService):
            continue
        if not full_scan and end > 0 and int(msg.id) > end:
            break
        snapshots.append(_snapshot_from_message(msg))
        if len(snapshots) % 500 == 0:
            print(f"    collected {len(snapshots)} messages...", flush=True)
    return snapshots


def _index_destination(dest: Sequence[MsgSnapshot]) -> Dict[str, List[int]]:
    by_text: Dict[str, List[int]] = {}
    by_media: Dict[str, List[int]] = {}
    by_media_type: Dict[str, List[int]] = {}
    date_epochs: List[float] = []
    for idx, m in enumerate(dest):
        if m.text_norm:
            by_text.setdefault(m.text_norm, []).append(idx)
        media_key = f"{m.media_type}|{m.mime_type}|{m.size_bytes}|{m.duration}|{m.width}|{m.height}"
        by_media.setdefault(media_key, []).append(idx)
        by_media_type.setdefault(m.media_type, []).append(idx)
        date_epochs.append(_epoch_seconds(m.date))
    return {
        "by_text": by_text,
        "by_media": by_media,
        "by_media_type": by_media_type,
        "date_epochs": date_epochs,
    }


def _candidate_indices(src: MsgSnapshot, dest: Sequence[MsgSnapshot], index: Dict[str, Dict[str, List[int]]]) -> List[int]:
    candidates: List[int] = []
    media_key = f"{src.media_type}|{src.mime_type}|{src.size_bytes}|{src.duration}|{src.width}|{src.height}"
    candidates.extend(index["by_media"].get(media_key, []))
    if src.text_norm:
        candidates.extend(index["by_text"].get(src.text_norm, []))

    # If strict keys did not find anything, fallback to nearby timestamp window.
    if not candidates and src.date:
        src_epoch = _epoch_seconds(src.date)
        if src_epoch != float("-inf"):
            epochs = index["date_epochs"]
            lo = bisect.bisect_left(epochs, src_epoch - (2 * 24 * 3600))
            hi = bisect.bisect_right(epochs, src_epoch + (2 * 24 * 3600))
            candidates.extend(range(lo, min(hi, len(dest))))

    # Next fallback: same media type only.
    if not candidates:
        candidates.extend(index["by_media_type"].get(src.media_type, []))

    # Final fallback: cap to first 2000 destinations to avoid O(n^2) explosion.
    if not candidates:
        return list(range(min(2000, len(dest))))

    # Deduplicate while preserving order.
    seen = set()
    unique: List[int] = []
    for idx in candidates:
        if idx in seen:
            continue
        seen.add(idx)
        unique.append(idx)
    return unique


def reconcile_content(source: Sequence[MsgSnapshot], destination: Sequence[MsgSnapshot]) -> List[MatchResult]:
    results: List[MatchResult] = []
    used_dest_ids = set()

    keyed_dest: Dict[Tuple[str, str], List[MsgSnapshot]] = {}
    for d in destination:
        key = (d.text_norm, d.mime_type)
        keyed_dest.setdefault(key, []).append(d)

    for i, src in enumerate(source, start=1):
        key = (src.text_norm, src.mime_type)
        candidates = keyed_dest.get(key, [])
        candidates = [d for d in candidates if d.message_id not in used_dest_ids]

        if not candidates:
            results.append(
                MatchResult(
                    source=src,
                    destination=None,
                    score=0.0,
                    text_score=0.0,
                    media_score=0.0,
                    time_score=0.0,
                    verdict="suspected_missing",
                )
            )
            if i % 500 == 0:
                print(f"    matched {i}/{len(source)} source messages...", flush=True)
            continue

        # Caption + MIME must match already; choose closest blob size.
        best = max(candidates, key=lambda d: _ratio_with_tolerance(src.size_bytes, d.size_bytes, tolerance=0.03))
        used_dest_ids.add(best.message_id)

        size_score = _ratio_with_tolerance(src.size_bytes, best.size_bytes, tolerance=0.03)
        text_score = 1.0
        media_score = 0.5 + (0.5 * size_score)
        total = (text_score + 1.0 + size_score) / 3.0

        if size_score >= 0.97:
            verdict = "confirmed_matched"
        elif size_score >= 0.85:
            verdict = "likely_matched"
        else:
            verdict = "suspected_missing"

        results.append(
            MatchResult(
                source=src,
                destination=best,
                score=total,
                text_score=text_score,
                media_score=media_score,
                time_score=0.0,
                verdict=verdict,
            )
        )
        if i % 500 == 0:
            print(f"    matched {i}/{len(source)} source messages...", flush=True)

    return results


def _report_paths(base_dir: str, conn_name: str) -> Tuple[str, str]:
    ts = datetime.now().strftime("%Y%m%d_%H%M%S")
    safe = re.sub(r"[^A-Za-z0-9._-]+", "_", conn_name).strip("_") or "connection"
    report_dir = os.path.join(base_dir, "document")
    os.makedirs(report_dir, exist_ok=True)
    csv_path = os.path.join(report_dir, f"reconcile_content_{safe}_{ts}.csv")
    json_path = os.path.join(report_dir, f"reconcile_content_{safe}_{ts}.json")
    return csv_path, json_path


def _write_report(
    csv_path: str,
    json_path: str,
    conn_name: str,
    src_chat_id: int,
    dst_chat_id: int,
    full_scan: bool,
    results: Sequence[MatchResult],
) -> None:
    rows = []
    summary = {
        "connection": conn_name,
        "source_chat_id": src_chat_id,
        "destination_chat_id": dst_chat_id,
        "full_scan": full_scan,
        "total_source_messages": len(results),
        "confirmed_matched": 0,
        "likely_matched": 0,
        "suspected_missing": 0,
    }

    for item in results:
        summary[item.verdict] += 1
        dst = item.destination
        rows.append(
            {
                "source_message_id": item.source.message_id,
                "source_date": _dt_to_iso(item.source.date),
                "source_grouped_id": item.source.grouped_id or "",
                "source_media_type": item.source.media_type,
                "source_mime_type": item.source.mime_type,
                "source_size_bytes": item.source.size_bytes,
                "source_text": item.source.text,
                "dest_message_id": dst.message_id if dst else "",
                "dest_date": _dt_to_iso(dst.date) if dst else "",
                "dest_grouped_id": (dst.grouped_id if dst else "") or "",
                "dest_media_type": dst.media_type if dst else "",
                "dest_mime_type": dst.mime_type if dst else "",
                "dest_size_bytes": dst.size_bytes if dst else "",
                "dest_text": dst.text if dst else "",
                "score": round(item.score, 6),
                "text_score": round(item.text_score, 6),
                "media_score": round(item.media_score, 6),
                "time_score": round(item.time_score, 6),
                "verdict": item.verdict,
            }
        )

    with open(csv_path, "w", encoding="utf-8", newline="") as f:
        writer = csv.DictWriter(f, fieldnames=list(rows[0].keys()) if rows else [])
        if rows:
            writer.writeheader()
            writer.writerows(rows)

    with open(json_path, "w", encoding="utf-8") as f:
        json.dump({"summary": summary, "rows": rows}, f, ensure_ascii=False, indent=2)


async def _run_for_connection(forward: Forward, full_scan: bool, base_dir: str) -> Dict[str, str]:
    client = await _connect_client()
    try:
        src_entity = await client.get_entity(forward.source)
        dst_entity = await client.get_entity(forward.dest[0])
        src_chat_id = await client.get_peer_id(src_entity)
        dst_chat_id = await client.get_peer_id(dst_entity)

        source = await _collect_snapshots(
            client,
            src_entity,
            full_scan=full_scan,
            offset=int(forward.offset or 0),
            end=int(forward.end or 0),
        )
        destination = await _collect_snapshots(
            client,
            dst_entity,
            full_scan=True,
            offset=0,
            end=0,
        )

        results = reconcile_content(source, destination)
        conn_name = forward.con_name.strip() or f"{src_chat_id}_to_{dst_chat_id}"
        csv_path, json_path = _report_paths(base_dir, conn_name)
        _write_report(csv_path, json_path, conn_name, src_chat_id, dst_chat_id, full_scan, results)
        return {
            "connection": conn_name,
            "source_chat_id": str(src_chat_id),
            "destination_chat_id": str(dst_chat_id),
            "source_messages": str(len(source)),
            "destination_messages": str(len(destination)),
            "csv": csv_path,
            "json": json_path,
        }
    finally:
        await client.disconnect()


def _enabled_forwards(forward_indices: Optional[Iterable[int]]) -> List[Forward]:
    all_enabled = [f for f in CONFIG.forwards if f.use_this and f.dest]
    if forward_indices is None:
        return all_enabled

    indexed = []
    raw_all = list(CONFIG.forwards)
    for idx in forward_indices:
        if idx < 0 or idx >= len(raw_all):
            raise ValueError(f"connection index out of range: {idx}")
        fw = raw_all[idx]
        if fw.use_this and fw.dest:
            indexed.append(fw)
    return indexed


async def _main_async(args: argparse.Namespace) -> int:
    forwards = _enabled_forwards(args.connection_index)
    if not forwards:
        print("No enabled connections found for reconciliation.")
        return 1

    base_dir = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
    print(f"Running content reconciliation for {len(forwards)} connection(s)")

    for forward in forwards:
        conn_name = forward.con_name.strip() or f"{forward.source}->{forward.dest[0]}"
        print(f"- Processing connection: {_safe_console_text(conn_name)}")
        output = await _run_for_connection(forward, full_scan=args.full, base_dir=base_dir)
        print(
            "  done: source={source_messages} destination={destination_messages} csv={csv} json={json}".format(
                **output
            )
        )
    return 0


def _parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Content-level channel reconciliation")
    parser.add_argument(
        "--full",
        action="store_true",
        help="Scan full source channel history (ignores forward offset/end)",
    )
    parser.add_argument(
        "--connection-index",
        type=int,
        action="append",
        default=None,
        help="Optional index in CONFIG.forwards (can be passed multiple times)",
    )
    return parser.parse_args()


def main() -> int:
    args = _parse_args()
    return asyncio.run(_main_async(args))


if __name__ == "__main__":
    raise SystemExit(main())
