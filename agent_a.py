import argparse
import csv
import json
import re
import zipfile
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from urllib.parse import urlparse

from bs4 import BeautifulSoup

import main as scraper


BASE_DIR = Path(__file__).resolve().parent
DEFAULT_OUTPUT_DIR = BASE_DIR / "pipeline_data" / "agent_a"
KST = scraper.KST

RAW_ARTICLE_COLUMNS = [
    "article_id",
    "target_date",
    "section_id",
    "section_label",
    "group",
    "category",
    "source",
    "published_date",
    "title",
    "url",
    "source_domain",
    "original_text",
    "text_char_count",
    "body_quality",
    "summary_context",
    "collected_at",
]
EXCEL_CELL_CHAR_LIMIT = 32767


@dataclass
class AgentAOptions:
    target_date: object
    output_dir: Path
    include_theme: bool = True
    include_newsletters: bool = True
    write_xlsx: bool = True


def parse_args():
    parser = argparse.ArgumentParser(
        description="Agent A: collect selected news titles and cleaned original article text for Agent B."
    )
    parser.add_argument("--date", "--news-date", dest="date", help="Target news date (YYYY-MM-DD). Defaults to yesterday in KST.")
    parser.add_argument("--output-dir", default=str(DEFAULT_OUTPUT_DIR), help="Base output directory for Agent A files.")
    parser.add_argument("--skip-theme", action="store_true", help="Skip the strong-theme news block.")
    parser.add_argument("--skip-newsletters", action="store_true", help="Skip Gmail newsletter collection.")
    parser.add_argument("--no-xlsx", action="store_true", help="Write JSONL/CSV only.")
    return parser.parse_args()


def target_date_from_arg(date_arg):
    if date_arg:
        return datetime.strptime(date_arg.strip().replace(".", "-"), "%Y-%m-%d").date()
    return (datetime.now(KST) - timedelta(days=1)).date()


def source_domain(url):
    try:
        netloc = urlparse(url or "").netloc.lower()
        return netloc[4:] if netloc.startswith("www.") else netloc
    except Exception:
        return ""


def normalize_text(text):
    return scraper.normalize_space(str(text or ""))


def text_has_korean(text, minimum_hangul_chars=20):
    return len(re.findall(r"[가-힣]", text or "")) >= minimum_hangul_chars


def cut_from_pattern(text, pattern, flags=re.IGNORECASE):
    match = re.search(pattern, text or "", flags)
    if not match:
        return text
    return text[: match.start()]


def remove_pattern(text, pattern, replacement="", flags=re.IGNORECASE):
    return re.sub(pattern, replacement, text or "", flags=flags)


def is_domain(domain, *candidates):
    return any(domain == candidate or domain.endswith("." + candidate) for candidate in candidates)


def clean_original_text_by_source(text, source, url, title=""):
    text = normalize_text(text)
    source = normalize_text(source)
    domain = source_domain(url)

    if is_domain(domain, "socialimpactnews.net"):
        text = cut_from_pattern(text, r"\b[가-힣]{2,5}\s+소임리포터\b.*$")

    if is_domain(domain, "eroun.net"):
        text = remove_pattern(text, r"^이로운넷\s*=\s*[^=]{1,30}?기자\s*")

    if is_domain(domain, "unicornfactory.co.kr"):
        text = cut_from_pattern(text, r"\[?\s*머니투데이\s+스타트업\s+미디어\s+플랫폼\s+유니콘팩토리\b.*$")

    if is_domain(domain, "startuprecipe.co.kr"):
        text = cut_from_pattern(text, r"\b이\s*글\s*공유하기\s*:\s*.*$")

    if is_domain(domain, "venturesquare.net"):
        text = cut_from_pattern(text, r"\bfacebook\s+twitter\b.*$")

    if is_domain(domain, "artificialintelligence-news.com"):
        text = cut_from_pattern(text, r"\bSee also\b.*$")

    if is_domain(domain, "aitimes.com"):
        text = remove_pattern(text, r"\s*[가-힣]{2,5}\s*기자\s+[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}\s*$")
        text = remove_pattern(text, r"\s+[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}\s*$")

    if is_domain(domain, "deeplearning.ai") and "batch" in (source + " " + title).lower():
        text = remove_pattern(text, r"^.*?\bDear friends\b[:,]?\s*", flags=re.IGNORECASE | re.DOTALL)
        text = cut_from_pattern(
            text,
            r"\bShare\s+Subscribe\s+to\s+The\s+Batch\s+Stay\s+updated\s+with\s+weekly\s+AI\s+News\s+and\s+Insights\s+delivered\s+to\s+your\s+inbox\b.*$",
        )

    if is_domain(domain, "mk.co.kr"):
        text = remove_pattern(text, r"\s*\([^)]*=연합뉴스\)\s*[^=]{1,80}?기자\s*=\s*", " ")
        text = remove_pattern(text, r"\s*(?:[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}|https?://\S+|www\.\S+).*$")

    if is_domain(domain, "yna.co.kr"):
        text = remove_pattern(text, r"^.*?\([^)]*=연합뉴스\)\s*[^=]{1,80}?기자\s*=\s*", flags=re.IGNORECASE | re.DOTALL)
        text = cut_from_pattern(text, r"\b[A-Za-z0-9._%+-]+@yna\.co\.kr\s+제보는\b.*$")
        text = remove_pattern(text, r"\s+[A-Za-z0-9._%+-]+@yna\.co\.kr\s*$")

    return normalize_text(text)


