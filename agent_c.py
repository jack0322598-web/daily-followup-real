import argparse
import csv
import json
import re
import urllib.parse
from collections import Counter
from datetime import datetime, timedelta, timezone
from pathlib import Path


BASE_DIR = Path(__file__).resolve().parent
KST = timezone(timedelta(hours=9))
DEFAULT_AGENT_B_DIR = BASE_DIR / "pipeline_data" / "agent_b"
DEFAULT_REPORT_DIR = BASE_DIR / "pipeline_data" / "agent_c"

TRACKING_QUERY_PREFIXES = ("utm_",)
TRACKING_QUERY_KEYS = {
    "fbclid",
    "gclid",
    "igshid",
    "mc_cid",
    "mc_eid",
}


def normalize_space(value):
    return re.sub(r"\s+", " ", str(value or "")).strip()


def date_key(value):
    if hasattr(value, "strftime"):
        return value.strftime("%Y-%m-%d")
    return normalize_space(value).replace(".", "-")


def normalize_url(value):
    value = normalize_space(value)
    if not value:
        return ""
    try:
        parsed = urllib.parse.urlsplit(value)
    except ValueError:
        return value.lower().rstrip("/")

    scheme = parsed.scheme.lower() or "https"
    netloc = parsed.netloc.lower()
    if netloc.startswith("www."):
        netloc = netloc[4:]
    path = urllib.parse.unquote(parsed.path or "").rstrip("/")
    query_pairs = []
    for key, val in urllib.parse.parse_qsl(parsed.query, keep_blank_values=True):
        key_lower = key.lower()
        if key_lower in TRACKING_QUERY_KEYS or any(key_lower.startswith(prefix) for prefix in TRACKING_QUERY_PREFIXES):
            continue
        query_pairs.append((key_lower, val))
    query = urllib.parse.urlencode(sorted(query_pairs))
    return urllib.parse.urlunsplit((scheme, netloc, path, query, ""))


def normalize_title_key(value):
    value = normalize_space(value).lower()
    return re.sub(r"[\s\"'“”‘’.,:;!?()\[\]{}<>|/\\_-]+", "", value)


def normalize_source_key(value):
    value = normalize_space(value).lower()
    return re.sub(r"[\s\"'“”‘’.,:;!?()\[\]{}<>|/\\_-]+", "", value)


def title_source_key(title, source):
    title_key = normalize_title_key(title)
    source_key = normalize_source_key(source)
    return f"{source_key}::{title_key}" if title_key and source_key else ""


def resolve_summary_path(target_date, summary_path=None, agent_b_dir=DEFAULT_AGENT_B_DIR):
    if summary_path:
        return Path(summary_path).resolve()
    key = date_key(target_date)
    base = Path(agent_b_dir).resolve() / key
    for suffix in ("jsonl", "csv"):
        candidate = base / f"summaries_{key}.{suffix}"
        if candidate.exists():
            return candidate
    return base / f"summaries_{key}.jsonl"


