from __future__ import annotations

import argparse
import json
import os
import time
from datetime import datetime


def _latest_session_path(root: str) -> str | None:
    if not os.path.isdir(root):
        return None
    candidates = [os.path.join(root, f) for f in os.listdir(root) if f.endswith(".jsonl")]
    if not candidates:
        return None
    candidates.sort(key=lambda p: os.path.getmtime(p), reverse=True)
    return candidates[0]


def _format_entry(data: dict) -> str:
    ts = data.get("ts")
    if isinstance(ts, (int, float)):
        when = datetime.fromtimestamp(ts).strftime("%H:%M:%S")
    else:
        when = "--:--:--"
    kind = data.get("kind", "unknown")
    tags = data.get("tags") or []
    tag_str = f" [{', '.join(tags)}]" if tags else ""
    text = str(data.get("text", "")).strip()
    return f"{when} {kind}{tag_str}: {text}"


def _load_tail(path: str, tail: int, kind_filter: str | None) -> list[str]:
    if tail <= 0:
        return []
    try:
        with open(path, "r", encoding="utf-8") as f:
            lines = f.readlines()
    except FileNotFoundError:
        return []
    out: list[str] = []
    for line in reversed(lines):
        try:
            data = json.loads(line)
        except Exception:
            continue
        if kind_filter and data.get("kind") != kind_filter:
            continue
        out.append(_format_entry(data))
        if len(out) >= tail:
            break
    return list(reversed(out))


def _follow(path: str, kind_filter: str | None) -> None:
    with open(path, "r", encoding="utf-8") as f:
        f.seek(0, os.SEEK_END)
        while True:
            line = f.readline()
            if not line:
                time.sleep(0.2)
                continue
            try:
                data = json.loads(line)
            except Exception:
                continue
            if kind_filter and data.get("kind") != kind_filter:
                continue
            print(_format_entry(data), flush=True)


def main() -> None:
    parser = argparse.ArgumentParser(description="Live Mechus transcript viewer")
    parser.add_argument("--path", help="Path to a session .jsonl file")
    parser.add_argument("--root", default="mechus_notes", help="Notes directory")
    parser.add_argument("--tail", type=int, default=20, help="Show last N entries")
    parser.add_argument("--kind", default="transcript", help="Filter by kind (default: transcript)")
    parser.add_argument("--no-follow", action="store_true", help="Do not follow live updates")
    args = parser.parse_args()

    path = args.path or _latest_session_path(args.root)
    if not path:
        print("No session file found.")
        return

    for line in _load_tail(path, args.tail, args.kind):
        print(line)

    if args.no_follow:
        return

    _follow(path, args.kind)


if __name__ == "__main__":
    main()