def should_keep_row(row):
    domain = row.get("source_domain", "")
    if is_domain(domain, "venturesquare.net"):
        return text_has_korean(f"{row.get('title', '')} {row.get('original_text', '')}")
    return True


def classify_body_quality(text):
    text = normalize_text(text)
    length = len(text)
    if length >= 1200:
        return "full"
    if length >= 450:
        return "usable"
    if length >= 160:
        return "thin"
    return "missing"


def make_article_id(target_date, index):
    return f"A-{target_date:%Y%m%d}-{index:04d}"


def news_to_row(news, target_date, index, section_id, section_label, group, category):
    title = normalize_text(news.get("title", ""))
    url = normalize_text(news.get("link", ""))
    source = normalize_text(news.get("source", ""))
    original_text = normalize_text(news.get("_summary_source", ""))
    if not original_text:
        original_text = normalize_text(" ".join(str(line) for line in news.get("summary", [])))
    original_text = clean_original_text_by_source(original_text, source, url, title)

    return {
        "article_id": make_article_id(target_date, index),
        "target_date": target_date.strftime("%Y-%m-%d"),
        "section_id": section_id,
        "section_label": section_label,
        "group": group,
        "category": category,
        "source": source,
        "published_date": normalize_text(news.get("date", "")),
        "title": title,
        "url": url,
        "source_domain": source_domain(url),
        "original_text": original_text,
        "text_char_count": len(original_text),
        "body_quality": classify_body_quality(original_text),
        "summary_context": normalize_text(news.get("_summary_context", "")),
        "collected_at": datetime.now(KST).strftime("%Y-%m-%d %H:%M:%S KST"),
    }


def append_news_rows(rows, news_list, target_date, section_id, section_label, group, category):
    for news in news_list or []:
        title = normalize_text(news.get("title", ""))
        url = normalize_text(news.get("link", ""))
        if not title or not url:
            continue
        row = news_to_row(
            news,
            target_date,
            len(rows) + 1,
            section_id,
            section_label,
            group,
            category,
        )
        if should_keep_row(row):
            rows.append(row)


def fetch_impacton_news(target_date, seen_links, seen_titles):
    target_dot = target_date.strftime("%Y.%m.%d")
    impact_news = []
    try:
        parser = scraper.ArticleLinkParser()
        parser.feed(scraper.fetch_text("https://www.impacton.net/news/articleList.html"))
        for title, href in parser.links:
            if len(impact_news) >= scraper.MAX_IMPACT_NEWS:
                break
            if not title or len(title) < 4:
                continue
            link = href if href.startswith("http") else f"https://www.impacton.net{href}"
            if link in seen_links or "pro" in title.lower() or "유료" in title:
                continue
            if any(scraper.is_similar_title(title, st) for st in seen_titles):
                continue

            try:
                article_html = scraper.fetch_text(link)
                if scraper.extract_impact_date(article_html) != target_dot:
                    continue
                soup = BeautifulSoup(article_html, "html.parser")
                if not scraper.is_allowed_impacton_section(soup):
                    continue
                body = scraper.extract_best_article_text(soup)
                seen_links.add(link)
                seen_titles.append(title)
                impact_news.append(
                    {
                        "title": title,
                        "link": link,
                        "date": target_dot,
                        "source": "임팩트온",
                        "summary": scraper.make_three_line_summary(title, body, "임팩트온", "국내 ESG 및 임팩트 비즈니스 뉴스입니다."),
                        "_summary_source": body,
                        "_summary_context": "국내 ESG 및 임팩트 비즈니스 뉴스입니다.",
                    }
                )
            except Exception:
                continue
    except Exception:
        pass
    return impact_news


