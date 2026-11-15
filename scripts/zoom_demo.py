from __future__ import annotations

import argparse
import os
import platform
import subprocess
import sys
from urllib.parse import parse_qsl, urlencode, urlparse, urlunparse

from mechus.envfile import load_env_file


def _open_link(link: str) -> None:
    link = link.strip()
    if not link:
        return
    system = platform.system().lower()
    if system == "darwin":
        subprocess.Popen(["open", link])
        return
    if system == "windows":
        os.startfile(link)  # type: ignore[attr-defined]
        return
        subprocess.Popen(["xdg-open", link])


def _maybe_with_display_name(link: str, display_name: str | None) -> str:
    """For Zoom browser links, add a display name so Mechus shows up as itself."""
    if not link or not display_name:
        return link

    parsed = urlparse(link)
    if "zoom.us" not in parsed.netloc:
        return link

    query = dict(parse_qsl(parsed.query, keep_blank_values=True))
    # Do not override if caller already provided one.
    query.setdefault("uname", display_name)
    parsed = parsed._replace(query=urlencode(query))
    return urlunparse(parsed)


def main() -> None:
    p = argparse.ArgumentParser()
    p.add_argument("--env", default=".env")
    p.add_argument("--link", default=None)
    p.add_argument("--name", default="Mechus", help="Display name to pass to Zoom browser links")
    p.add_argument("--no-open", action="store_true")
    args = p.parse_args()

    repo_root = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
    if repo_root not in sys.path:
        sys.path.insert(0, repo_root)

    load_env_file(args.env)

    link = args.link
    if link is None:
        sys.stdout.write("Paste Zoom meeting link (or press Enter to skip): ")
        sys.stdout.flush()
        link = sys.stdin.readline().strip()

    link = _maybe_with_display_name(link, args.name)

    if link and not args.no_open:
        _open_link(link)

    from main import main as run_main

    run_main()


if __name__ == "__main__":
    main()
