import argparse
import html
import re
import time
import urllib.parse
import urllib.request
from datetime import datetime, timedelta, timezone
from email.utils import parsedate_to_datetime
from html.parser import HTMLParser
from pathlib import Path
from urllib.parse import urlparse
from xml.etree import ElementTree


KST = timezone(timedelta(hours=9))
BASE_DIR = Path(__file__).resolve().parent
OUTPUT_FILE = BASE_DIR / "index.html"

HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/120.0.0.0 Safari/537.36"
    )
}

MAX_IMPACT_NEWS = 5
MAX_NEWS_PER_CATEGORY = 3
SUMMARY_LINE_COUNT = 3
SUMMARY_MAX_CHARS = 110

SUMMARY_SKIP_KEYWORDS = (
    "무단전재",
    "재배포",
    "저작권",
    "copyright",
    "구독",
    "광고",
    "관련기사",
    "뉴스레터",
    "로그인",
    "회원가입",
    "Internet Explorer",
    "최신 브라우저",
    "Browser) 사용",
)

BLOCKED_SOURCE_KEYWORDS = (
    "블로그",
    "blog",
    "카페",
    "cafe",
    "티스토리",
    "tistory",
    "브런치",
    "brunch",
    "medium",
    "substack",
    "wordpress",
    "velog",
    "reddit",
    "quora",
    "youtube",
    "youtu.be",
    "facebook",
    "instagram",
    "x.com",
    "twitter",
    "threads",
    "linkedin",
    "네이버뉴스",
    "다음뉴스",
)

BLOCKED_SOURCE_DOMAINS = (
    "blog.naver.com",
    "m.blog.naver.com",
    "post.naver.com",
    "cafe.naver.com",
    "tistory.com",
    "brunch.co.kr",
    "medium.com",
    "substack.com",
    "wordpress.com",
    "velog.io",
    "youtube.com",
    "youtu.be",
    "news.naver.com",
    "n.news.naver.com",
    "m.news.naver.com",
    "news.daum.net",
    "v.daum.net",
)

