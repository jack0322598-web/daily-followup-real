#!/usr/bin/env python3
"""Restore generated archives and caches from the current Pages deployment."""

from __future__ import annotations

import os
import re
import urllib.error
import urllib.request
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
MAX_DOWNLOAD_SIZE = 10 * 1024 * 1024
REQUEST_HEADERS = {
    "User-Agent": "Mozilla/5.0 (compatible; DailyBriefingStateSync/1.0)",
    "Accept": "text/html,application/javascript,application/json;q=0.9,*/*;q=0.8",
}
STATE_FILES = (
    "industry_source_cache.json",
    "industry_trend_cache.json",
    "weekly_keywords.json",
    "trellis_yesterday_news.json",
    "summary_cache.json",
)


def download(base_url: str, remote_path: str, destination: Path, required: bool = False) -> bool:
    url = f"{base_url.rstrip('/')}/{remote_path.lstrip('/')}"
    request = urllib.request.Request(url, headers=REQUEST_HEADERS)
    try:
        with urllib.request.urlopen(request, timeout=30) as response:
            data = response.read(MAX_DOWNLOAD_SIZE + 1)
    except urllib.error.HTTPError as exc:
        if not required and exc.code == 404:
            exc.close()
            return False
        raise
    if len(data) > MAX_DOWNLOAD_SIZE:
        raise RuntimeError(f"Deployed file is unexpectedly large: {remote_path}")
    temporary = destination.with_suffix(destination.suffix + ".tmp")
    temporary.write_bytes(data)
    temporary.replace(destination)
    return True


def sync_deployed() -> list[str]:
    base_url = os.environ.get("SITE_URL", "").strip().rstrip("/")
    if not base_url:
        print("SITE_URL is empty; skipping deployed-state sync.")
        return []

    try:
        request = urllib.request.Request(f"{base_url}/archive_list.js", headers=REQUEST_HEADERS)
        with urllib.request.urlopen(request, timeout=30) as response:
            archive_list = response.read(MAX_DOWNLOAD_SIZE + 1).decode("utf-8", errors="ignore")
    except (urllib.error.HTTPError, urllib.error.URLError, TimeoutError, OSError) as exc:
        if isinstance(exc, urllib.error.HTTPError):
            exc.close()
        require_deployed_state = os.environ.get("REQUIRE_DEPLOYED_STATE", "").strip().lower() in {
            "1",
            "true",
            "yes",
            "on",
        }
        if require_deployed_state:
            raise RuntimeError(
                f"Required deployed state is unavailable at {base_url}: {type(exc).__name__}"
            ) from exc
        print(
            "Previous deployment is not available yet "
            f"({type(exc).__name__}); starting from repository files."
        )
        return []

    dates = sorted(set(re.findall(r"\d{4}-\d{2}-\d{2}", archive_list)))
    synced = []
    for name in ("index.html", "share_index.html", "archive_list.js"):
        if download(base_url, name, ROOT / name):
            synced.append(name)
    for value in dates:
        name = f"archive_{value}.html"
        if not (ROOT / name).exists() and download(base_url, name, ROOT / name, required=True):
            synced.append(name)
    for name in STATE_FILES:
        if download(base_url, f"_state/{name}", ROOT / name):
            synced.append(name)

    print(f"Restored {len(synced)} files from {base_url}")
    return synced


def main() -> int:
    sync_deployed()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
