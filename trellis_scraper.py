import argparse
import json
import re
import time
import urllib.parse
import urllib.request
from dataclasses import dataclass, asdict
from datetime import date, datetime, timedelta, timezone
from email.utils import parsedate_to_datetime
from html import unescape
from pathlib import Path
from typing import Iterable, Optional
from xml.etree import ElementTree as ET

from bs4 import BeautifulSoup

try:
    import requests
except ImportError:  # pragma: no cover - fallback for bundled runtime
    requests = None


BASE_URL = "https://trellis.net"
SITEMAP_INDEX_URL = f"{BASE_URL}/sitemap_index.xml"
DEFAULT_DELAY_SECONDS = 3.5
KST = timezone(timedelta(hours=9))

HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/126.0.0.0 Safari/537.36"
    ),
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
    "Accept-Language": "en-US,en;q=0.9",
}


@dataclass
class TrellisArticle:
    url: str
    title: str = ""
    published_at: str = ""
    updated_at: str = ""
    author: str = ""
    description: str = ""
    content: str = ""


def http_get_text(url: str, timeout: int = 30, encoding: Optional[str] = None) -> str:
    if requests is not None:
        response = requests.get(url, headers=HEADERS, timeout=timeout)
        response.raise_for_status()
        if encoding:
            response.encoding = encoding
        return response.text

    request = urllib.request.Request(url, headers=HEADERS)
    with urllib.request.urlopen(request, timeout=timeout) as response:
        body = response.read()
        charset = encoding or response.headers.get_content_charset() or "utf-8"
        return body.decode(charset, errors="replace")


def normalize_space(text: str) -> str:
    return re.sub(r"\s+", " ", unescape(text or "")).strip()


def parse_xml(text: str) -> ET.Element:
    return ET.fromstring(text)


def tag_name(tag: str) -> str:
    return tag.rsplit("}", 1)[-1]


def parse_sitemap_index(xml_text: str) -> list[str]:
    root = parse_xml(xml_text)
    sitemap_urls: list[str] = []
    for child in root.iter():
        if tag_name(child.tag) == "loc" and child.text:
            sitemap_urls.append(child.text.strip())
    return sitemap_urls


def parse_urlset(xml_text: str) -> list[dict[str, str]]:
    root = parse_xml(xml_text)
    entries: list[dict[str, str]] = []

    for url_node in root.findall(".//{*}url"):
        entry: dict[str, str] = {}
        loc_node = url_node.find("{*}loc")
        lastmod_node = url_node.find("{*}lastmod")
        if loc_node is not None and loc_node.text:
            entry["loc"] = loc_node.text.strip()
        if lastmod_node is not None and lastmod_node.text:
            entry["lastmod"] = lastmod_node.text.strip()
        if entry.get("loc"):
            entries.append(entry)

    return entries


def iter_sitemap_urls(index_url: str) -> Iterable[tuple[str, Optional[str]]]:
    index_xml = http_get_text(index_url, timeout=30)
    sitemap_urls = parse_sitemap_index(index_xml)
    for sitemap_url in sitemap_urls:
        try:
            sitemap_xml = http_get_text(sitemap_url, timeout=30)
            for entry in parse_urlset(sitemap_xml):
                yield entry.get("loc", ""), entry.get("lastmod")
        except Exception as exc:  # pragma: no cover - network failure path
            print(f"[warn] failed to read sitemap {sitemap_url}: {exc}")


def normalize_url(url: str) -> str:
    parsed = urllib.parse.urlparse(url)
    cleaned = parsed._replace(fragment="")
    return urllib.parse.urlunparse(cleaned)


def should_skip_url(url: str) -> bool:
    parsed = urllib.parse.urlparse(url)
    path = parsed.path.lower()
    query = parsed.query.lower()

    if "/search/" in path:
        return True
    if "/wp-json/" in path:
        return True
    if "/page/" in path and "s=" in query:
        return True
    if "s=" in query or "rest_route=" in query:
        return True
    return False