SEARCH_SECTIONS = [
    {
        "id": "macro",
        "label": "거시경제",
        "groups": [
            {
                "title": "미국",
                "categories": [
                    {
                        "name": "경제지표",
                        "query": "미국 경제지표 OR 미국 GDP OR 미국 CPI OR 미국 PCE OR 미국 고용 OR 미국 물가",
                        "context": "미국 경기, 물가, 고용 흐름을 판단하는 데 연결되는 기사입니다.",
                    },
                    {
                        "name": "관세",
                        "query": "미국 관세 OR 트럼프 관세 OR 미국 무역대표부 OR USTR OR tariff",
                        "context": "미국 무역정책과 관세 리스크의 변화를 다루는 기사입니다.",
                    },
                    {
                        "name": "통화정책",
                        "query": "미국 통화정책 OR 연준 OR FOMC OR 미국 금리 OR 파월",
                        "context": "연준 금리 경로와 채권시장 흐름에 영향을 줄 수 있는 기사입니다.",
                    },
                    {
                        "name": "외교",
                        "query": "미국 외교 OR 미국 제재 OR 미중 관계 OR 미국 중국 OR 미국 러시아",
                        "context": "미국 대외관계, 제재, 협상 흐름을 확인할 수 있는 기사입니다.",
                    },
                ],
            },
            {
                "title": "한국",
                "categories": [
                    {
                        "name": "경제지표",
                        "query": "한국 경제지표 OR 한국 GDP OR 한국 소비자물가 OR 한국 고용 OR 한국 수출입 OR 한국 산업생산",
                        "context": "한국 경기, 물가, 고용, 수출입 흐름을 살피는 기사입니다.",
                    },
                    {
                        "name": "통화정책",
                        "query": "한국은행 OR 금통위 OR 기준금리 OR 한국 통화정책 OR 이창용",
                        "context": "한국은행 기준금리와 금융시장 방향성을 확인하는 기사입니다.",
                    },
                ],
            },
            {
                "title": "유럽",
                "categories": [
                    {
                        "name": "통화정책",
                        "query": "유럽중앙은행 OR ECB OR 유로존 금리 OR 유럽 통화정책 OR 라가르드",
                        "context": "ECB 정책과 유로존 금리 흐름을 확인하는 기사입니다.",
                    },
                ],
            },
            {
                "title": "중국",
                "categories": [
                    {
                        "name": "통화정책",
                        "query": "중국 인민은행 OR PBOC OR 중국 LPR OR 중국 지준율 OR 중국 통화정책",
                        "context": "중국 인민은행 정책과 유동성 흐름을 살피는 기사입니다.",
                    },
                ],
            },
        ],
    },
    {
        "id": "ai",
        "label": "AI",
        "groups": [
            {
                "title": "AI",
                "categories": [
                    {
                        "name": "글로벌/빅테크 오픈소스 동향",
                        "query": "AI 오픈소스 OR 오픈AI OR 구글 AI OR 메타 AI OR 앤트로픽 OR 빅테크 AI OR open source AI",
                        "context": "글로벌 빅테크와 오픈소스 AI 생태계의 변화를 보여주는 기사입니다.",
                    },
                    {
                        "name": "AI 인프라 및 비용 동향",
                        "query": "AI 인프라 OR AI 데이터센터 OR GPU OR 엔비디아 OR AI 비용 OR AI 반도체",
                        "context": "AI 데이터센터, GPU, 반도체, 비용 구조와 관련된 기사입니다.",
                    },
                    {
                        "name": "AI 융합 산업",
                        "query": "AI 융합 산업 OR AI 헬스케어 OR AI 금융 OR AI 제조 OR AI 로봇 OR AI 서비스",
                        "context": "AI가 산업 현장과 서비스에 결합되는 흐름을 다루는 기사입니다.",
                    },
                    {
                        "name": "규제 이슈",
                        "query": "AI 규제 OR AI 법안 OR AI 저작권 OR AI 개인정보 OR EU AI Act OR 미국 AI 규제",
                        "context": "AI 법제, 저작권, 개인정보, 안전 규제와 관련된 기사입니다.",
                    },
                ],
            },
        ],
    },
    {
        "id": "vcac",
        "label": "VC/AC",
        "groups": [
            {
                "title": "VC/AC",
                "categories": [
                    {
                        "name": "빅딜 및 메가 라운드 소식",
                        "query": "스타트업 투자 OR 메가라운드 OR 시리즈 C OR Series D OR 대규모 투자 OR 벤처투자",
                        "context": "대규모 스타트업 투자와 성장자금 유입을 확인하는 기사입니다.",
                    },
                    {
                        "name": "신규 펀드 결성",
                        "query": "VC 펀드 결성 OR 벤처캐피탈 펀드 OR 신규 펀드 OR 모태펀드 OR 출자사업",
                        "context": "VC와 AC의 신규 펀드 결성, 출자, 운용자금 흐름을 다루는 기사입니다.",
                    },
                    {
                        "name": "M&A 및 IPO 소식",
                        "query": "스타트업 M&A OR 스타트업 IPO OR 기업공개 OR 인수합병 OR 상장예비심사",
                        "context": "스타트업과 벤처 시장의 인수합병, 상장, 회수 이벤트를 다루는 기사입니다.",
                    },
                ],
            },
        ],
    },
]

NAV_SECTIONS = (
    ("macro", "거시경제"),
    ("ai", "AI"),
    ("vcac", "VC/AC"),
    ("impact", "임팩트"),
)


def parse_args():
    parser = argparse.ArgumentParser(
        description="전일 기준 뉴스들을 수집해 index.html을 갱신합니다."
    )
    parser.add_argument(
        "--date",
        help="수집 기준일. YYYY-MM-DD 또는 YYYY.MM.DD 형식입니다. 기본값은 한국시간 기준 어제입니다.",
    )
    return parser.parse_args()


def get_target_date(date_arg=None):
    if not date_arg:
        return (datetime.now(KST) - timedelta(days=1)).date()

    normalized = date_arg.strip().replace(".", "-")
    return datetime.strptime(normalized, "%Y-%m-%d").date()


def normalize_space(text):
    return re.sub(r"\s+", " ", text or "").strip()


def clean_google_title(title, publisher):
    title = normalize_space(title)
    publisher = normalize_space(publisher)

    if publisher and title.endswith(f" - {publisher}"):
        return title[: -len(f" - {publisher}")].strip()

    if " - " in title:
        possible_title, possible_publisher = title.rsplit(" - ", 1)
        if publisher and possible_publisher.strip() == publisher:
            return possible_title.strip()

    return title


def source_domain(source_url):
    if not source_url:
        return ""
    return urlparse(source_url).netloc.lower().removeprefix("www.")


