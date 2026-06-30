#!/usr/bin/env python3
"""Run the daily briefing pipeline safely in a scheduled CI job."""

from __future__ import annotations

import argparse
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
from contextlib import contextmanager
from datetime import date, datetime, timedelta, timezone
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
KST = timezone(timedelta(hours=9))
LOCK_FILE = ROOT / ".update.lock"
LOG_DIR = ROOT / "logs"
RESULT_FILE = ROOT / "deploy_result.json"
STATE_FILE = ROOT / "pipeline_state.json"
SUMMARY_STATE_FILE = ROOT / "pipeline_summarize_state.json"
ARCHIVE_PATTERN = re.compile(r"archive_(\d{4}-\d{2}-\d{2})\.html$")
MIN_NEWS_CARDS = 3
STAGES = ("all", "collect", "summarize", "render")


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Generate the daily news briefing.")
    parser.add_argument("--date", help="Generate one date only (YYYY-MM-DD).")
    parser.add_argument(
        "--stage",
        choices=STAGES,
        default="all",
        help="Run the complete pipeline or one CI stage.",
    )
    parser.add_argument("--dry-run", action="store_true", help="Show pending dates without running the pipeline.")
    return parser.parse_args()


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
            f"{len(dates)} dates are pending, exceeding MAX_BACKFILL_DAYS={max_days}. "
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


def stage_log_path(stage: str) -> Path:
    stamp = datetime.now(KST).strftime("%Y-%m-%d_%H%M%S")
    return LOG_DIR / f"scheduled_update_{stage}_{stamp}.log"


def log_message(message: str, log_handle) -> None:
    print(message, flush=True)
    print(message, file=log_handle, flush=True)


def run_step(label: str, command: list[str], env: dict[str, str], log_handle) -> None:
    started = datetime.now(KST)
    log_message(f"\n[{label}] {' '.join(command)}", log_handle)
    result = subprocess.run(
        command,
        cwd=ROOT,
        env=env,
        text=True,
        timeout=3 * 60 * 60,
        check=False,
    )
    duration = (datetime.now(KST) - started).total_seconds()
    log_message(f"[{label}] finished with exit code {result.returncode} in {duration:.1f}s", log_handle)
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


def command_environment() -> dict[str, str]:
    env = os.environ.copy()
    env.update({"TZ": "Asia/Seoul", "PYTHONIOENCODING": "utf-8", "PYTHONUNBUFFERED": "1"})
    return env


def run_collect(target: date, log_handle) -> None:
    value = target.isoformat()
    python = sys.executable
    env = command_environment()

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


def run_summarize(target: date, log_handle) -> None:
    value = target.isoformat()
    run_step(
        "Agent B",
        [
            sys.executable,
            "-u",
            "agent_b.py",
            "--date",
            value,
            "--fallback-models",
            "none",
            "--retry-attempts",
            "2",
        ],
        command_environment(),
        log_handle,
    )


def run_render(target: date, log_handle) -> Path:
    value = target.isoformat()
    with rollback_on_failure(target):
        run_step(
            "Final render",
            [sys.executable, "-u", "main.py", "--date", value],
            command_environment(),
            log_handle,
        )
        return validate_archive(target)


def run_pipeline(target: date, log_handle) -> Path:
    run_collect(target, log_handle)
    run_summarize(target, log_handle)
    return run_render(target, log_handle)


def write_result(status: str, dates: list[date], error: str = "") -> None:
    archives = archive_dates()
    payload = {
        "status": status,
        "dates": [value.isoformat() for value in dates],
        "latest_archive": archives[-1].isoformat() if archives else "",
        "generated_at": datetime.now(KST).isoformat(),
        "error": error,
    }
    RESULT_FILE.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")


