#!/usr/bin/env python3
"""Send deployment status from CircleCI to Slack."""

from __future__ import annotations

import argparse
import json
import os
import re
import urllib.request
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path

from bs4 import BeautifulSoup


ROOT = Path(__file__).resolve().parents[1]
KST = timezone(timedelta(hours=9))
SLACK_SECTION_LIMIT = 2900
SECTION_EMOJIS = {
    "주요 지표": "📊",
    "임팩트": "🌱",
    "VC/AC/대체투자": "🚀",
    "AI": "🤖",
    "거시경제": "🌐",
    "산업트랜드": "🏭",
    "MBB 인사이트": "🧠",
    "강세 테마": "🔥",
}


@dataclass
class Article:
    title: str
    link: str
    source: str = ""


@dataclass
class ArticleSection:
    name: str
    articles: list[Article]


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser()
    parser.add_argument("status", choices=("success", "failure", "test"))
    return parser.parse_args()


def load_result() -> dict:
    path = ROOT / "deploy_result.json"
    if not path.exists():
        return {}
    return json.loads(path.read_text(encoding="utf-8"))


def normalize_text(value: str) -> str:
    return re.sub(r"\s+", " ", value or "").strip()


def format_dot_date(value: str) -> str:
    value = (value or "").strip()
    if re.fullmatch(r"\d{4}-\d{2}-\d{2}", value):
        return value.replace("-", ".")
    return value


def today_dot() -> str:
    return datetime.now(KST).strftime("%Y.%m.%d")


def slack_escape(value: str) -> str:
    return (value or "").replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")


def slack_link(url: str, label: str) -> str:
    clean_url = (url or "").strip().replace(" ", "%20").replace(">", "%3E")
    clean_label = slack_escape(label).replace("|", "¦")
    if not clean_url:
        return clean_label
    return f"<{clean_url}|{clean_label}>"


def archive_path(latest_archive: str) -> Path | None:
    if latest_archive:
        name = f"archive_{latest_archive}.html"
        for candidate in (ROOT / name, ROOT / "public" / name):
            if candidate.exists():
                return candidate
        return None

    candidates = sorted(ROOT.glob("archive_*.html"))
    if not candidates:
        candidates = sorted((ROOT / "public").glob("archive_*.html"))
    return candidates[-1] if candidates else None


def section_name_map(soup: BeautifulSoup) -> dict[str, str]:
    names = {}
    for button in soup.select(".sidebar-tab[data-target]"):
        section_id = button.get("data-target", "").strip()
        label = normalize_text(button.get_text(" ", strip=True))
        if section_id and label:
            names[section_id] = label
    return names


def parse_source(meta_text: str) -> str:
    match = re.search(r"출처:\s*([^|]+)", meta_text or "")
    return normalize_text(match.group(1)) if match else ""


def extract_sections_from_archive(latest_archive: str) -> list[ArticleSection]:
    path = archive_path(latest_archive)
    if not path:
        return []

    soup = BeautifulSoup(path.read_text(encoding="utf-8", errors="ignore"), "html.parser")
    sidebar_names = section_name_map(soup)
    sections: list[ArticleSection] = []

    for section in soup.select("section.content-section"):
        section_id = section.get("id", "").strip()
        fallback_heading = section.find("h2")
        section_name = sidebar_names.get(section_id) or normalize_text(
            fallback_heading.get_text(" ", strip=True) if fallback_heading else section_id
        )
        if not section_name:
            continue

        articles = []
        for card in section.select("article.news-card"):
            title_link = card.select_one(".news-title a[href]") or card.select_one("a[href]")
            if not title_link:
                continue
            title = normalize_text(title_link.get_text(" ", strip=True))
            link = title_link.get("href", "").strip()
            if not title:
                continue
            meta = card.select_one(".news-date")
            source = parse_source(meta.get_text(" ", strip=True) if meta else "")
            articles.append(Article(title=title, link=link, source=source))

        if articles:
            sections.append(ArticleSection(name=section_name, articles=articles))

    return sections


def result_date_text(result: dict) -> str:
    dates = result.get("dates") or []
    if dates:
        if len(dates) == 1:
            return format_dot_date(dates[0])
        return f"{format_dot_date(dates[0])} ~ {format_dot_date(dates[-1])}"
    latest = result.get("latest_archive", "")
    return format_dot_date(latest) if latest else "변경 없음"