def is_blocked_domain(domain):
    return any(domain == blocked or domain.endswith(f".{blocked}") for blocked in BLOCKED_SOURCE_DOMAINS)


def is_news_publisher(publisher, source_url):
    publisher = normalize_space(publisher)
    domain = source_domain(source_url)
    joined = f"{publisher} {domain} {source_url}".lower()

    if not publisher or not domain:
        return False

    if any(keyword in joined for keyword in BLOCKED_SOURCE_KEYWORDS):
        return False

    if is_blocked_domain(domain):
        return False

    return True


class ArticleLinkParser(HTMLParser):
    def __init__(self):
        super().__init__()
        self.links = []
        self._href = ""
        self._text_parts = []
        self._capture_depth = 0

    def handle_starttag(self, tag, attrs):
        attrs_dict = dict(attrs)
        if tag == "a" and "articleView.html" in attrs_dict.get("href", ""):
            self._href = attrs_dict["href"]
            self._text_parts = []
            self._capture_depth = 1
        elif self._capture_depth:
            self._capture_depth += 1

    def handle_data(self, data):
        if self._capture_depth:
            self._text_parts.append(data)

    def handle_endtag(self, tag):
        if not self._capture_depth:
            return

        self._capture_depth -= 1
        if self._capture_depth == 0 and self._href:
            title = normalize_space(html.unescape(" ".join(self._text_parts)))
            self.links.append((title, self._href))
            self._href = ""
            self._text_parts = []


def fetch_text(url, timeout=15):
    request = urllib.request.Request(url, headers=HEADERS)
    with urllib.request.urlopen(request, timeout=timeout) as response:
        charset = response.headers.get_content_charset() or "utf-8"
        return response.read().decode(charset, errors="replace")


def strip_tags(raw_html):
    text = re.sub(r"<script\b[^>]*>.*?</script>", " ", raw_html, flags=re.IGNORECASE | re.DOTALL)
    text = re.sub(r"<style\b[^>]*>.*?</style>", " ", text, flags=re.IGNORECASE | re.DOTALL)
    text = re.sub(r"<[^>]+>", " ", text)
    return normalize_space(html.unescape(text))


def extract_meta_description(raw_html):
    for meta_tag in re.findall(r"<meta\b[^>]*>", raw_html, flags=re.IGNORECASE):
        attrs = dict(
            (name.lower(), value)
            for name, value in re.findall(r'([:\w-]+)\s*=\s*["\']([^"\']*)["\']', meta_tag)
        )
        key = (attrs.get("name") or attrs.get("property") or "").lower()
        if key in {"description", "og:description", "twitter:description"} and attrs.get("content"):
            return normalize_space(html.unescape(attrs["content"]))
    return ""


def clean_summary_candidate(text):
    text = strip_tags(text)
    text = re.sub(r"\[[^\]]{1,20}\]", " ", text)
    text = re.sub(r"\([^)]{1,18}(기자|특파원|연합뉴스)[^)]*\)", " ", text)
    return normalize_space(text)


def is_useful_summary_line(text, title):
    lowered = text.lower()
    if len(text) < 18:
        return False
    if any(keyword.lower() in lowered for keyword in SUMMARY_SKIP_KEYWORDS):
        return False
    if text == title or text in title:
        return False
    if title and title in text and len(text) <= len(title) + 25:
        return False
    return True


def trim_summary_line(text, max_chars=SUMMARY_MAX_CHARS):
    text = normalize_space(text)
    if len(text) <= max_chars:
        return text
    return text[: max_chars - 1].rstrip(" ,.;:") + "…"


def split_summary_sentences(text):
    text = clean_summary_candidate(text)
    if not text:
        return []

    text = re.sub(r"([.!?])\s+", r"\1|", text)
    text = re.sub(r"(다\.|요\.|죠\.|니다\.|했다\.|됐다\.|된다\.)\s+", r"\1|", text)

    sentences = []
    for part in text.split("|"):
        part = normalize_space(part)
        if part:
            sentences.append(part)
    return sentences


def extract_article_text(raw_html):
    meta_description = extract_meta_description(raw_html)
    paragraph_html = re.findall(r"<p\b[^>]*>(.*?)</p>", raw_html, flags=re.IGNORECASE | re.DOTALL)
    paragraphs = []

    for paragraph in paragraph_html:
        text = clean_summary_candidate(paragraph)
        if len(text) >= 30 and not any(keyword.lower() in text.lower() for keyword in SUMMARY_SKIP_KEYWORDS):
            paragraphs.append(text)

    if paragraphs:
        return " ".join(paragraphs[:8])

    return meta_description or strip_tags(raw_html)[:1200]