def collect_raw_articles(options):
    env = scraper.load_env()
    env["AI_SUMMARY_ENABLED"] = "0"
    scraper.configure_summary_generator(env)
    trend_keywords = scraper.load_trend_keywords()

    rows = []
    seen_links, seen_titles = set(), []
    target_date = options.target_date

    if options.include_theme:
        strong_theme = scraper.fetch_strong_theme()
        append_news_rows(
            rows,
            strong_theme.get("news", []),
            target_date,
            "theme",
            "강세 테마",
            strong_theme.get("name", "강세 테마"),
            "테마 관련 최신 뉴스",
        )

    global_impact = scraper.fetch_global_impact(target_date, seen_links, seen_titles)
    trellis_news = scraper.fetch_trellis_news(target_date, seen_links, seen_titles)
    causeartist_news = scraper.fetch_causeartist_news(target_date, seen_links, seen_titles)
    socialimpact_news = scraper.fetch_sitemap_news_source(
        "소셜임팩트뉴스",
        "https://www.socialimpactnews.net/sitemap.xml",
        target_date,
        seen_links,
        seen_titles,
        "Korean social impact and mission-driven business news.",
        limit=10,
        delay_seconds=2.0,
    )
    eroun_news = scraper.fetch_sitemap_news_source(
        "이로운넷",
        "https://www.eroun.net/sitemap.xml",
        target_date,
        seen_links,
        seen_titles,
        "Korean social economy and impact ecosystem news.",
        limit=10,
        delay_seconds=2.0,
    )
    newsletter_news = []
    if options.include_newsletters:
        gmail_user = env.get("GMAIL_USER", "")
        gmail_password = env.get("GMAIL_APP_PASSWORD", "")
        if gmail_user and gmail_password:
            print("\n[Agent A] 뉴스레터 원문 수집 중...")
            newsletter_news = scraper.fetch_newsletter_emails(gmail_user, gmail_password, target_date, seen_links, seen_titles)

    impacton_news = fetch_impacton_news(target_date, seen_links, seen_titles)
    all_impact = impacton_news + global_impact + trellis_news + causeartist_news + socialimpact_news + eroun_news + newsletter_news
    for news in all_impact:
        group = "국내" if scraper.is_domestic_news(news.get("title", ""), news.get("summary", []), news.get("source", "")) else "글로벌"
        append_news_rows(rows, [news], target_date, "impact", "임팩트", group, news.get("source", ""))

    search_sections = scraper.fetch_search_sections(target_date, seen_links, seen_titles, trend_keywords)
    for section in search_sections:
        for group in section.get("groups", []):
            for category in group.get("categories", []):
                append_news_rows(
                    rows,
                    category.get("news", []),
                    target_date,
                    section.get("id", ""),
                    section.get("label", ""),
                    group.get("title", ""),
                    category.get("name", ""),
                )

    return rows


def write_jsonl(rows, path):
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8", newline="\n") as f:
        for row in rows:
            f.write(json.dumps(row, ensure_ascii=False) + "\n")


def write_csv(rows, path):
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8-sig", newline="") as f:
        writer = csv.DictWriter(f, fieldnames=RAW_ARTICLE_COLUMNS, extrasaction="ignore")
        writer.writeheader()
        writer.writerows(rows)


def excel_col_name(index):
    name = ""
    while index:
        index, remainder = divmod(index - 1, 26)
        name = chr(65 + remainder) + name
    return name


def xml_escape(value):
    text = str(value if value is not None else "")
    text = re.sub(r"[\x00-\x08\x0b\x0c\x0e-\x1f]", "", text)
    if len(text) > EXCEL_CELL_CHAR_LIMIT:
        text = text[: EXCEL_CELL_CHAR_LIMIT - 20] + " [truncated for Excel]"
    return (
        text.replace("&", "&amp;")
        .replace("<", "&lt;")
        .replace(">", "&gt;")
        .replace('"', "&quot;")
        .replace("'", "&apos;")
    )


