#!/usr/bin/env python3
"""Run the daily briefing pipeline safely on PythonAnywhere."""

from __future__ import annotations

import argparse
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
import urllib.request
from contextlib import contextmanager
from datetime import date, datetime, timedelta, timezone
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
KST = timezone(timedelta(hours=9))
LOCK_FILE = ROOT / ".update.lock"
LOG_DIR = ROOT / "logs"
ARCHIVE_PATTERN = re.compile(r"archive_(\d{4}-\d{2}-\d{2})\.html$")
MIN_NEWS_CARDS = 3


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Generate and publish the daily news briefing.")
    parser.add_argument("--date", help="Generate one date only (YYYY-MM-DD).")
    parser.add_argument("--dry-run", action="store_true", help="Show pending dates without running the pipeline.")
    parser.add_argument("--notify-test", action="store_true", help="Send a Slack connection test and exit.")
    return parser.parse_args()


def load_env_file(path: Path) -> None:
    if not path.exists():
        return
    for raw_line in path.read_text(encoding="utf-8", errors="ignore").splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        key = key.strip()
        if key:
            os.environ.setdefault(key, value.strip().strip('"').strip("'"))


def parse_iso_date(value: str) -> date:
    return datetime.strptime(value.strip(), "%Y-%m-%d").date()


def yesterday_kst() -> date:
    return datetime.now(KST).date() - timedelta(days=1)


def archive_dates() -> list[date]:
    found = []
    for path in ROOT.glob("archive_*.html"):
        match = ARCHIVE_PATTERN.fullmatch(path.name)
        if match:
            found.append(parse_iso_date(match.group(1)))
    return sorted(found)


def pending_dates(requested: str | None = None) -> list[date]:
    if requested:
        return [parse_iso_date(requested)]

    target = yesterday_kst()
    existing = [value for value in archive_dates() if value <= target]
    start = (existing[-1] + timedelta(days=1)) if existing else target
    if start > target:
        return []

    dates = []
    current = start
    while current <= target:
        dates.append(current)
        current += timedelta(days=1)

    max_days = max(1, int(os.environ.get("MAX_BACKFILL_DAYS", "7")))
    if len(dates) > max_days:
        raise RuntimeError(
            f"{len(dates)} dates are pending, which exceeds MAX_BACKFILL_DAYS={max_days}. "
            "Run older dates manually before resuming the schedule."
        )
    return dates


def protected_paths(target: date) -> list[Path]:
    return [
        ROOT / "index.html",
        ROOT / "share_index.html",
        ROOT / "archive_list.js",
        ROOT / f"archive_{target.isoformat()}.html",
        ROOT / "industry_source_cache.json",
        ROOT / "industry_trend_cache.json",
        ROOT / "weekly_keywords.json",
        ROOT / "summary_cache.json",
    ]


@contextmanager
def rollback_on_failure(target: date):
    paths = protected_paths(target)
    with tempfile.TemporaryDirectory(prefix="news-backup-", dir=LOG_DIR) as temp_dir:
        backup_dir = Path(temp_dir)
        existed = {path: path.exists() for path in paths}
        for path in paths:
            if path.exists():
                shutil.copy2(path, backup_dir / path.name)
        try:
            yield
        except Exception:
            for path in paths:
                backup = backup_dir / path.name
                if existed[path] and backup.exists():
                    shutil.copy2(backup, path)
                elif not existed[path] and path.exists():
                    path.unlink()
            raise


@contextmanager
def update_lock():
    if LOCK_FILE.exists():
        age = datetime.now().timestamp() - LOCK_FILE.stat().st_mtime
        if age < 6 * 60 * 60:
            raise RuntimeError(f"Another update is running (lock age: {age / 60:.1f} minutes).")
        LOCK_FILE.unlink()
    try:
        descriptor = os.open(LOCK_FILE, os.O_CREAT | os.O_EXCL | os.O_WRONLY)
        with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
            handle.write(datetime.now(KST).isoformat())
        yield
    finally:
        LOCK_FILE.unlink(missing_ok=True)


def run_step(label: str, command: list[str], env: dict[str, str], log_handle) -> None:
    print(f"\n[{label}] {' '.join(command)}", file=log_handle, flush=True)
    result = subprocess.run(
        command,
        cwd=ROOT,
        env=env,
        stdout=log_handle,
        stderr=subprocess.STDOUT,
        text=True,
        timeout=3 * 60 * 60,
        check=False,
    )
    if result.returncode:
        raise RuntimeError(f"{label} failed with exit code {result.returncode}")