def make_three_line_summary(title, raw_text="", source="", context=""):
    title = normalize_space(title)
    source = normalize_space(source)
    context = normalize_space(context)
    lines = []
    seen = set()

    for sentence in split_summary_sentences(raw_text):
        sentence = trim_summary_line(sentence)
        key = sentence.casefold()
        if key in seen or not is_useful_summary_line(sentence, title):
            continue
        lines.append(sentence)
        seen.add(key)
        if len(lines) >= SUMMARY_LINE_COUNT:
            break

    fallback_lines = [
        f"핵심: {title}",
        f"맥락: {context or '전일 주요 이슈로 분류된 기사입니다.'}",
        f"출처: {source or '원문'}에서 세부 배경과 수치를 확인할 수 있습니다.",
    ]

    for fallback in fallback_lines:
        if len(lines) >= SUMMARY_LINE_COUNT:
            break
        fallback = trim_summary_line(fallback)
        key = fallback.casefold()
        if key not in seen:
            lines.append(fallback)
            seen.add(key)

    return lines[:SUMMARY_LINE_COUNT]


def extract_impact_date(article_html):
    meta_pattern = (
        r'<meta[^>]+property=["\']article:published_time["\'][^>]+'
        r'content=["\']([^"\']+)["\']'
    )
    match = re.search(meta_pattern, article_html, flags=re.IGNORECASE)
    if match:
        return match.group(1)[:10].replace("-", ".")

    page_text = strip_tags(article_html)
    match = re.search(r"승인\s*(\d{4}[.-]\d{2}[.-]\d{2})", page_text)
    if match:
        return match.group(1).replace("-", ".")

    return ""


def fetch_impact_news(target_date, seen_links):
    target_dot = target_date.strftime("%Y.%m.%d")
    impact_news = []

    print("\n1. 임팩트뉴스 수집 시작...")
    try:
        list_html = fetch_text("https://www.impacton.net/news/articleList.html")
        parser = ArticleLinkParser()
        parser.feed(list_html)

        for title, href in parser.links:
            if len(impact_news) >= MAX_IMPACT_NEWS:
                break

            if not title or len(title) < 4 or not href:
                continue

            link = href if href.startswith("http") else f"https://www.impacton.net{href}"
            if link in seen_links or "pro" in title.lower() or "유료" in title:
                continue

            try:
                article_html = fetch_text(link)
                article_date = extract_impact_date(article_html)
                summary = make_three_line_summary(
                    title,
                    extract_article_text(article_html),
                    "임팩트온",
                    "임팩트뉴스의 주요 비즈니스/ESG 이슈입니다.",
                )
            except Exception as exc:
                print(f"  - 임팩트뉴스 상세 확인 실패: {title} ({exc})")
                continue

            if article_date == target_dot:
                seen_links.add(link)
                impact_news.append({
                    "title": title,
                    "link": link,
                    "date": article_date,
                    "source": "임팩트온",
                    "summary": summary,
                })
                print(f"  - 수집: {title}")

            time.sleep(0.5)
    except Exception as exc:
        print(f"임팩트뉴스 수집 오류: {exc}")

    return impact_news


def fetch_search_sections(target_date, seen_links):
    results = []

    print("\n2. Google News 카테고리 수집 시작...")
    for section in SEARCH_SECTIONS:
        section_result = {
            "id": section["id"],
            "label": section["label"],
            "groups": [],
        }
        print(f"\n  [{section['label']}]")

        for group in section["groups"]:
            group_result = {
                "title": group["title"],
                "categories": [],
            }

            for category in group["categories"]:
                news_list = fetch_google_news_category(
                    target_date=target_date,
                    section=section,
                    group=group,
                    category=category,
                    seen_links=seen_links,
                )
                group_result["categories"].append({
                    "name": category["name"],
                    "news": news_list,
                })

            section_result["groups"].append(group_result)

        results.append(section_result)

    return results