def load_jsonl(path):
    rows = []
    with Path(path).open("r", encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if line:
                rows.append(json.loads(line))
    return rows


def load_csv(path):
    with Path(path).open("r", encoding="utf-8-sig", newline="") as f:
        return list(csv.DictReader(f))


def load_agent_b_summaries(path):
    path = Path(path)
    if not path.exists():
        return []
    if path.suffix.lower() == ".jsonl":
        return load_jsonl(path)
    if path.suffix.lower() == ".csv":
        return load_csv(path)
    raise ValueError(f"Unsupported Agent B summary file: {path.suffix}")


def summary_lines_from_row(row):
    lines = [normalize_space(row.get(f"summary_line_{idx}", "")) for idx in range(1, 4)]
    return [line for line in lines if line]


def summary_score(row):
    status_score = {"ai": 3, "fallback": 1}.get(normalize_space(row.get("summary_status", "")).lower(), 0)
    confidence_score = {"high": 3, "medium": 2, "low": 1}.get(normalize_space(row.get("summary_confidence", "")).lower(), 0)
    return status_score, confidence_score, normalize_space(row.get("summarized_at", ""))


def prefer_summary(current, candidate):
    if not current:
        return candidate
    return candidate if summary_score(candidate) > summary_score(current) else current


def build_summary_index(rows):
    by_url = {}
    by_title_source = {}
    valid_rows = []
    skipped_rows = []

    for row in rows:
        lines = summary_lines_from_row(row)
        if len(lines) != 3:
            skipped_rows.append(row)
            continue

        row = {**row, "_agent_c_lines": lines}
        valid_rows.append(row)

        url_key = normalize_url(row.get("url", ""))
        if url_key:
            by_url[url_key] = prefer_summary(by_url.get(url_key), row)

        ts_key = title_source_key(row.get("title", ""), row.get("source", ""))
        if ts_key:
            by_title_source[ts_key] = prefer_summary(by_title_source.get(ts_key), row)

    return {
        "by_url": by_url,
        "by_title_source": by_title_source,
        "valid_rows": valid_rows,
        "skipped_rows": skipped_rows,
    }


def iter_news_items(strong_theme, domestic_impact, global_impact, search_sections):
    seen = set()

    def yield_once(news_item):
        if not isinstance(news_item, dict):
            return
        object_id = id(news_item)
        if object_id in seen:
            return
        seen.add(object_id)
        yield news_item

    for news in (strong_theme or {}).get("news", []):
        yield from yield_once(news)
    for news in domestic_impact or []:
        yield from yield_once(news)
    for news in global_impact or []:
        yield from yield_once(news)
    for section in search_sections or []:
        for group in section.get("groups", []):
            for category in group.get("categories", []):
                for news in category.get("news", []):
                    yield from yield_once(news)


def match_summary(news, index):
    url_key = normalize_url(news.get("link") or news.get("url", ""))
    if url_key and url_key in index["by_url"]:
        return index["by_url"][url_key], "url"

    ts_key = title_source_key(news.get("title", ""), news.get("source", ""))
    if ts_key and ts_key in index["by_title_source"]:
        return index["by_title_source"][ts_key], "title_source"

    return None, ""


def apply_summary(news, row, match_type):
    lines = list(row.get("_agent_c_lines") or summary_lines_from_row(row))
    news["summary"] = lines
    news["_summary_mode"] = f"agent-b:{row.get('summary_status', '')}:{row.get('summary_model', '')}"
    news["_summary_confidence"] = row.get("summary_confidence", "")
    news["_agent_b_article_id"] = row.get("article_id", "")
    news["_agent_b_match_type"] = match_type
    news["_agent_b_summary_error"] = row.get("summary_error", "")


def write_report(report, report_dir=DEFAULT_REPORT_DIR):
    key = report.get("date") or "unknown-date"
    path = Path(report_dir).resolve() / key / f"agent_c_report_{key}.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    return path


def apply_agent_b_summaries(
    target_date,
    strong_theme,
    domestic_impact,
    global_impact,
    search_sections,
    summary_path=None,
    agent_b_dir=DEFAULT_AGENT_B_DIR,
    report_dir=DEFAULT_REPORT_DIR,
    write_report_file=True,
):
    key = date_key(target_date)
    path = resolve_summary_path(target_date, summary_path, agent_b_dir)
    report = {
        "date": key,
        "summary_path": str(path),
        "summary_file_exists": path.exists(),
        "summary_rows": 0,
        "valid_summary_rows": 0,
        "skipped_summary_rows": 0,
        "news_items": 0,
        "applied": 0,
        "match_counts": {},
        "summary_status_counts": {},
        "unmatched_summary_rows": [],
        "unmatched_news": [],
        "report_path": "",
    }

    if not path.exists():
        if write_report_file:
            report["report_path"] = str(write_report(report, report_dir))
        return report

    rows = load_agent_b_summaries(path)
    index = build_summary_index(rows)
    report["summary_rows"] = len(rows)
    report["valid_summary_rows"] = len(index["valid_rows"])
    report["skipped_summary_rows"] = len(index["skipped_rows"])
    report["summary_status_counts"] = dict(Counter(normalize_space(row.get("summary_status", "")) for row in index["valid_rows"]))

    matched_article_ids = set()
    match_counts = Counter()
    unmatched_news = []
    news_items = list(iter_news_items(strong_theme, domestic_impact, global_impact, search_sections))

    for news in news_items:
        row, match_type = match_summary(news, index)
        if not row:
            unmatched_news.append({
                "title": normalize_space(news.get("title", "")),
                "source": normalize_space(news.get("source", "")),
                "link": normalize_space(news.get("link") or news.get("url", "")),
            })
            continue
        apply_summary(news, row, match_type)
        matched_article_ids.add(row.get("article_id", ""))
        match_counts[match_type] += 1

    unmatched_rows = []
    for row in index["valid_rows"]:
        article_id = row.get("article_id", "")
        if article_id not in matched_article_ids:
            unmatched_rows.append({
                "article_id": article_id,
                "title": normalize_space(row.get("title", "")),
                "source": normalize_space(row.get("source", "")),
                "url": normalize_space(row.get("url", "")),
                "summary_status": normalize_space(row.get("summary_status", "")),
            })

    report["news_items"] = len(news_items)
    report["applied"] = sum(match_counts.values())
    report["match_counts"] = dict(match_counts)
    report["unmatched_summary_rows"] = unmatched_rows[:100]
    report["unmatched_news"] = unmatched_news[:100]
    if write_report_file:
        report["report_path"] = str(write_report(report, report_dir))
    return report


def parse_args():
    parser = argparse.ArgumentParser(description="Agent C: validate Agent B summaries for webpage rendering.")
    parser.add_argument("--date", "--news-date", dest="date", help="Target news date (YYYY-MM-DD). Defaults to yesterday in KST.")
    parser.add_argument("--summaries", dest="summary_path", help="Agent B summary file (.jsonl or .csv).")
    parser.add_argument("--agent-b-dir", default=str(DEFAULT_AGENT_B_DIR), help="Base Agent B output directory.")
    return parser.parse_args()


def target_date_from_arg(date_arg):
    if date_arg:
        return datetime.strptime(date_arg.strip().replace(".", "-"), "%Y-%m-%d").date()
    return (datetime.now(KST) - timedelta(days=1)).date()


def main():
    args = parse_args()
    target_date = target_date_from_arg(args.date)
    path = resolve_summary_path(target_date, args.summary_path, Path(args.agent_b_dir))
    rows = load_agent_b_summaries(path) if path.exists() else []
    index = build_summary_index(rows)
    print(f"[Agent C] summaries: {path}")
    print(f"[Agent C] rows: {len(rows)}")
    print(f"[Agent C] valid rows: {len(index['valid_rows'])}")
    print(f"[Agent C] skipped rows: {len(index['skipped_rows'])}")
    print(f"[Agent C] status: {json.dumps(dict(Counter(row.get('summary_status', '') for row in index['valid_rows'])), ensure_ascii=False, sort_keys=True)}")


if __name__ == "__main__":
    main()