def write_xlsx(rows, path):
    path.parent.mkdir(parents=True, exist_ok=True)
    matrix = [RAW_ARTICLE_COLUMNS] + [[row.get(col, "") for col in RAW_ARTICLE_COLUMNS] for row in rows]
    shared_strings = []
    shared_index = {}
    shared_string_ref_count = 0

    def shared_string_id(value):
        text = str(value if value is not None else "")
        if text not in shared_index:
            shared_index[text] = len(shared_strings)
            shared_strings.append(text)
        return shared_index[text]

    row_xml = []
    for row_idx, row in enumerate(matrix, 1):
        cells = []
        for col_idx, value in enumerate(row, 1):
            cell_ref = f"{excel_col_name(col_idx)}{row_idx}"
            style = ' s="1"' if row_idx == 1 else ' s="2"'
            if RAW_ARTICLE_COLUMNS[col_idx - 1] == "text_char_count" and row_idx > 1:
                cells.append(f'<c r="{cell_ref}"{style}><v>{int(value or 0)}</v></c>')
            else:
                shared_string_ref_count += 1
                sid = shared_string_id(value)
                cells.append(f'<c r="{cell_ref}" t="s"{style}><v>{sid}</v></c>')
        row_xml.append(f'<row r="{row_idx}">{"".join(cells)}</row>')

    shared_items = "".join(
        f'<si><t xml:space="preserve">{xml_escape(value)}</t></si>' for value in shared_strings
    )
    last_cell = f"{excel_col_name(len(RAW_ARTICLE_COLUMNS))}{len(matrix)}"
    sheet_xml = f'''<?xml version="1.0" encoding="UTF-8" standalone="yes"?>
<worksheet xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main" xmlns:r="http://schemas.openxmlformats.org/officeDocument/2006/relationships">
  <dimension ref="A1:{last_cell}"/>
  <sheetViews><sheetView workbookViewId="0"><pane ySplit="1" topLeftCell="A2" activePane="bottomLeft" state="frozen"/></sheetView></sheetViews>
  <cols>
    <col min="1" max="1" width="18" customWidth="1"/>
    <col min="2" max="8" width="16" customWidth="1"/>
    <col min="9" max="10" width="42" customWidth="1"/>
    <col min="11" max="11" width="22" customWidth="1"/>
    <col min="12" max="12" width="80" customWidth="1"/>
    <col min="13" max="16" width="18" customWidth="1"/>
  </cols>
  <sheetData>{"".join(row_xml)}</sheetData>
  <autoFilter ref="A1:{last_cell}"/>
</worksheet>'''
    workbook_xml = '''<?xml version="1.0" encoding="UTF-8" standalone="yes"?>
<workbook xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main" xmlns:r="http://schemas.openxmlformats.org/officeDocument/2006/relationships">
  <sheets><sheet name="raw_articles" sheetId="1" r:id="rId1"/></sheets>
</workbook>'''
    workbook_rels = '''<?xml version="1.0" encoding="UTF-8" standalone="yes"?>
<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">
  <Relationship Id="rId1" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/worksheet" Target="worksheets/sheet1.xml"/>
  <Relationship Id="rId2" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/styles" Target="styles.xml"/>
  <Relationship Id="rId3" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/sharedStrings" Target="sharedStrings.xml"/>
</Relationships>'''
    rels = '''<?xml version="1.0" encoding="UTF-8" standalone="yes"?>
<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">
  <Relationship Id="rId1" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/officeDocument" Target="xl/workbook.xml"/>
  <Relationship Id="rId2" Type="http://schemas.openxmlformats.org/package/2006/relationships/metadata/core-properties" Target="docProps/core.xml"/>
  <Relationship Id="rId3" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/extended-properties" Target="docProps/app.xml"/>
</Relationships>'''
    styles = '''<?xml version="1.0" encoding="UTF-8" standalone="yes"?>
<styleSheet xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main">
  <fonts count="2"><font><sz val="11"/><name val="Calibri"/></font><font><b/><sz val="11"/><color rgb="FFFFFFFF"/><name val="Calibri"/></font></fonts>
  <fills count="3"><fill><patternFill patternType="none"/></fill><fill><patternFill patternType="gray125"/></fill><fill><patternFill patternType="solid"><fgColor rgb="FF1F4E79"/><bgColor indexed="64"/></patternFill></fill></fills>
  <borders count="1"><border><left/><right/><top/><bottom/><diagonal/></border></borders>
  <cellStyleXfs count="1"><xf numFmtId="0" fontId="0" fillId="0" borderId="0"/></cellStyleXfs>
  <cellXfs count="3"><xf numFmtId="0" fontId="0" fillId="0" borderId="0" xfId="0"/><xf numFmtId="0" fontId="1" fillId="2" borderId="0" xfId="0" applyFont="1" applyFill="1"/><xf numFmtId="0" fontId="0" fillId="0" borderId="0" xfId="0" applyAlignment="1"><alignment vertical="top" wrapText="1"/></xf></cellXfs>
  <cellStyles count="1"><cellStyle name="Normal" xfId="0" builtinId="0"/></cellStyles>
</styleSheet>'''
    content_types = '''<?xml version="1.0" encoding="UTF-8" standalone="yes"?>
<Types xmlns="http://schemas.openxmlformats.org/package/2006/content-types">
  <Default Extension="rels" ContentType="application/vnd.openxmlformats-package.relationships+xml"/>
  <Default Extension="xml" ContentType="application/xml"/>
  <Override PartName="/xl/workbook.xml" ContentType="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet.main+xml"/>
  <Override PartName="/xl/worksheets/sheet1.xml" ContentType="application/vnd.openxmlformats-officedocument.spreadsheetml.worksheet+xml"/>
  <Override PartName="/xl/styles.xml" ContentType="application/vnd.openxmlformats-officedocument.spreadsheetml.styles+xml"/>
  <Override PartName="/xl/sharedStrings.xml" ContentType="application/vnd.openxmlformats-officedocument.spreadsheetml.sharedStrings+xml"/>
  <Override PartName="/docProps/core.xml" ContentType="application/vnd.openxmlformats-package.core-properties+xml"/>
  <Override PartName="/docProps/app.xml" ContentType="application/vnd.openxmlformats-officedocument.extended-properties+xml"/>
</Types>'''
    core = f'''<?xml version="1.0" encoding="UTF-8" standalone="yes"?>
<cp:coreProperties xmlns:cp="http://schemas.openxmlformats.org/package/2006/metadata/core-properties" xmlns:dc="http://purl.org/dc/elements/1.1/" xmlns:dcterms="http://purl.org/dc/terms/" xmlns:xsi="http://www.w3.org/2001/XMLSchema-instance">
  <dc:creator>Agent A</dc:creator>
  <dc:title>Raw news articles for summarization</dc:title>
  <dcterms:created xsi:type="dcterms:W3CDTF">{datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")}</dcterms:created>
</cp:coreProperties>'''
    app = '''<?xml version="1.0" encoding="UTF-8" standalone="yes"?>
<Properties xmlns="http://schemas.openxmlformats.org/officeDocument/2006/extended-properties" xmlns:vt="http://schemas.openxmlformats.org/officeDocument/2006/docPropsVTypes">
  <Application>Agent A</Application>
</Properties>'''
    shared_xml = f'''<?xml version="1.0" encoding="UTF-8" standalone="yes"?>
<sst xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main" count="{shared_string_ref_count}" uniqueCount="{len(shared_strings)}">{shared_items}</sst>'''

    with zipfile.ZipFile(path, "w", compression=zipfile.ZIP_DEFLATED) as xlsx:
        xlsx.writestr("[Content_Types].xml", content_types)
        xlsx.writestr("_rels/.rels", rels)
        xlsx.writestr("docProps/core.xml", core)
        xlsx.writestr("docProps/app.xml", app)
        xlsx.writestr("xl/workbook.xml", workbook_xml)
        xlsx.writestr("xl/_rels/workbook.xml.rels", workbook_rels)
        xlsx.writestr("xl/worksheets/sheet1.xml", sheet_xml)
        xlsx.writestr("xl/sharedStrings.xml", shared_xml)
        xlsx.writestr("xl/styles.xml", styles)