def fetch_google_news_category(target_date, section, group, category, seen_links):
    target_dot = target_date.strftime("%Y.%m.%d")
    news_list = []

    print(f"    - {group['title']} / {category['name']} 검색 중...")
    try:
        full_query = (
            f"({category['query']}) "
            "-블로그 -카페 -티스토리 -브런치 -blog -cafe when:2d"
        )
        encoded_query = urllib.parse.quote(full_query)
        rss_url = (
            "https://news.google.com/rss/search?"
            f"q={encoded_query}&hl=ko&gl=KR&ceid=KR%3Ako"
        )
        rss_text = fetch_text(rss_url)
        root = ElementTree.fromstring(rss_text)
        items = root.findall(".//item")

        for item in items:
            if len(news_list) >= MAX_NEWS_PER_CATEGORY:
                break

            article = parse_google_news_item(item, target_dot)
            if not article:
                continue

            dedupe_key = (
                article["title"].casefold(),
                article["source"].casefold(),
            )
            if article["link"] in seen_links or dedupe_key in seen_links:
                continue

            if not is_news_publisher(article["source"], article["source_url"]):
                continue

            article["section"] = section["label"]
            article["group"] = group["title"]
            article["category"] = category["name"]
            article["context"] = category["context"]
            article["summary"] = build_search_summary(article)

            seen_links.add(article["link"])
            seen_links.add(dedupe_key)
            news_list.append(article)
            print(f"      수집: {article['title']} ({article['source']})")

    except Exception as exc:
        print(f"      수집 오류: {exc}")

    return news_list


def parse_google_news_item(item, target_dot):
    title = normalize_space(item.findtext("title", ""))
    link = normalize_space(item.findtext("link", ""))
    description = strip_tags(item.findtext("description", ""))
    pub_date_text = normalize_space(item.findtext("pubDate", ""))
    source_tag = item.find("source")
    publisher = normalize_space(source_tag.text if source_tag is not None else "")
    source_url = source_tag.get("url", "") if source_tag is not None else ""

    if not title or not link or not pub_date_text:
        return None

    try:
        pub_dt = parsedate_to_datetime(pub_date_text).astimezone(KST)
    except Exception:
        return None

    pub_date_dot = pub_dt.strftime("%Y.%m.%d")
    if pub_date_dot != target_dot:
        return None

    clean_title = clean_google_title(title, publisher)
    if is_redundant_description(description, clean_title, publisher):
        description = ""

    return {
        "title": clean_title,
        "link": link,
        "source": publisher or "구글뉴스",
        "source_url": source_url,
        "date": pub_date_dot,
        "description": description,
    }


def is_redundant_description(description, title, publisher):
    description = normalize_space(description)
    title = normalize_space(title)
    publisher = normalize_space(publisher)
    compact_description = re.sub(r"\W+", "", description).casefold()
    compact_title = re.sub(r"\W+", "", title).casefold()
    compact_publisher = re.sub(r"\W+", "", publisher).casefold()

    if not description:
        return True
    if compact_description in {compact_title, f"{compact_title}{compact_publisher}"}:
        return True
    if compact_description.startswith(compact_title) and len(description) <= len(title) + len(publisher) + 18:
        return True
    return False


def build_search_summary(article):
    return make_three_line_summary(
        article["title"],
        article.get("description", ""),
        article.get("source", ""),
        article.get("context", ""),
    )


def count_search_section_articles(section):
    return sum(
        len(category["news"])
        for group in section["groups"]
        for category in group["categories"]
    )


def section_count_map(search_sections, impact_news):
    counts = {section["id"]: count_search_section_articles(section) for section in search_sections}
    counts["impact"] = len(impact_news)
    return counts


