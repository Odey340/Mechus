from __future__ import annotations

import os
import tarfile
import urllib.request


DEFAULT_URL = "https://alphacephei.com/vosk/models/vosk-model-small-en-us-0.15.zip"


def _download(url: str, dest_path: str) -> None:
    os.makedirs(os.path.dirname(dest_path), exist_ok=True)
    urllib.request.urlretrieve(url, dest_path)


def main() -> None:
    import argparse

    p = argparse.ArgumentParser()
    p.add_argument("--url", default=DEFAULT_URL)
    p.add_argument("--out", default="models")
    args = p.parse_args()

    out_dir = os.path.abspath(args.out)
    os.makedirs(out_dir, exist_ok=True)

    archive = os.path.join(out_dir, os.path.basename(args.url))
    print(f"Downloading {args.url}")
    _download(args.url, archive)
    print(f"Saved {archive}")

    if archive.endswith(".zip"):
        import zipfile

        with zipfile.ZipFile(archive) as z:
            z.extractall(out_dir)
        print(f"Extracted to {out_dir}")
        return

    if archive.endswith(".tar.gz") or archive.endswith(".tgz"):
        with tarfile.open(archive, "r:gz") as t:
            t.extractall(out_dir)
        print(f"Extracted to {out_dir}")
        return

    print("Unknown archive format; extract manually.")


if __name__ == "__main__":
    main()

