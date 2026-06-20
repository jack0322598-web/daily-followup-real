#!/usr/bin/env python3
"""Create a Cloudflare Pages upload directory containing public assets only."""

from __future__ import annotations

import json
import shutil
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
PUBLIC_DIR = ROOT / "public"
PUBLIC_SUFFIXES = {".html", ".css", ".js", ".png", ".jpg", ".jpeg", ".gif", ".svg", ".svgz", ".webp", ".ico"}
MAX_FILE_SIZE = 25 * 1024 * 1024


def public_files() -> list[Path]:
    return sorted(
        path for path in ROOT.iterdir()
        if path.is_file() and not path.name.startswith(".") and path.suffix.lower() in PUBLIC_SUFFIXES
    )


def build_public() -> list[Path]:
    if PUBLIC_DIR.exists():
        shutil.rmtree(PUBLIC_DIR)
    PUBLIC_DIR.mkdir(parents=True)

    copied = []
    for source in public_files():
        if source.stat().st_size > MAX_FILE_SIZE:
            raise RuntimeError(f"Cloudflare Pages file limit exceeded: {source.name}")
        destination = PUBLIC_DIR / source.name
        shutil.copy2(source, destination)
        copied.append(destination)

    required = [PUBLIC_DIR / "index.html", PUBLIC_DIR / "archive_list.js"]
    missing = [path.name for path in required if not path.exists()]
    if missing:
        raise RuntimeError(f"Missing required public files: {', '.join(missing)}")
    if not list(PUBLIC_DIR.glob("archive_*.html")):
        raise RuntimeError("No archive files were staged")

    (PUBLIC_DIR / "_headers").write_text(
        "/*.html\n  Cache-Control: no-cache\n/archive_list.js\n  Cache-Control: no-cache\n",
        encoding="utf-8",
    )
    (PUBLIC_DIR / "deploy-metadata.json").write_text(
        json.dumps({"file_count": len(copied)}, indent=2),
        encoding="utf-8",
    )
    return copied


def main() -> int:
    copied = build_public()
    print(f"Prepared {len(copied)} public files in {PUBLIC_DIR}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