def validate_archive(target: date) -> Path:
    archive = ROOT / f"archive_{target.isoformat()}.html"
    if not archive.exists():
        raise RuntimeError(f"Expected archive was not created: {archive.name}")
    content = archive.read_text(encoding="utf-8", errors="ignore")
    count = content.count('class="news-card')
    if count < MIN_NEWS_CARDS:
        raise RuntimeError(f"Validation failed: {archive.name} has only {count} news cards")
    return archive


def run_pipeline(target: date, log_handle) -> Path:
    value = target.isoformat()
    python = sys.executable
    env = os.environ.copy()
    env.update({"TZ": "Asia/Seoul", "PYTHONIOENCODING": "utf-8"})

    with rollback_on_failure(target):
        first_pass_env = env.copy()
        first_pass_env["AI_SUMMARY_ENABLED"] = "0"
        run_step("Initial render", [python, "-u", "main.py", "--date", value], first_pass_env, log_handle)
        run_step(
            "Agent A",
            [python, "-u", "agent_a.py", "--date", value, "--selection-archive", f"archive_{value}.html"],
            env,
            log_handle,
        )
        run_step(
            "Agent B",
            [python, "-u", "agent_b.py", "--date", value, "--fallback-models", "none", "--retry-attempts", "2"],
            env,
            log_handle,
        )
        run_step("Final render", [python, "-u", "main.py", "--date", value], env, log_handle)
        return validate_archive(target)


def slack_url() -> str:
    return os.environ.get("SLACK_WEBHOOK_URL", "").strip().strip('"').strip("'")


def site_url() -> str:
    return os.environ.get("SITE_URL", "").strip().rstrip("/") + "/"


def send_slack(title: str, message: str, color: str = "#2EB67D") -> None:
    webhook = slack_url()
    if not webhook:
        raise RuntimeError("SLACK_WEBHOOK_URL is not configured")
    payload = {
        "text": f"{title}: {message}",
        "attachments": [{"color": color, "blocks": [{"type": "section", "text": {"type": "mrkdwn", "text": f"*{title}*\n{message}"}}]}],
    }
    request = urllib.request.Request(
        webhook,
        data=json.dumps(payload, ensure_ascii=False).encode("utf-8"),
        headers={"Content-Type": "application/json; charset=utf-8"},
        method="POST",
    )
    with urllib.request.urlopen(request, timeout=20) as response:
        body = response.read().decode("utf-8", errors="replace").strip()
        if response.status != 200 or body.lower() != "ok":
            raise RuntimeError(f"Slack returned HTTP {response.status}: {body[:200]}")


def success_message(dates: list[date]) -> str:
    first, last = dates[0].isoformat(), dates[-1].isoformat()
    label = first if first == last else f"{first} ~ {last}"
    base = site_url()
    page = f"{base}archive_{last}.html" if base else ""
    link = f"\n<{page}|브리핑 바로 보기>" if page else ""
    return f"업데이트 날짜: `{label}`{link}"


def main() -> int:
    args = parse_args()
    LOG_DIR.mkdir(parents=True, exist_ok=True)
    load_env_file(ROOT / ".env")

    if args.notify_test:
        send_slack("뉴스 브리핑 연결 테스트", "PythonAnywhere 자동화의 Slack 연결이 정상입니다.")
        print("Slack test sent successfully.")
        return 0

    try:
        dates = pending_dates(args.date)
        if args.dry_run:
            print("Pending dates:", ", ".join(value.isoformat() for value in dates) or "none")
            return 0
        if not dates:
            print("No pending dates.")
            return 0

        stamp = datetime.now(KST).strftime("%Y-%m-%d_%H%M%S")
        log_path = LOG_DIR / f"pythonanywhere_update_{stamp}.log"
        with update_lock(), log_path.open("a", encoding="utf-8") as log_handle:
            for target in dates:
                print(f"Starting {target.isoformat()}", file=log_handle, flush=True)
                run_pipeline(target, log_handle)
        send_slack("✅ 뉴스 브리핑 업데이트 완료", success_message(dates))
        print(f"Update completed. Log: {log_path}")
        return 0
    except Exception as exc:
        error_message = f"`{type(exc).__name__}: {exc}`"
        try:
            send_slack("❌ 뉴스 브리핑 업데이트 실패", error_message, "#E01E5A")
        except Exception as slack_exc:
            print(f"Slack failure notification also failed: {slack_exc}", file=sys.stderr)
        print(error_message, file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