def write_outputs(rows, options):
    date_key = options.target_date.strftime("%Y-%m-%d")
    output_dir = options.output_dir / date_key
    output_dir.mkdir(parents=True, exist_ok=True)

    jsonl_path = output_dir / f"raw_articles_{date_key}.jsonl"
    csv_path = output_dir / f"raw_articles_{date_key}.csv"
    xlsx_path = output_dir / f"raw_articles_{date_key}.xlsx"

    write_jsonl(rows, jsonl_path)
    write_csv(rows, csv_path)
    if options.write_xlsx:
        write_xlsx(rows, xlsx_path)
    return {
        "jsonl": jsonl_path,
        "csv": csv_path,
        "xlsx": xlsx_path if options.write_xlsx else None,
    }


def print_collection_summary(rows, output_paths):
    quality_counts = {}
    for row in rows:
        quality_counts[row["body_quality"]] = quality_counts.get(row["body_quality"], 0) + 1
    print(f"\n[Agent A] collected articles: {len(rows)}")
    print(f"[Agent A] body quality: {json.dumps(quality_counts, ensure_ascii=False, sort_keys=True)}")
    for label, path in output_paths.items():
        if path:
            print(f"[Agent A] {label}: {path}")


def main():
    args = parse_args()
    options = AgentAOptions(
        target_date=target_date_from_arg(args.date),
        output_dir=Path(args.output_dir).resolve(),
        include_theme=not args.skip_theme,
        include_newsletters=not args.skip_newsletters,
        write_xlsx=not args.no_xlsx,
    )
    rows = collect_raw_articles(options)
    output_paths = write_outputs(rows, options)
    print_collection_summary(rows, output_paths)


if __name__ == "__main__":
    main()