def render_html(target_date, impact_news, search_sections):
    target_dot = target_date.strftime("%Y.%m.%d")
    updated_at = datetime.now(KST).strftime("%Y.%m.%d %H:%M")
    counts = section_count_map(search_sections, impact_news)

    html_content = f"""<!DOCTYPE html>
<html lang="ko">
<head>
    <meta charset="UTF-8">
    <meta name="viewport" content="width=device-width, initial-scale=1.0">
    <title>오늘의 뉴스 브리핑</title>
    <style>
        * {{ box-sizing: border-box; }}
        body {{
            font-family: "Malgun Gothic", "Apple SD Gothic Neo", Arial, sans-serif;
            max-width: 1120px;
            margin: 0 auto;
            padding: 28px 18px 46px;
            background-color: #f4f7f6;
            color: #24313d;
        }}
        h1 {{
            margin: 0 0 8px;
            color: #1f2d3a;
            text-align: center;
            font-size: 2rem;
        }}
        .date-title {{
            text-align: center;
            color: #667788;
            margin-bottom: 26px;
            line-height: 1.6;
        }}
        .top-tabs {{
            display: grid;
            grid-template-columns: repeat(4, minmax(0, 1fr));
            gap: 10px;
            margin: 0 0 28px;
            position: sticky;
            top: 0;
            z-index: 5;
            padding: 10px 0;
            background: #f4f7f6;
        }}
        .nav-tab {{
            border: 1px solid #d7e0e6;
            background: #ffffff;
            color: #24313d;
            border-radius: 8px;
            padding: 13px 12px;
            font-size: 1rem;
            font-weight: 700;
            cursor: pointer;
            box-shadow: 0 2px 5px rgba(25, 42, 58, 0.04);
        }}
        .nav-tab:hover {{
            border-color: #9fb0bd;
        }}
        .nav-tab.active {{
            background: #24313d;
            color: #ffffff;
            border-color: #24313d;
        }}
        .tab-count {{
            display: inline-flex;
            align-items: center;
            justify-content: center;
            min-width: 24px;
            height: 24px;
            margin-left: 6px;
            padding: 0 7px;
            border-radius: 999px;
            background: rgba(36, 49, 61, 0.1);
            font-size: 0.82rem;
        }}
        .nav-tab.active .tab-count {{
            background: rgba(255, 255, 255, 0.2);
        }}
        .content-section {{
            display: none;
        }}
        .content-section.active {{
            display: block;
        }}
        .section-title {{
            font-size: 1.55rem;
            color: #22313f;
            border-left: 6px solid #2f8f83;
            padding-left: 12px;
            margin: 30px 0 18px;
            font-weight: 800;
        }}
        .group-title {{
            margin: 26px 0 12px;
            padding: 9px 12px;
            border-radius: 8px;
            background: #e6ecef;
            color: #263746;
            font-size: 1.08rem;
            font-weight: 800;
        }}
        .sub-category {{
            font-size: 1rem;
            color: #2c3e50;
            margin: 18px 0 10px;
            font-weight: 800;
        }}
        .news-card {{
            background: #ffffff;
            padding: 18px 20px;
            margin-bottom: 12px;
            border-radius: 8px;
            border: 1px solid #e4e9ed;
            box-shadow: 0 2px 5px rgba(25, 42, 58, 0.04);
        }}
        .news-title {{
            font-size: 1.05rem;
            font-weight: 800;
            margin-bottom: 8px;
            line-height: 1.45;
        }}
        .news-title a {{
            color: #1f2d3a;
            text-decoration: none;
        }}
        .news-title a:hover {{
            color: #1d6fa5;
            text-decoration: underline;
        }}
        .news-date {{
            font-size: 0.86rem;
            color: #748595;
            line-height: 1.5;
        }}
        .news-summary {{
            margin: 12px 0 0;
            padding-left: 20px;
            color: #334454;
            font-size: 0.94rem;
            line-height: 1.65;
        }}
        .news-summary li {{
            margin: 2px 0;
        }}
        .no-news {{
            color: #8a98a5;
            font-style: italic;
            padding: 10px 4px 16px;
        }}
        @media (max-width: 720px) {{
            body {{ padding: 22px 14px 36px; }}
            h1 {{ font-size: 1.55rem; }}
            .top-tabs {{ grid-template-columns: repeat(2, minmax(0, 1fr)); }}
            .section-title {{ font-size: 1.22rem; }}
            .news-card {{ padding: 16px; }}
        }}
    </style>
</head>
<body>
    <h1>오늘의 뉴스 브리핑</h1>
    <div class="date-title">
        자동 업데이트 기준일: {html.escape(target_dot)} 전일 기사<br>
        마지막 갱신: {html.escape(updated_at)} KST
    </div>
    <div class="top-tabs" role="tablist">
{render_nav_buttons(counts)}
    </div>
"""

    for index, section in enumerate(search_sections):
        html_content += render_search_section(section, active=index == 0)

    html_content += render_impact_section(impact_news, target_dot)
    html_content += """
    <script>
        const tabs = document.querySelectorAll(".nav-tab");
        const sections = document.querySelectorAll(".content-section");

        function activateSection(targetId) {
            tabs.forEach((tab) => {
                const isActive = tab.dataset.target === targetId;
                tab.classList.toggle("active", isActive);
                tab.setAttribute("aria-selected", String(isActive));
            });
            sections.forEach((section) => {
                section.classList.toggle("active", section.id === targetId);
            });
        }

        tabs.forEach((tab) => {
            tab.addEventListener("click", () => {
                activateSection(tab.dataset.target);
                history.replaceState(null, "", "#" + tab.dataset.target.replace("section-", ""));
            });
        });

        const initialHash = location.hash.replace("#", "");
        if (initialHash) {
            const target = "section-" + initialHash;
            if (document.getElementById(target)) {
                activateSection(target);
            }
        }
    </script>
</body>
</html>
"""
    return html_content


