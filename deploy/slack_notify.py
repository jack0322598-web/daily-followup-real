#!/usr/bin/env python3
"""Send deployment status from CircleCI to Slack."""

from __future__ import annotations

import argparse
import html
import json
import os
import re
import urllib.parse
import urllib.request
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from email.utils import parsedate_to_datetime
from pathlib import Path
from xml.etree import ElementTree

from bs4 import BeautifulSoup


ROOT = Path(__file__).resolve().parents[1]
KST = timezone(timedelta(hours=9))
SLACK_SECTION_LIMIT = 2900
KB_DAILY_FX_REPORT_URL = "https://obank.kbstar.com/quics?page=C101426"
GOOGLE_NEWS_RSS_URL = "https://news.google.com/rss/search"
MARKET_FACTOR_TEXT_CACHE: dict[str, str] = {}
SECTION_EMOJIS = {
    "주요 지표": "📊",
    "임팩트": "🌱",
    "VC/AC/PEF": "🚀",
    "AI": "🤖",
    "거시경제": "🌐",
    "산업트랜드": "🏭",
    "MBB 인사이트": "🧠",
    "강세 테마": "🔥",
}
SECTION_DISPLAY_NAMES = {
    "VC/AC/대체투자": "VC/AC/PEF",
}
SECTION_EXCLUDED_SOURCES = {
    "VC/AC/대체투자": {"플래텀", "벤처스퀘어"},
    "VC/AC/PEF": {"플래텀", "벤처스퀘어"},
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


def strip_tags(value: str) -> str:
    return normalize_text(html.unescape(re.sub(r"<[^>]+>", " ", value or "")))


def format_dot_date(value: str) -> str:
    value = (value or "").strip()
    if re.fullmatch(r"\d{4}-\d{2}-\d{2}", value):
        return value.replace("-", ".")
    return value


def today_dot() -> str:
    return datetime.now(KST).strftime("%Y.%m.%d")


def parse_iso_date(value: str) -> datetime | None:
    try:
        return datetime.strptime((value or "").strip(), "%Y-%m-%d")
    except ValueError:
        return None


def result_target_date(result: dict) -> datetime | None:
    dates = result.get("dates") or []
    if dates:
        return parse_iso_date(dates[-1])
    return parse_iso_date(result.get("latest_archive", ""))


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


def display_section_name(section_name: str) -> str:
    return SECTION_DISPLAY_NAMES.get(section_name, section_name)


def should_include_article(section_name: str, article: Article) -> bool:
    excluded_sources = SECTION_EXCLUDED_SOURCES.get(section_name, set())
    return article.source not in excluded_sources


def prepare_sections_for_slack(sections: list[ArticleSection]) -> list[ArticleSection]:
    prepared = []
    for section in sections:
        articles = [article for article in section.articles if should_include_article(section.name, article)]
        if articles:
            prepared.append(ArticleSection(name=display_section_name(section.name), articles=articles))
    return prepared


def fetch_url_text(url: str, timeout: int = 12) -> str:
    request = urllib.request.Request(
        url,
        headers={
            "User-Agent": "Mozilla/5.0",
            "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
        },
    )
    with urllib.request.urlopen(request, timeout=timeout) as response:
        body = response.read()
    return body.decode("utf-8", errors="replace")


def parse_chart_data(soup: BeautifulSoup) -> dict:
    match = re.search(r"const\s+chartData\s*=\s*(\{.*?\});", str(soup), flags=re.DOTALL)
    if not match:
        return {}
    try:
        return json.loads(match.group(1))
    except Exception:
        return {}


def parent_indicator_card(node) -> BeautifulSoup | None:
    current = node
    while current:
        if getattr(current, "name", "") == "article" and "indicator-card" in current.get("class", []):
            return current
        current = current.parent
    return None


def extract_indicator_metric(soup: BeautifulSoup, chart_id: str, chart_key: str) -> dict:
    chart_node = soup.find(id=chart_id)
    card = parent_indicator_card(chart_node) if chart_node else None
    label = normalize_text(card.select_one(".metric-label").get_text(" ", strip=True)) if card and card.select_one(".metric-label") else ""
    value = normalize_text(card.select_one(".metric-value").get_text(" ", strip=True)) if card and card.select_one(".metric-value") else ""
    detail = normalize_text(card.select_one(".metric-detail").get_text(" ", strip=True)) if card and card.select_one(".metric-detail") else ""
    return {"label": label, "value": value, "detail": detail, "chart_key": chart_key}


def parse_number(value: str) -> float | None:
    match = re.search(r"[-+]?\d[\d,]*(?:\.\d+)?", value or "")
    if not match:
        return None
    try:
        return float(match.group(0).replace(",", ""))
    except ValueError:
        return None


def enrich_metric_with_chart(metric: dict, chart_data: dict) -> dict:
    chart = chart_data.get(metric.get("chart_key", ""), {}) if isinstance(chart_data, dict) else {}
    values = chart.get("values") or []
    if len(values) >= 1 and not metric.get("value"):
        metric["value"] = f"{float(values[-1]):,.2f}"
    if len(values) >= 2:
        try:
            previous = float(values[-2])
            current = float(values[-1])
            change = current - previous
            pct = (change / previous * 100) if previous else 0.0
            metric["change"] = change
            metric["change_pct"] = pct
            metric["direction"] = "상승" if change > 0 else "하락" if change < 0 else "보합"
        except Exception:
            pass
    return metric


def market_metric_suffix(metric: dict) -> str:
    value = metric.get("value", "")
    value = re.sub(r"^종가:\s*", "", value)
    pct = metric.get("change_pct")
    if value and pct is not None:
        return f"({value}, {pct:+.2f}%)"
    return f"({value})" if value else ""


def google_news_items(query: str, target_date: datetime | None = None, max_items: int = 8) -> list[str]:
    params = {
        "q": query,
        "hl": "ko",
        "gl": "KR",
        "ceid": "KR:ko",
    }
    url = f"{GOOGLE_NEWS_RSS_URL}?{urllib.parse.urlencode(params)}"
    try:
        root = ElementTree.fromstring(fetch_url_text(url, timeout=15).lstrip("\ufeff"))
    except Exception:
        return []
    items = []
    target_dot = target_date.strftime("%Y.%m.%d") if target_date else ""
    for item in root.findall(".//item"):
        if len(items) >= max_items:
            break
        title = normalize_text(item.findtext("title", ""))
        description = strip_tags(item.findtext("description", ""))
        pub_text = item.findtext("pubDate", "")
        if target_dot and pub_text:
            try:
                pub_dt = parsedate_to_datetime(pub_text).astimezone(KST)
                if pub_dt.strftime("%Y.%m.%d") not in {target_dot, (target_date + timedelta(days=1)).strftime("%Y.%m.%d")}:
                    continue
            except Exception:
                pass
        text = normalize_text(f"{title}. {description}")
        if text:
            items.append(text)
    return items


def split_reason_sentences(text: str) -> list[str]:
    text = normalize_text(re.sub(r"\s+-\s+[^-.]{2,30}$", "", text or ""))
    parts = re.split(r"(?<=[.!?])\s+|(?<=다)\s+", text)
    return [normalize_text(part.strip(" .")) for part in parts if len(normalize_text(part)) >= 18]


def select_reason_sentence(texts: list[str], required_terms: tuple[str, ...], reason_terms: tuple[str, ...]) -> str:
    scored = []
    for text in texts:
        for sentence in split_reason_sentences(text):
            if required_terms and not any(term in sentence for term in required_terms):
                continue
            score = 0
            for term in reason_terms:
                if term in sentence:
                    score += 8
            if re.search(r"\d|%|원|포인트|bp", sentence):
                score += 4
            if 45 <= len(sentence) <= 165:
                score += 4
            scored.append((score, sentence))
    if not scored:
        return ""
    scored.sort(key=lambda item: item[0], reverse=True)
    return truncate_reason(scored[0][1])


def truncate_reason(text: str, limit: int = 175) -> str:
    text = normalize_text(text)
    if len(text) <= limit:
        return text
    return text[: limit - 1].rstrip(" ,.;") + "…"


def fetch_kospi_factor_summary(target_date: datetime | None, metric: dict) -> str:
    close_value = parse_number(metric.get("value", ""))
    close_hint = f"{int(round(close_value)):,}" if close_value else ""
    date_query = ""
    if target_date:
        start = target_date.strftime("%Y-%m-%d")
        end = (target_date + timedelta(days=1)).strftime("%Y-%m-%d")
        date_query = f" after:{start} before:{end}"
    queries = [
        f"코스피 {close_hint} 연구원{date_query}".strip(),
        f"코스피 마감 연구원 상승 하락{date_query}".strip(),
    ]
    texts = []
    for query in queries:
        texts.extend(google_news_items(query, target_date=target_date))
        if texts:
            break
    reason = select_reason_sentence(
        texts,
        ("코스피",),
        ("연구원", "외국인", "기관", "개인", "매수", "매도", "반도체", "삼성전자", "SK하이닉스", "미국", "금리", "환율", "상승", "하락"),
    )
    if reason:
        return reason
    direction = metric.get("direction") or "변동"
    flow = metric.get("detail", "")
    if flow:
        return f"코스피는 {direction} 마감했으며, 수급상 {flow} 흐름을 함께 점검할 필요가 있습니다."
    return f"코스피는 {direction} 마감했으며, 대형주 수급과 대외 변수 영향을 함께 확인할 필요가 있습니다."


def kb_report_interpretation(title: str) -> str:
    if any(term in title for term in ("당국", "개입", "게이지")):
        return "외환당국 경계감이 상단을 제한하는 요인으로 언급됐습니다."
    if any(term in title for term in ("긴축", "FOMC", "연준", "인하")):
        return "미 연준의 통화정책 기대 변화가 달러/원 방향성을 좌우하는 변수로 지목됐습니다."
    if any(term in title for term in ("유가", "이란", "호르무즈", "중동")):
        return "중동 리스크와 유가 흐름이 위험선호와 달러 수요를 흔드는 요인으로 정리됐습니다."
    if any(term in title for term in ("기초가치", "눈높이", "환율")):
        return "전일 환율 레벨과 기초여건 간 괴리가 시장의 되돌림 여부를 가르는 쟁점으로 제시됐습니다."
    return ""


def fetch_kb_fx_report(target_date: datetime | None) -> dict:
    if not target_date:
        return {}
    try:
        page_text = normalize_text(BeautifulSoup(fetch_url_text(KB_DAILY_FX_REPORT_URL, timeout=15), "html.parser").get_text(" ", strip=True))
    except Exception:
        return {}
    dates = [target_date + timedelta(days=1), target_date]
    rows = [
        {
            "range": normalize_text(match.group(1)),
            "title": normalize_text(match.group(2)),
            "date": normalize_text(match.group(3)),
        }
        for match in re.finditer(
            r"\[금일 달러/원 환율 ([^\]]+)\]\|([^[]+?)\s+(\d{4}\.\d{2}\.\d{2})\s+\d+\s+\d+",
            page_text,
        )
    ]
    for report_date in dates:
        dot = report_date.strftime("%Y.%m.%d")
        for row in rows:
            if row["date"] == dot:
                return row
    return {}


def fetch_fx_factor_summary(target_date: datetime | None, metric: dict) -> str:
    report = fetch_kb_fx_report(target_date)
    close_value = parse_number(metric.get("value", ""))
    close_hint = f"{int(round(close_value)):,}" if close_value else ""
    date_query = ""
    if target_date:
        start = target_date.strftime("%Y-%m-%d")
        end = (target_date + timedelta(days=2)).strftime("%Y-%m-%d")
        date_query = f" after:{start} before:{end}"
    texts = google_news_items(f"달러 원 환율 {close_hint} 상승 하락{date_query}".strip(), target_date=target_date)
    reason = select_reason_sentence(
        texts,
        ("환율",),
        ("연준", "FOMC", "달러", "위안", "유가", "이란", "호르무즈", "당국", "개입", "수출", "네고", "위험선호", "상승", "하락"),
    )
    kb_reason = kb_report_interpretation(report.get("title", ""))
    if reason and kb_reason:
        return f"{reason} KB 리포트는 '{report['title']}'로 {kb_reason}"
    if reason:
        return reason
    if report:
        range_text = f"달러/원 {report['range']}" if report.get("range") else "달러/원 환율"
        tail = kb_reason or "전일 레벨과 대외 변수에 따른 변동성 점검을 제시했습니다."
        return f"KB 일간환율동향리포트는 '{report['title']}' 제목으로 {range_text}을 전망하며, {tail}"
    direction = metric.get("direction") or "변동"
    return f"원/달러 환율은 {direction} 흐름을 보였으며, 달러 강세와 위험선호, 외환당국 경계감을 함께 확인할 필요가 있습니다."


def extract_market_metrics(latest_archive: str) -> tuple[dict, dict]:
    path = archive_path(latest_archive)
    if not path:
        return {}, {}
    soup = BeautifulSoup(path.read_text(encoding="utf-8", errors="ignore"), "html.parser")
    chart_data = parse_chart_data(soup)
    kospi = enrich_metric_with_chart(extract_indicator_metric(soup, "chart-kospi", "kospi"), chart_data)
    fx = enrich_metric_with_chart(extract_indicator_metric(soup, "chart-fx", "fx"), chart_data)
    return kospi, fx


def build_market_factor_lines(result: dict) -> list[str]:
    target_date = result_target_date(result)
    kospi, fx = extract_market_metrics(result.get("latest_archive", ""))
    if not kospi and not fx:
        return []

    lines = ["*[주요 지표]*"]
    if kospi:
        kospi_suffix = market_metric_suffix(kospi)
        kospi_reason = fetch_kospi_factor_summary(target_date, kospi)
        lines.extend(["📈 *코스피*", f"• {slack_escape(kospi_reason)} {slack_escape(kospi_suffix)}".rstrip()])
    if fx:
        fx_suffix = market_metric_suffix(fx)
        fx_reason = fetch_fx_factor_summary(target_date, fx)
        lines.extend(["💱 *환율*", f"• {slack_escape(fx_reason)} {slack_escape(fx_suffix)}".rstrip()])
    return lines


def build_market_factor_text(result: dict) -> str:
    cache_key = json.dumps(
        {"latest_archive": result.get("latest_archive", ""), "dates": result.get("dates") or []},
        ensure_ascii=False,
        sort_keys=True,
    )
    if cache_key not in MARKET_FACTOR_TEXT_CACHE:
        MARKET_FACTOR_TEXT_CACHE[cache_key] = "\n".join(build_market_factor_lines(result))
    return MARKET_FACTOR_TEXT_CACHE[cache_key]


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
    article_sections = prepare_sections_for_slack(extract_sections_from_archive(result.get("latest_archive", "")))
    lines = [
        f"*오늘의 날짜: {today_dot()}*",
        f"*업데이트된 기사 발행 날짜: {result_date_text(result)}*",
    ]
    market_text = build_market_factor_text(result)
    if market_text:
        lines.extend(["", market_text])

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
    article_sections = prepare_sections_for_slack(extract_sections_from_archive(result.get("latest_archive", "")))
    blocks = section_blocks(
        "\n".join(
            [
                f"*오늘의 날짜: {today_dot()}*",
                f"*업데이트된 기사 발행 날짜: {result_date_text(result)}*",
            ]
        )
    )
    market_text = build_market_factor_text(result)
    if market_text:
        blocks.extend(section_blocks(market_text))

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
