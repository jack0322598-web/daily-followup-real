#!/usr/bin/env python3
"""Send deployment status from CircleCI to Slack."""

from __future__ import annotations

import argparse
import json
import os
import urllib.request
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser()
    parser.add_argument("status", choices=("success", "failure", "test"))
    return parser.parse_args()


def load_result() -> dict:
    path = ROOT / "deploy_result.json"
    if not path.exists():
        return {}
    return json.loads(path.read_text(encoding="utf-8"))


def build_message(status: str, result: dict) -> tuple[str, str, str]:
    site = os.environ.get("SITE_URL", "").strip().rstrip("/")
    build_url = os.environ.get("CIRCLE_BUILD_URL", "").strip()
    latest = result.get("latest_archive", "")
    links = []
    if site:
        page = f"{site}/archive_{latest}.html" if latest else site
        links.append(f"<{page}|브리핑 보기>")
    if build_url:
        links.append(f"<{build_url}|실행 로그>")
    link_text = " | ".join(links)

    if status == "success":
        dates = result.get("dates") or []
        target = f"`{dates[0]}`" if len(dates) == 1 else (f"`{dates[0]} ~ {dates[-1]}`" if dates else "변경 없음")
        return "✅ 뉴스 브리핑 배포 완료", f"업데이트: {target}\n{link_text}".strip(), "#2EB67D"
    if status == "test":
        return "✅ Slack 연결 테스트", "CircleCI 자동화의 Slack 연결이 정상입니다.", "#36C5F0"
    error = result.get("error") or "CircleCI 작업이 실패했습니다."
    return "❌ 뉴스 브리핑 배포 실패", f"`{error}`\n{link_text}".strip(), "#E01E5A"


def send(status: str) -> None:
    webhook = os.environ.get("SLACK_WEBHOOK_URL", "").strip().strip('"').strip("'")
    if not webhook:
        raise RuntimeError("SLACK_WEBHOOK_URL is not configured")
    title, message, color = build_message(status, load_result())
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


def main() -> int:
    args = parse_args()
    send(args.status)
    print(f"Slack {args.status} notification sent.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