def is_trellis_news_url(url: str) -> bool:
    parsed = urllib.parse.urlparse(url)
    path = parsed.path.lower()
    return parsed.netloc.endswith("trellis.net") and ("/article/" in path or "/news/" in path)


def parse_date_value(value: str) -> Optional[datetime]:
    if not value:
        return None

    value = value.strip()
    candidates = (
        value,
        value.replace("Z", "+00:00"),
    )
    for candidate in candidates:
        try:
            parsed = datetime.fromisoformat(candidate)
            if parsed.tzinfo is None:
                return parsed.replace(tzinfo=timezone.utc)
            return parsed
        except ValueError:
            pass

    try:
        return parsedate_to_datetime(value)
    except Exception:
        return None


def extract_json_ld(soup: BeautifulSoup) -> dict:
    for script in soup.find_all("script", attrs={"type": "application/ld+json"}):
        raw = script.string or script.get_text(strip=True)
        raw = raw.strip()
        if not raw:
            continue
        try:
            data = json.loads(raw)
        except json.JSONDecodeError:
            continue
        if isinstance(data, dict):
            if data.get("@type") in {"NewsArticle", "Article", "ReportageNewsArticle", "BlogPosting"}:
                return data
            graph = data.get("@graph")
            if isinstance(graph, list):
                for item in graph:
                    if isinstance(item, dict) and item.get("@type") in {
                        "NewsArticle",
                        "Article",
                        "ReportageNewsArticle",
                        "BlogPosting",
                    }:
                        return item
        elif isinstance(data, list):
            for item in data:
                if isinstance(item, dict) and item.get("@type") in {
                    "NewsArticle",
                    "Article",
                    "ReportageNewsArticle",
                    "BlogPosting",
                }:
                    return item
    return {}


def extract_article_text(soup: BeautifulSoup) -> str:
    article = soup.find("article")
    if article is None:
        article = soup

    for selector in [
        "script",
        "style",
        "noscript",
        "svg",
        "iframe",
    ]:
        for node in article.select(selector):
            node.decompose()

    paragraphs = []
    for p in article.find_all(["p", "h2", "h3", "li"]):
        text = normalize_space(p.get_text(" ", strip=True))
        if text:
            paragraphs.append(text)

    return "\n".join(paragraphs)


def parse_article_page(url: str, html_text: str) -> TrellisArticle:
    soup = BeautifulSoup(html_text, "html.parser")
    json_ld = extract_json_ld(soup)

    title = ""
    if soup.title and soup.title.string:
        title = normalize_space(soup.title.string)
    if json_ld.get("headline"):
        title = normalize_space(str(json_ld["headline"]))
    elif soup.find("meta", property="og:title"):
        title = normalize_space(soup.find("meta", property="og:title").get("content", ""))

    description = ""
    meta_desc = soup.find("meta", attrs={"name": "description"})
    if meta_desc:
        description = normalize_space(meta_desc.get("content", ""))
    if not description and soup.find("meta", property="og:description"):
        description = normalize_space(soup.find("meta", property="og:description").get("content", ""))
    if json_ld.get("description"):
        description = normalize_space(str(json_ld["description"]))

    published_at = ""
    updated_at = ""
    for key in ("datePublished", "dateCreated"):
        if json_ld.get(key):
            published_at = str(json_ld[key])
            break
    if json_ld.get("dateModified"):
        updated_at = str(json_ld["dateModified"])

    if not published_at:
        meta_pub = soup.find("meta", attrs={"property": "article:published_time"})
        if meta_pub:
            published_at = meta_pub.get("content", "")
    if not updated_at:
        meta_mod = soup.find("meta", attrs={"property": "article:modified_time"})
        if meta_mod:
            updated_at = meta_mod.get("content", "")

    author = ""
    author_value = json_ld.get("author")
    if isinstance(author_value, dict):
        author = normalize_space(str(author_value.get("name", "")))
    elif isinstance(author_value, list):
        author = ", ".join(
            normalize_space(str(item.get("name", "")))
            for item in author_value
            if isinstance(item, dict) and item.get("name")
        )
    elif isinstance(author_value, str):
        author = normalize_space(author_value)

    if not author:
        author_meta = soup.find("meta", attrs={"name": "author"})
        if author_meta:
            author = normalize_space(author_meta.get("content", ""))

    content = extract_article_text(soup)
    return TrellisArticle(
        url=url,
        title=title,
        published_at=published_at,
        updated_at=updated_at,
        author=author,
        description=description,
        content=content,
    )