def render_nav_buttons(counts):
    buttons = []
    for index, (section_id, label) in enumerate(NAV_SECTIONS):
        target = f"section-{section_id}"
        active_class = " active" if index == 0 else ""
        count = counts.get(section_id, 0)
        buttons.append(
            f'        <button class="nav-tab{active_class}" type="button" '
            f'role="tab" aria-selected="{str(index == 0).lower()}" '
            f'data-target="{html.escape(target)}">{html.escape(label)}'
            f'<span class="tab-count">{count}</span></button>'
        )
    return "\n".join(buttons)


def render_search_section(section, active=False):
    active_class = " active" if active else ""
    html_content = (
        f'    <section id="section-{html.escape(section["id"])}" '
        f'class="content-section{active_class}">\n'
        f'        <div class="section-title">{html.escape(section["label"])}</div>\n'
    )

    for group in section["groups"]:
        html_content += f'        <div class="group-title">{html.escape(group["title"])}</div>\n'
        for category in group["categories"]:
            html_content += f'        <div class="sub-category">{html.escape(category["name"])}</div>\n'
            if not category["news"]:
                html_content += "        <div class='no-news'>전일 기준 수집된 뉴스가 없습니다.</div>\n"
                continue

            for news in category["news"]:
                html_content += render_news_card(news)

    html_content += "    </section>\n"
    return html_content


def render_impact_section(impact_news, target_dot):
    html_content = (
        '    <section id="section-impact" class="content-section">\n'
        '        <div class="section-title">임팩트</div>\n'
    )

    if not impact_news:
        html_content += f"        <div class='no-news'>{html.escape(target_dot)} 자에 발행된 임팩트뉴스가 없습니다.</div>\n"
    else:
        for news in impact_news:
            html_content += render_news_card(news, default_source="임팩트온")

    html_content += "    </section>\n"
    return html_content


def render_news_card(news, default_source=None):
    title = html.escape(news["title"])
    link = html.escape(news["link"], quote=True)
    date = html.escape(news.get("date", ""))
    source = html.escape(news.get("source") or default_source or "")
    summary = news.get("summary") or []
    if len(summary) < SUMMARY_LINE_COUNT:
        summary = make_three_line_summary(
            news["title"],
            " ".join(summary),
            news.get("source") or default_source or "",
            news.get("context", ""),
        )
    summary_items = "\n".join(
        f"            <li>{html.escape(line)}</li>" for line in summary[:SUMMARY_LINE_COUNT]
    )

    source_text = f"출처: {source}" if source else ""
    date_text = f"발행일: {date}" if date else ""
    separator = " | " if source_text and date_text else ""
    meta = f"{source_text}{separator}{date_text}"

    return f"""        <div class="news-card">
            <div class="news-title"><a href="{link}" target="_blank" rel="noopener noreferrer">{title}</a></div>
            <div class="news-date">{meta}</div>
            <ul class="news-summary">
{summary_items}
            </ul>
        </div>
"""


def main():
    args = parse_args()
    target_date = get_target_date(args.date)
    target_dot = target_date.strftime("%Y.%m.%d")
    seen_links = set()

    print(f"수집 기준일: {target_dot} (한국시간 기준 전일)")
    impact_news = fetch_impact_news(target_date, seen_links)
    search_sections = fetch_search_sections(target_date, seen_links)

    html_content = render_html(target_date, impact_news, search_sections)
    OUTPUT_FILE.write_text(html_content, encoding="utf-8")

    counts = section_count_map(search_sections, impact_news)
    print("\n완료")
    for section_id, label in NAV_SECTIONS:
        print(f"- {label}: {counts.get(section_id, 0)}건")
    print(f"- 결과 파일: {OUTPUT_FILE}")


if __name__ == "__main__":
    main()