def write_state(
    status: str,
    target: date | None = None,
    error: str = "",
    path: Path | None = None,
) -> None:
    payload = {
        "status": status,
        "target_date": target.isoformat() if target else "",
        "updated_at": datetime.now(KST).isoformat(),
        "error": error,
    }
    (path or STATE_FILE).write_text(
        json.dumps(payload, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )


def load_state(path: Path | None = None) -> dict:
    state_file = path or STATE_FILE
    if not state_file.exists():
        raise RuntimeError(f"Pipeline state is missing: {state_file.name}")
    return json.loads(state_file.read_text(encoding="utf-8"))


def state_target(
    expected_status: str,
    requested: str | None = None,
    path: Path | None = None,
) -> date | None:
    state = load_state(path)
    if state.get("status") == "no_changes":
        return None
    if state.get("status") != expected_status:
        raise RuntimeError(
            f"Expected pipeline state {expected_status!r}, got {state.get('status')!r}"
        )
    target = parse_iso_date(state.get("target_date", ""))
    if requested and target != parse_iso_date(requested):
        raise RuntimeError(f"Pipeline state date {target} does not match requested date {requested}")
    return target


def next_target(requested: str | None = None) -> tuple[date | None, int]:
    dates = pending_dates(requested)
    if not dates:
        return None, 0
    return dates[0], len(dates)


def run_stage(stage: str, requested: str | None = None) -> None:
    if stage == "collect":
        target, pending_count = next_target(requested)
        if target is None:
            write_state("no_changes")
            print("No pending dates; downstream stages will skip generation.")
            return
        log_path = stage_log_path(stage)
        with update_lock(), log_path.open("a", encoding="utf-8") as log_handle:
            log_message(
                f"Starting collect stage for {target.isoformat()} (pending dates: {pending_count})",
                log_handle,
            )
            run_collect(target, log_handle)
        write_state("collected", target)
        print(f"Collect stage completed. Log: {log_path}")
        return

    if stage == "summarize":
        target = state_target("collected", requested)
        if target is None:
            (ROOT / "pipeline_data" / "agent_b").mkdir(parents=True, exist_ok=True)
            write_state("no_changes", path=SUMMARY_STATE_FILE)
            print("No pending dates; summarize stage skipped.")
            return
        log_path = stage_log_path(stage)
        with update_lock(), log_path.open("a", encoding="utf-8") as log_handle:
            log_message(f"Starting summarize stage for {target.isoformat()}", log_handle)
            run_summarize(target, log_handle)
        write_state("summarized", target, path=SUMMARY_STATE_FILE)
        print(f"Summarize stage completed. Log: {log_path}")
        return

    if stage == "render":
        target = state_target("summarized", requested, path=SUMMARY_STATE_FILE)
        if target is None:
            archives = archive_dates()
            if not archives:
                write_result("no_changes", [])
                print("No pending dates or existing archives; render stage skipped.")
                return
            target = archives[-1]
            log_path = stage_log_path(stage)
            with update_lock(), log_path.open("a", encoding="utf-8") as log_handle:
                log_message(
                    f"No pending dates; re-rendering {target.isoformat()} with the current site code.",
                    log_handle,
                )
                run_render(target, log_handle)
            write_state("rendered", target)
            write_result("no_changes", [])
            print(f"Render stage refreshed the existing site. Log: {log_path}")
            return
        log_path = stage_log_path(stage)
        with update_lock(), log_path.open("a", encoding="utf-8") as log_handle:
            log_message(f"Starting render stage for {target.isoformat()}", log_handle)
            run_render(target, log_handle)
        write_state("rendered", target)
        write_result("updated", [target])
        print(f"Render stage completed. Log: {log_path}")
        return

    target, pending_count = next_target(requested)
    if target is None:
        write_result("no_changes", [])
        print("No pending dates; the existing site will still be deployed.")
        return
    log_path = stage_log_path(stage)
    with update_lock(), log_path.open("a", encoding="utf-8") as log_handle:
        log_message(
            f"Starting complete pipeline for {target.isoformat()} (pending dates: {pending_count})",
            log_handle,
        )
        run_pipeline(target, log_handle)
    write_result("updated", [target])
    print(f"Update completed. Log: {log_path}")


def main() -> int:
    args = parse_args()
    LOG_DIR.mkdir(parents=True, exist_ok=True)
    result_dates: list[date] = []
    try:
        if args.dry_run:
            dates = pending_dates(args.date)
            print("Pending dates:", ", ".join(value.isoformat() for value in dates) or "none")
            return 0
        if args.date:
            result_dates = [parse_iso_date(args.date)]
        elif args.stage in {"summarize", "render"} and STATE_FILE.exists():
            state_file = SUMMARY_STATE_FILE if args.stage == "render" and SUMMARY_STATE_FILE.exists() else STATE_FILE
            target_text = load_state(state_file).get("target_date", "")
            if target_text:
                result_dates = [parse_iso_date(target_text)]
        run_stage(args.stage, args.date)
        return 0
    except Exception as exc:
        error = f"{type(exc).__name__}: {exc}"
        write_result("failed", result_dates, error)
        try:
            write_state("failed", result_dates[0] if result_dates else None, error)
        except Exception:
            pass
        print(error, file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