def article_date(article: TrellisArticle) -> Optional[datetime]:
    for value in (article.published_at, article.updated_at):
        parsed = parse_date_value(value)
        if parsed is not None:
            return parsed.astimezone(KST)
    return None


def fetch_article(url: str, timeout: int = 30) -> str:
    return http_get_text(url, timeout=timeout)


def collect_yesterday_articles(
    target_date: date,
    delay_seconds: float,
    limit: Optional[int] = None,
) -> list[TrellisArticle]:
    results: list[TrellisArticle] = []
    seen: set[str] = set()

    index_xml = http_get_text(SITEMAP_INDEX_URL, timeout=30)
    sitemap_urls = [
        url
        for url in parse_sitemap_index(index_xml)
        if "post-sitemap" in url
    ]

    def sitemap_sort_key(url: str) -> tuple[int, str]:
        match = re.search(r"post-sitemap(?:([0-9]+))?\.xml$", url)
        if not match:
            return (0, url)
        number = int(match.group(1) or "1")
        return (number, url)

    sitemap_urls.sort(key=sitemap_sort_key, reverse=True)

    for sitemap_url in sitemap_urls:
        try:
            sitemap_xml = http_get_text(sitemap_url, timeout=30)
        except Exception as exc:  # pragma: no cover - network failure path
            print(f"[warn] failed to read sitemap {sitemap_url}: {exc}")
            continue

        entries = parse_urlset(sitemap_xml)
        if not entries:
            continue

        for entry in reversed(entries):
            url = normalize_url(entry.get("loc", ""))
            if not url or url in seen:
                continue
            if should_skip_url(url) or not is_trellis_news_url(url):
                continue

            lastmod = entry.get("lastmod", "")
            parsed_lastmod = parse_date_value(lastmod) if lastmod else None
            if parsed_lastmod is not None:
                source_lastmod = parsed_lastmod.date()
                if source_lastmod < target_date:
                    break
                if source_lastmod != target_date:
                    continue

            seen.add(url)

            try:
                html_text = fetch_article(url)
                article = parse_article_page(url, html_text)
                published = article_date(article)
                if published and published.date() == target_date:
                    results.append(article)
                    print(f"[match] {published.date()} | {article.title or url}")
                    if limit is not None and len(results) >= limit:
                        return results
            except Exception as exc:  # pragma: no cover - network failure path
                print(f"[warn] failed to fetch {url}: {exc}")

            time.sleep(delay_seconds)

    return results


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Scrape Trellis news articles published on a target date using sitemap URLs."
    )
    parser.add_argument(
        "--date",
        help="Target date in YYYY-MM-DD. Defaults to yesterday in KST.",
        default=None,
    )
    parser.add_argument(
        "--delay",
        type=float,
        default=DEFAULT_DELAY_SECONDS,
        help="Delay between article requests in seconds.",
    )
    parser.add_argument(
        "--limit",
        type=int,
        default=None,
        help="Stop after collecting this many matching articles.",
    )
    parser.add_argument(
        "--output",
        default="trellis_yesterday_news.json",
        help="Where to write the collected articles as JSON.",
    )
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    if args.date:
        target_date = datetime.strptime(args.date, "%Y-%m-%d").date()
    else:
        target_date = datetime.now(KST).date() - timedelta(days=1)

    articles = collect_yesterday_articles(
        target_date=target_date,
        delay_seconds=args.delay,
        limit=args.limit,
    )

    output_path = Path(args.output).resolve()
    payload = {
        "source": "trellis.net",
        "target_date": target_date.isoformat(),
        "count": len(articles),
        "articles": [asdict(article) for article in articles],
    }
    output_path.write_text(
        json.dumps(payload, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )

    print(f"[done] saved {len(articles)} articles to {output_path}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