def footer_links(result: dict) -> str:
    site = os.environ.get("SITE_URL", "").strip().rstrip("/")
    build_url = os.environ.get("CIRCLE_BUILD_URL", "").strip()
    latest = result.get("latest_archive", "")
    links = []
    if site:
        page = f"{site}/archive_{latest}.html" if latest else site
        links.append(slack_link(page, "전체 브리핑 보기"))
    if build_url:
        links.append(slack_link(build_url, "실행 로그"))
    return " | ".join(links)


def section_heading(section_name: str) -> str:
    emoji = SECTION_EMOJIS.get(section_name, "🗞️")
    return f"{emoji} *{section_name}*"


def article_rich_text_section(article: Article) -> dict:
    elements = []
    clean_url = (article.link or "").strip().replace(" ", "%20").replace(">", "%3E")
    if clean_url:
        elements.append({"type": "link", "url": clean_url, "text": article.title})
    else:
        elements.append({"type": "text", "text": article.title})
    if article.source:
        elements.append({"type": "text", "text": f" ({article.source})"})
    return {"type": "rich_text_section", "elements": elements}


def article_list_block(articles: list[Article]) -> dict:
    return {
        "type": "rich_text",
        "elements": [
            {
                "type": "rich_text_list",
                "style": "bullet",
                "indent": 0,
                "border": 0,
                "elements": [article_rich_text_section(article) for article in articles],
            }
        ],
    }


def build_daily_briefing_message(result: dict) -> str:
    article_sections = extract_sections_from_archive(result.get("latest_archive", ""))
    lines = [
        f"*오늘의 날짜: {today_dot()}*",
        f"*업데이트된 기사 발행 날짜: {result_date_text(result)}*",
    ]

    if article_sections:
        for section in article_sections:
            lines.extend(["", section_heading(section.name)])
            for article in section.articles:
                source = f" ({slack_escape(article.source)})" if article.source else ""
                lines.append(f"• {slack_link(article.link, article.title)}{source}")
    else:
        lines.extend(["", "수집된 기사 목록을 찾지 못했습니다."])

    links = footer_links(result)
    if links:
        lines.extend(["", links])
    return "\n".join(lines).strip()


def build_daily_briefing_blocks(result: dict) -> list[dict]:
    article_sections = extract_sections_from_archive(result.get("latest_archive", ""))
    blocks = section_blocks(
        "\n".join(
            [
                f"*오늘의 날짜: {today_dot()}*",
                f"*업데이트된 기사 발행 날짜: {result_date_text(result)}*",
            ]
        )
    )

    if article_sections:
        for section in article_sections:
            blocks.extend(section_blocks(section_heading(section.name)))
            blocks.append(article_list_block(section.articles))
    else:
        blocks.extend(section_blocks("수집된 기사 목록을 찾지 못했습니다."))

    links = footer_links(result)
    if links:
        blocks.extend(section_blocks(links))
    return blocks


def legacy_success_message(result: dict) -> str:
    links = footer_links(result)
    target = result_date_text(result)
    return f"업데이트: `{target}`\n{links}".strip()


def build_message(status: str, result: dict) -> tuple[str, str, str]:
    if status == "success":
        message = build_daily_briefing_message(result) or legacy_success_message(result)
        return "✅ 뉴스 브리핑 배포 완료", message, "#2EB67D"
    if status == "test":
        return "✅ Slack 연결 테스트", "CircleCI 자동화의 Slack 연결이 정상입니다.", "#36C5F0"

    error = result.get("error") or "CircleCI 작업이 실패했습니다."
    links = footer_links(result)
    return "❌ 뉴스 브리핑 배포 실패", f"`{error}`\n{links}".strip(), "#E01E5A"


def section_blocks(text: str) -> list[dict]:
    blocks = []
    current = ""
    for line in text.splitlines():
        candidate = f"{current}\n{line}".strip() if current else line
        if len(candidate) > SLACK_SECTION_LIMIT and current:
            blocks.append({"type": "section", "text": {"type": "mrkdwn", "text": current}})
            current = line
        else:
            current = candidate
    if current:
        blocks.append({"type": "section", "text": {"type": "mrkdwn", "text": current}})
    return blocks


def send(status: str) -> None:
    webhook = os.environ.get("SLACK_WEBHOOK_URL", "").strip().strip('"').strip("'")
    if not webhook:
        raise RuntimeError("SLACK_WEBHOOK_URL is not configured")
    result = load_result()
    title, message, color = build_message(status, result)
    blocks = [{"type": "section", "text": {"type": "mrkdwn", "text": f"*{title}*"}}]
    if status == "success":
        blocks.extend(build_daily_briefing_blocks(result))
    else:
        blocks.extend(section_blocks(message))
    payload = {
        "text": title,
        "blocks": blocks,
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
