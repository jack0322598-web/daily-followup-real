import argparse
import csv
import json
import random
import re
import time
import urllib.error
import urllib.request
import zipfile
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from xml.etree import ElementTree

import agent_a
import main as scraper


BASE_DIR = Path(__file__).resolve().parent
DEFAULT_AGENT_A_DIR = BASE_DIR / "pipeline_data" / "agent_a"
DEFAULT_OUTPUT_DIR = BASE_DIR / "pipeline_data" / "agent_b"
KST = scraper.KST
SUMMARY_INPUT_MAX_CHARS = 9000
DEFAULT_BATCH_SIZE = 2
DEFAULT_RETRY_ATTEMPTS = 2
DEFAULT_RETRY_BASE_DELAY = 20.0
DEFAULT_INTER_BATCH_DELAY = 12.0
DEFAULT_PRIMARY_MODEL = "gemini-2.5-flash-lite"
DEFAULT_FALLBACK_MODELS = "none"
DEFAULT_OPENAI_MODEL = "gpt-5-mini"

SUMMARY_COLUMNS = [
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
    "text_char_count",
    "body_quality",
    "summary_context",
    "summary_line_1",
    "summary_line_2",
    "summary_line_3",
    "summary_text",
    "summary_model",
    "summary_status",
    "summary_confidence",
    "summary_error",
    "summarized_at",
]


@dataclass
class AgentBOptions:
    target_date: object
    input_path: Path
    output_dir: Path
    model: str
    batch_size: int = DEFAULT_BATCH_SIZE
    limit: int = 0
    fallback_only: bool = False
    write_xlsx: bool = True
    retry_attempts: int = DEFAULT_RETRY_ATTEMPTS
    retry_base_delay: float = DEFAULT_RETRY_BASE_DELAY
    inter_batch_delay: float = DEFAULT_INTER_BATCH_DELAY
    fallback_models: str = DEFAULT_FALLBACK_MODELS
    openai_model: str = DEFAULT_OPENAI_MODEL
    disable_openai_fallback: bool = True


def parse_args():
    parser = argparse.ArgumentParser(
        description="Agent B: summarize Agent A's cleaned article text into strict three-line summaries."
    )
    parser.add_argument("--date", "--news-date", dest="date", help="Target news date (YYYY-MM-DD). Defaults to yesterday in KST.")
    parser.add_argument("--input", dest="input_path", help="Agent A input file (.jsonl, .csv, or .xlsx).")
    parser.add_argument("--agent-a-dir", default=str(DEFAULT_AGENT_A_DIR), help="Base Agent A output directory.")
    parser.add_argument("--output-dir", default=str(DEFAULT_OUTPUT_DIR), help="Base output directory for Agent B files.")
    parser.add_argument("--model", default="", help=f"Gemini model override. Defaults to AGENT_B_GEMINI_MODEL/{DEFAULT_PRIMARY_MODEL}.")
    parser.add_argument("--batch-size", type=int, default=DEFAULT_BATCH_SIZE, help="Number of articles per Gemini request.")
    parser.add_argument("--limit", type=int, default=0, help="Summarize only the first N articles for testing.")
    parser.add_argument("--fallback-only", action="store_true", help="Skip Gemini and use extractive fallback summaries.")
    parser.add_argument("--no-xlsx", action="store_true", help="Write JSONL/CSV only.")
    parser.add_argument("--retry-attempts", type=int, default=DEFAULT_RETRY_ATTEMPTS, help="Gemini retry attempts per batch for rate limits/transient errors.")
    parser.add_argument("--retry-base-delay", type=float, default=DEFAULT_RETRY_BASE_DELAY, help="Initial retry delay in seconds. Exponential backoff is applied.")
    parser.add_argument("--inter-batch-delay", type=float, default=DEFAULT_INTER_BATCH_DELAY, help="Seconds to wait between successful Gemini batches.")
    parser.add_argument("--fallback-models", default=DEFAULT_FALLBACK_MODELS, help="Deprecated compatibility option. Agent B uses one Gemini model pass.")
    parser.add_argument("--openai-model", default="", help=f"OpenAI model for Gemini-failed articles. Defaults to OPENAI_SUMMARY_MODEL/{DEFAULT_OPENAI_MODEL}.")
    parser.add_argument("--enable-openai-fallback", action="store_false", dest="disable_openai_fallback", default=True, help="Explicitly enable OpenAI fallback. Disabled by default.")
    return parser.parse_args()


def target_date_from_arg(date_arg):
    if date_arg:
        return datetime.strptime(date_arg.strip().replace(".", "-"), "%Y-%m-%d").date()
    return (datetime.now(KST) - timedelta(days=1)).date()


def normalize_text(text):
    return scraper.normalize_space(str(text or ""))


def latest_agent_a_date(agent_a_dir):
    if not agent_a_dir.exists():
        return None
    candidates = []
    for child in agent_a_dir.iterdir():
        if child.is_dir():
            try:
                candidates.append(datetime.strptime(child.name, "%Y-%m-%d").date())
            except Exception:
                pass
    return max(candidates) if candidates else None


def resolve_input_path(args, target_date):
    if args.input_path:
        return Path(args.input_path).resolve()
    agent_a_dir = Path(args.agent_a_dir).resolve()
    date_key = target_date.strftime("%Y-%m-%d")
    candidates = [
        agent_a_dir / date_key / f"raw_articles_{date_key}.jsonl",
        agent_a_dir / date_key / f"raw_articles_{date_key}.csv",
        agent_a_dir / date_key / f"raw_articles_{date_key}.xlsx",
    ]
    for candidate in candidates:
        if candidate.exists():
            return candidate
    latest = latest_agent_a_date(agent_a_dir)
    if latest:
        latest_key = latest.strftime("%Y-%m-%d")
        latest_candidate = agent_a_dir / latest_key / f"raw_articles_{latest_key}.jsonl"
        if latest_candidate.exists():
            return latest_candidate
    return candidates[0]


def load_jsonl(path):
    rows = []
    with path.open("r", encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if line:
                rows.append(json.loads(line))
    return rows


def load_csv(path):
    with path.open("r", encoding="utf-8-sig", newline="") as f:
        return list(csv.DictReader(f))


def cell_col_index(cell_ref):
    letters = "".join(ch for ch in cell_ref if ch.isalpha())
    index = 0
    for ch in letters:
        index = index * 26 + (ord(ch.upper()) - 64)
    return index - 1


def load_xlsx(path):
    with zipfile.ZipFile(path) as xlsx:
        shared = []
        if "xl/sharedStrings.xml" in xlsx.namelist():
            root = ElementTree.fromstring(xlsx.read("xl/sharedStrings.xml"))
            ns = {"m": "http://schemas.openxmlformats.org/spreadsheetml/2006/main"}
            for si in root.findall("m:si", ns):
                shared.append("".join(t.text or "" for t in si.findall(".//m:t", ns)))
        sheet = ElementTree.fromstring(xlsx.read("xl/worksheets/sheet1.xml"))

    ns = {"m": "http://schemas.openxmlformats.org/spreadsheetml/2006/main"}
    matrix = []
    for row in sheet.findall(".//m:sheetData/m:row", ns):
        values = []
        for cell in row.findall("m:c", ns):
            col_idx = cell_col_index(cell.attrib.get("r", "A1"))
            while len(values) <= col_idx:
                values.append("")
            value_node = cell.find("m:v", ns)
            raw_value = value_node.text if value_node is not None else ""
            if cell.attrib.get("t") == "s" and raw_value != "":
                values[col_idx] = shared[int(raw_value)]
            else:
                values[col_idx] = raw_value
        matrix.append(values)

    if not matrix:
        return []
    headers = [normalize_text(value) for value in matrix[0]]
    rows = []
    for values in matrix[1:]:
        row = {}
        for idx, header in enumerate(headers):
            if header:
                row[header] = values[idx] if idx < len(values) else ""
        rows.append(row)
    return rows


def load_agent_a_rows(path):
    if not path.exists():
        raise FileNotFoundError(f"Agent A input file not found: {path}")
    suffix = path.suffix.lower()
    if suffix == ".jsonl":
        return load_jsonl(path)
    if suffix == ".csv":
        return load_csv(path)
    if suffix == ".xlsx":
        return load_xlsx(path)
    raise ValueError(f"Unsupported Agent A input file type: {path.suffix}")


def clean_summary_line(line):
    line = normalize_text(line)
    line = re.sub(r"^\s*[-*•\d.)]+\s*", "", line).strip()
    line = re.sub(r"^【[^】]{1,90}】\s*", "", line)
    line = line.strip("\"'` ")
    return line


def has_formal_korean_ending(line):
    return bool(re.search(r"[가-힣]+(?:니다|입니다|습니다)\.$", line or ""))


def has_hangul(text):
    return bool(re.search(r"[가-힣]", text or ""))


def is_likely_english_article(row):
    sample = " ".join(
        [
            str(row.get("title", "")),
            str(row.get("source", "")),
            str(row.get("source_domain", "")),
            str(row.get("original_text", ""))[:2500],
        ]
    )
    hangul_count = len(re.findall(r"[가-힣]", sample))
    latin_count = len(re.findall(r"[A-Za-z]", sample))
    return latin_count >= 120 and latin_count > hangul_count * 3


def enforce_formal_summary_style(line, add_english_suffix=True):
    line = clean_summary_line(line)
    if not line:
        return line
    if not has_hangul(line):
        stripped = line.strip()
        if len(stripped) > 165:
            stripped = stripped[:165].rstrip(" ,.;:，。")
        if not add_english_suffix:
            return stripped if re.search(r"[.!?]$", stripped) else stripped + "."
        stripped = stripped.rstrip(" .。")
        return f"{stripped}라고 설명합니다."

    stripped = line.rstrip(" .。")
    replacements = [
        (r"하였다$", "했습니다"),
        (r"했다$", "했습니다"),
        (r"되었다$", "됐습니다"),
        (r"됐다$", "됐습니다"),
        (r"이었다$", "이었습니다"),
        (r"였다$", "였습니다"),
        (r"어졌다$", "어졌습니다"),
        (r"아졌다$", "아졌습니다"),
        (r"졌다$", "졌습니다"),
        (r"왔다$", "왔습니다"),
        (r"갔다$", "갔습니다"),
        (r"났다$", "났습니다"),
        (r"었다$", "었습니다"),
        (r"았다$", "았습니다"),
        (r"된다$", "됩니다"),
        (r"한다$", "합니다"),
        (r"이다$", "입니다"),
        (r"있다$", "있습니다"),
        (r"없다$", "없습니다"),
        (r"보인다$", "보입니다"),
        (r"전망된다$", "전망됩니다"),
        (r"예상된다$", "예상됩니다"),
        (r"분석된다$", "분석됩니다"),
        (r"평가된다$", "평가됩니다"),
        (r"확인됐다$", "확인됐습니다"),
        (r"나타났다$", "나타났습니다"),
        (r"밝혔다$", "밝혔습니다"),
        (r"전했다$", "전했습니다"),
        (r"말했다$", "말했습니다"),
        (r"설명했다$", "설명했습니다"),
        (r"강조했다$", "강조했습니다"),
        (r"공개했다$", "공개했습니다"),
        (r"발표했다$", "발표했습니다"),
        (r"추진한다$", "추진합니다"),
        (r"가능하다$", "가능합니다"),
        (r"필요하다$", "필요합니다"),
        (r"어렵다$", "어렵습니다"),
        (r"높다$", "높습니다"),
        (r"낮다$", "낮습니다"),
        (r"크다$", "큽니다"),
    ]
    replaced = False
    for pattern, replacement in replacements:
        if re.search(pattern, stripped):
            stripped = re.sub(pattern, replacement, stripped)
            replaced = True
            break
    if not replaced and stripped.endswith("다") and not has_formal_korean_ending(stripped + "."):
        stripped = f"{stripped}고 정리됩니다"

    line = stripped if stripped.endswith(".") else stripped + "."
    if not has_formal_korean_ending(line):
        stripped = stripped.rstrip(" .。…").strip()
        line = f"{stripped}입니다."
    if len(line) > 180:
        line = line[:174].rstrip(" ,.;:，。") + "입니다."
    return line


def normalize_summary_lines(value, add_english_suffix=True):
    if isinstance(value, dict):
        value = value.get("summary") or value.get("lines")
    if isinstance(value, str):
        value = [part for part in re.split(r"\n+|(?<=다\.)\s+", value) if part.strip()]
    if not isinstance(value, list):
        return []

    lines = []
    seen = set()
    for item in value:
        line = enforce_formal_summary_style(str(item), add_english_suffix=add_english_suffix)
        if not line or len(line) < 10:
            continue
        key = line.casefold()
        if key in seen:
            continue
        seen.add(key)
        lines.append(line)
        if len(lines) == 3:
            break
    return lines


def fallback_summary(row):
    summary_text = clean_text_for_summary(row.get("original_text", ""))
    lines = scraper.make_extractive_three_line_summary(
        row.get("title", ""),
        summary_text,
        row.get("source", ""),
        row.get("summary_context", ""),
    )
    return normalize_summary_lines(lines, add_english_suffix=not is_likely_english_article(row))


def clean_text_for_summary(text):
    text = normalize_text(text)
    text = re.sub(r"^【[^】]{1,90}】\s*", "", text)
    text = re.sub(r"^\([^)]*=연합뉴스\)\s*[^=]{1,90}?기자\s*=\s*", "", text)
    text = re.sub(r"^[가-힣A-Za-z0-9 .·_-]{2,40}\s*=\s*[^=]{1,70}?기자\s*=\s*", "", text)
    text = re.sub(r"\s+[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}\s*$", "", text)
    return normalize_text(text)


def fit_article_text(text):
    text = normalize_text(text)
    if len(text) <= SUMMARY_INPUT_MAX_CHARS:
        return text
    head_len = int(SUMMARY_INPUT_MAX_CHARS * 0.72)
    tail_len = SUMMARY_INPUT_MAX_CHARS - head_len - 36
    return normalize_text(f"{text[:head_len]} [...중략...] {text[-tail_len:]}")


def build_summary_prompt(items):
    payload = [
        {
            "article_id": item["article_id"],
            "source": item.get("source", ""),
            "category": item.get("category", ""),
            "context": item.get("summary_context", ""),
            "title": item.get("title", ""),
            "body": fit_article_text(clean_text_for_summary(item.get("original_text", ""))),
        }
        for item in items
    ]
    return f"""
너는 뉴스레터 편집자다. 아래 기사 원문만 근거로 각 기사를 정확히 3줄로 요약한다.

규칙:
- 반드시 JSON만 출력한다.
- 스키마: {{"items":[{{"article_id":"...", "summary":["1줄","2줄","3줄"], "confidence":"high|medium|low"}}]}}
- 입력된 모든 article_id에 대해 결과를 하나씩 반환한다.
- 각 summary는 정확히 3개 문장 또는 절로 구성한다.
- 모든 summary 문장은 반드시 "-습니다." 또는 "-입니다." 계열의 격식체로 끝내며, "-다." 평서체를 쓰지 않는다.
- 기사에 없는 사실, 전망, 수치, 인과관계를 새로 만들지 않는다.
- 제목을 그대로 반복하지 말고, 본문에서 확인되는 핵심 사건, 배경, 의미를 나눠 쓴다.
- 기자 이름, 이메일, 구독 안내, 공유 문구, 관련기사 안내 등 메타 문구는 요약하지 않는다.
- 본문이 부족하면 확인 가능한 내용만 요약하고 confidence를 low로 둔다.
- 한국어 기사는 한국어로, 영어 기사는 자연스러운 한국어로 요약한다.

기사 목록:
{json.dumps(payload, ensure_ascii=False)}
""".strip()


def parse_batch_result(parsed):
    if not isinstance(parsed, dict):
        return {}
    items = parsed.get("items") or parsed.get("results") or parsed.get("summaries") or []
    if isinstance(items, dict):
        items = [{"article_id": key, "summary": value} for key, value in items.items()]
    result = {}
    for item in items:
        if not isinstance(item, dict):
            continue
        article_id = normalize_text(item.get("article_id") or item.get("id"))
        lines = normalize_summary_lines(item)
        if article_id and lines:
            result[article_id] = {
                "lines": lines,
                "confidence": normalize_text(item.get("confidence", "")) or "medium",
            }
    return result


def is_retryable_gemini_error(exc):
    if isinstance(exc, urllib.error.HTTPError):
        return exc.code in {429, 500, 502, 503, 504}
    error_text = str(exc)
    return any(code in error_text for code in ["429", "500", "502", "503", "504"])


def is_gemini_rate_limit_error(exc):
    if isinstance(exc, urllib.error.HTTPError):
        return exc.code == 429
    return "429" in str(exc) or "RESOURCE_EXHAUSTED" in str(exc).upper()


def retry_delay_seconds(exc, attempt, base_delay):
    retry_after = ""
    if isinstance(exc, urllib.error.HTTPError):
        retry_after = exc.headers.get("Retry-After", "")
    if retry_after:
        try:
            return max(float(retry_after), base_delay)
        except ValueError:
            pass
    jitter = random.uniform(0, min(5.0, base_delay))
    return min(240.0, base_delay * (2 ** attempt) + jitter)


def gemini_model_candidates(env, primary_model, fallback_models):
    fallback_models = fallback_models or env.get("GEMINI_FALLBACK_MODELS", DEFAULT_FALLBACK_MODELS)
    if fallback_models.strip().lower() in {"", "none", "off", "false", "0"}:
        return [primary_model]
    candidates = [primary_model]
    candidates.extend(part.strip() for part in re.split(r"[,;\s]+", fallback_models) if part.strip())
    return candidates


def summarize_with_gemini(rows, env, model, batch_size, retry_attempts, retry_base_delay, inter_batch_delay):
    api_key = env.get("GEMINI_API_KEY", "")
    if not api_key:
        return {}, "missing GEMINI_API_KEY"
    results = {}
    errors = {}
    for start in range(0, len(rows), batch_size):
        batch = rows[start : start + batch_size]
        prompt = build_summary_prompt(batch)
        for attempt in range(max(1, retry_attempts)):
            try:
                parsed = scraper.call_gemini_json(api_key, model, prompt, timeout=90)
                batch_result = parse_batch_result(parsed)
                for row in batch:
                    article_id = row["article_id"]
                    if article_id in batch_result:
                        results[article_id] = {
                            **batch_result[article_id],
                            "model": model,
                        }
                    else:
                        errors[article_id] = "missing summary in Gemini response"
                if inter_batch_delay > 0:
                    time.sleep(inter_batch_delay)
                break
            except Exception as exc:
                error_text = format_exception_message(exc)
                if is_gemini_rate_limit_error(exc):
                    for row in rows[start:]:
                        errors[row["article_id"]] = error_text
                    print(
                        f"  - Gemini 429 circuit breaker: rows {start + 1}-{len(rows)} "
                        "즉시 규칙 기반 요약으로 전환"
                    )
                    return results, errors
                should_retry = is_retryable_gemini_error(exc) and attempt < max(1, retry_attempts) - 1
                if should_retry:
                    delay = retry_delay_seconds(exc, attempt, retry_base_delay)
                    print(
                        f"  - Gemini retry {attempt + 1}/{retry_attempts} "
                        f"for rows {start + 1}-{start + len(batch)} after {delay:.1f}s: {error_text}"
                    )
                    time.sleep(delay)
                    continue
                for row in batch:
                    errors[row["article_id"]] = error_text
                break
    return results, errors


def is_retryable_openai_error(exc):
    if isinstance(exc, urllib.error.HTTPError):
        return exc.code in {429, 500, 502, 503, 504}
    error_text = str(exc)
    return any(code in error_text for code in ["429", "500", "502", "503", "504"])


def format_exception_message(exc):
    if isinstance(exc, urllib.error.HTTPError):
        try:
            body = exc.read().decode("utf-8", errors="replace")
        except Exception:
            body = ""
        body = normalize_text(body)
        if body:
            return f"HTTP {exc.code}: {body[:700]}"
        return f"HTTP {exc.code}: {exc.reason}"
    return str(exc)


def extract_openai_output_text(data):
    if isinstance(data.get("output_text"), str) and data["output_text"].strip():
        return data["output_text"]

    texts = []
    for output_item in data.get("output", []) or []:
        if not isinstance(output_item, dict):
            continue
        for content_item in output_item.get("content", []) or []:
            if isinstance(content_item, dict):
                text = content_item.get("text") or content_item.get("output_text")
                if text:
                    texts.append(str(text))
            elif isinstance(content_item, str):
                texts.append(content_item)

    # This keeps the parser compatible if the endpoint is swapped to chat/completions later.
    for choice in data.get("choices", []) or []:
        if not isinstance(choice, dict):
            continue
        message = choice.get("message") or {}
        if isinstance(message, dict) and message.get("content"):
            texts.append(str(message["content"]))

    return "\n".join(texts).strip()


def call_openai_json(api_key, model, prompt, timeout=120):
    body = {
        "model": model,
        "instructions": (
            "You are a careful Korean newsletter editor. "
            "Return only valid JSON that matches the requested schema."
        ),
        "input": prompt,
        "text": {"format": {"type": "json_object"}},
        "max_output_tokens": 1800,
        "store": False,
    }
    request = urllib.request.Request(
        "https://api.openai.com/v1/responses",
        data=json.dumps(body, ensure_ascii=False).encode("utf-8"),
        headers={
            **scraper.HEADERS,
            "Authorization": f"Bearer {api_key}",
            "Content-Type": "application/json",
        },
        method="POST",
    )
    with urllib.request.urlopen(request, timeout=timeout) as response:
        data = json.loads(response.read().decode("utf-8", errors="replace"))
    text = extract_openai_output_text(data)
    if not text:
        raise ValueError("OpenAI response did not include output text")
    parsed = scraper.extract_json_payload(text)
    if not parsed:
        raise ValueError("OpenAI response did not contain a JSON payload")
    return parsed


def summarize_with_openai(rows, env, model, batch_size, retry_attempts, retry_base_delay, inter_batch_delay):
    api_key = env.get("OPENAI_API_KEY", "").strip()
    if not api_key:
        return {}, "missing OPENAI_API_KEY"
    results = {}
    errors = {}
    for start in range(0, len(rows), batch_size):
        batch = rows[start : start + batch_size]
        prompt = build_summary_prompt(batch)
        for attempt in range(max(1, retry_attempts)):
            try:
                parsed = call_openai_json(api_key, model, prompt, timeout=120)
                batch_result = parse_batch_result(parsed)
                for row in batch:
                    article_id = row["article_id"]
                    if article_id in batch_result:
                        results[article_id] = {
                            **batch_result[article_id],
                            "model": f"openai:{model}",
                        }
                    else:
                        errors[article_id] = "missing summary in OpenAI response"
                if inter_batch_delay > 0:
                    time.sleep(inter_batch_delay)
                break
            except Exception as exc:
                error_text = format_exception_message(exc)
                should_retry = is_retryable_openai_error(exc) and attempt < max(1, retry_attempts) - 1
                if should_retry:
                    delay = retry_delay_seconds(exc, attempt, retry_base_delay)
                    print(
                        f"  - OpenAI retry {attempt + 1}/{retry_attempts} "
                        f"for rows {start + 1}-{start + len(batch)} after {delay:.1f}s: {error_text}"
                    )
                    time.sleep(delay)
                    continue
                for row in batch:
                    errors[row["article_id"]] = error_text
                break
    return results, errors


def combine_summary_errors(*labeled_errors):
    parts = []
    for label, error in labeled_errors:
        error = normalize_text(error)
        if error:
            parts.append(f"{label}: {error}")
    return "; ".join(parts)


def infer_summary_confidence(row, lines, status, original_text=""):
    complete = len([line for line in lines if line]) == 3
    formal = complete and all(has_formal_korean_ending(line) for line in lines[:3])
    body_quality = row.get("body_quality", "")
    text_length = len(original_text or row.get("original_text", "") or "")

    if status == "ai":
        return "low" if body_quality in {"missing", "thin"} or not complete else "high"
    if body_quality in {"missing", "thin"} or text_length < 80 or not complete:
        return "low"
    if body_quality in {"full", "usable"} and formal:
        return "high"
    return "medium"


def build_summary_row(row, lines, status, model, confidence="", error=""):
    add_english_suffix = not (status == "fallback" and is_likely_english_article(row))
    lines = [enforce_formal_summary_style(line, add_english_suffix=add_english_suffix) for line in lines]
    lines = (lines + ["", "", ""])[:3]
    summarized_at = datetime.now(KST).strftime("%Y-%m-%d %H:%M:%S KST")
    return {
        **{key: row.get(key, "") for key in SUMMARY_COLUMNS},
        "article_id": row.get("article_id", ""),
        "target_date": row.get("target_date", ""),
        "section_id": row.get("section_id", ""),
        "section_label": row.get("section_label", ""),
        "group": row.get("group", ""),
        "category": row.get("category", ""),
        "source": row.get("source", ""),
        "published_date": row.get("published_date", ""),
        "title": row.get("title", ""),
        "url": row.get("url", ""),
        "source_domain": row.get("source_domain", ""),
        "text_char_count": row.get("text_char_count", len(row.get("original_text", ""))),
        "body_quality": row.get("body_quality", ""),
        "summary_context": row.get("summary_context", ""),
        "summary_line_1": lines[0],
        "summary_line_2": lines[1],
        "summary_line_3": lines[2],
        "summary_text": "\n".join(line for line in lines if line),
        "summary_model": model,
        "summary_status": status,
        "summary_confidence": confidence or infer_summary_confidence(row, lines, status),
        "summary_error": error,
        "summarized_at": summarized_at,
    }


def summarize_rows(rows, options):
    env = scraper.load_env()
    scraper.configure_summary_generator(env)
    model = options.model or env.get("AGENT_B_GEMINI_MODEL") or DEFAULT_PRIMARY_MODEL
    openai_model = options.openai_model or env.get("OPENAI_SUMMARY_MODEL") or DEFAULT_OPENAI_MODEL

    target_rows = rows[: options.limit] if options.limit and options.limit > 0 else rows
    openai_results = {}
    openai_errors = {}
    openai_shared_error = ""
    if options.fallback_only:
        gemini_results, gemini_errors, shared_error = {}, {}, "fallback only"
    else:
        gemini_results = {}
        gemini_errors = {}
        shared_error = ""
        ai_candidate_rows = [
            row
            for row in target_rows
            if len(clean_text_for_summary(row.get("original_text", ""))) >= 80
        ]
        remaining_rows = ai_candidate_rows
        if remaining_rows:
            print(f"[Agent B] Gemini single pass: {model} ({len(remaining_rows)} articles)")
            model_results, model_errors = summarize_with_gemini(
                remaining_rows,
                env,
                model,
                max(1, options.batch_size),
                max(1, options.retry_attempts),
                max(1.0, options.retry_base_delay),
                max(0.0, options.inter_batch_delay),
            )
            if isinstance(model_errors, str):
                shared_error = model_errors
            else:
                gemini_results.update(model_results)
                for row in remaining_rows:
                    article_id = row.get("article_id", "")
                    if article_id in model_results:
                        gemini_errors.pop(article_id, None)
                    else:
                        gemini_errors[article_id] = model_errors.get(article_id, f"missing summary from {model}")
        remaining_rows = [
            row for row in remaining_rows
            if row.get("article_id", "") not in gemini_results
        ]
        if remaining_rows and not options.disable_openai_fallback:
            print(f"[Agent B] OpenAI fallback pass: {openai_model} ({len(remaining_rows)} articles)")
            model_results, model_errors = summarize_with_openai(
                remaining_rows,
                env,
                openai_model,
                max(1, options.batch_size),
                max(1, options.retry_attempts),
                max(1.0, options.retry_base_delay),
                max(0.0, options.inter_batch_delay),
            )
            if isinstance(model_errors, str):
                openai_shared_error = model_errors
            else:
                openai_results.update(model_results)
                for row in remaining_rows:
                    article_id = row.get("article_id", "")
                    if article_id in model_results:
                        openai_errors.pop(article_id, None)
                    else:
                        openai_errors[article_id] = model_errors.get(article_id, f"missing summary from OpenAI {openai_model}")

    output_rows = []
    for row in target_rows:
        article_id = row.get("article_id", "")
        original_text = clean_text_for_summary(row.get("original_text", ""))
        if len(original_text) < 80:
            lines = fallback_summary(row)
            output_rows.append(build_summary_row(row, lines, "fallback", "extractive", "", "original text too short"))
            continue

        if article_id in gemini_results:
            result = gemini_results[article_id]
            output_rows.append(build_summary_row(row, result["lines"], "ai", result.get("model", model), result.get("confidence", "medium")))
            continue

        if article_id in openai_results:
            result = openai_results[article_id]
            output_rows.append(build_summary_row(row, result["lines"], "ai", result.get("model", f"openai:{openai_model}"), result.get("confidence", "medium")))
            continue

        error = combine_summary_errors(
            ("gemini", gemini_errors.get(article_id) or shared_error),
            ("openai", openai_errors.get(article_id) or openai_shared_error),
        )
        lines = fallback_summary(row)
        confidence = infer_summary_confidence(row, lines, "fallback", original_text)
        output_rows.append(build_summary_row(row, lines, "fallback", "extractive", confidence, error))
    return output_rows


def write_jsonl(rows, path):
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8", newline="\n") as f:
        for row in rows:
            f.write(json.dumps(row, ensure_ascii=False) + "\n")


def write_csv(rows, path):
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8-sig", newline="") as f:
        writer = csv.DictWriter(f, fieldnames=SUMMARY_COLUMNS, extrasaction="ignore")
        writer.writeheader()
        writer.writerows(rows)


def xml_escape(value):
    text = str(value if value is not None else "")
    text = re.sub(r"[\x00-\x08\x0b\x0c\x0e-\x1f]", "", text)
    if len(text) > agent_a.EXCEL_CELL_CHAR_LIMIT:
        text = text[: agent_a.EXCEL_CELL_CHAR_LIMIT - 20] + " [truncated for Excel]"
    return (
        text.replace("&", "&amp;")
        .replace("<", "&lt;")
        .replace(">", "&gt;")
        .replace('"', "&quot;")
        .replace("'", "&apos;")
    )


def write_xlsx(rows, path):
    path.parent.mkdir(parents=True, exist_ok=True)
    matrix = [SUMMARY_COLUMNS] + [[row.get(col, "") for col in SUMMARY_COLUMNS] for row in rows]
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
            cell_ref = f"{agent_a.excel_col_name(col_idx)}{row_idx}"
            style = ' s="1"' if row_idx == 1 else ' s="2"'
            if SUMMARY_COLUMNS[col_idx - 1] == "text_char_count" and row_idx > 1:
                cells.append(f'<c r="{cell_ref}"{style}><v>{int(float(value or 0))}</v></c>')
            else:
                shared_string_ref_count += 1
                sid = shared_string_id(value)
                cells.append(f'<c r="{cell_ref}" t="s"{style}><v>{sid}</v></c>')
        row_xml.append(f'<row r="{row_idx}">{"".join(cells)}</row>')

    shared_items = "".join(f'<si><t xml:space="preserve">{xml_escape(value)}</t></si>' for value in shared_strings)
    last_cell = f"{agent_a.excel_col_name(len(SUMMARY_COLUMNS))}{len(matrix)}"
    sheet_xml = f'''<?xml version="1.0" encoding="UTF-8" standalone="yes"?>
<worksheet xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main" xmlns:r="http://schemas.openxmlformats.org/officeDocument/2006/relationships">
  <dimension ref="A1:{last_cell}"/>
  <sheetViews><sheetView workbookViewId="0"><pane ySplit="1" topLeftCell="A2" activePane="bottomLeft" state="frozen"/></sheetView></sheetViews>
  <cols>
    <col min="1" max="11" width="18" customWidth="1"/>
    <col min="12" max="14" width="16" customWidth="1"/>
    <col min="15" max="18" width="48" customWidth="1"/>
    <col min="19" max="23" width="18" customWidth="1"/>
  </cols>
  <sheetData>{"".join(row_xml)}</sheetData>
  <autoFilter ref="A1:{last_cell}"/>
</worksheet>'''
    workbook_xml = '''<?xml version="1.0" encoding="UTF-8" standalone="yes"?>
<workbook xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main" xmlns:r="http://schemas.openxmlformats.org/officeDocument/2006/relationships">
  <sheets><sheet name="summaries" sheetId="1" r:id="rId1"/></sheets>
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
</Relationships>'''
    styles = '''<?xml version="1.0" encoding="UTF-8" standalone="yes"?>
<styleSheet xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main">
  <fonts count="2"><font><sz val="11"/><name val="Calibri"/></font><font><b/><sz val="11"/><color rgb="FFFFFFFF"/><name val="Calibri"/></font></fonts>
  <fills count="3"><fill><patternFill patternType="none"/></fill><fill><patternFill patternType="gray125"/></fill><fill><patternFill patternType="solid"><fgColor rgb="FF0F766E"/><bgColor indexed="64"/></patternFill></fill></fills>
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
</Types>'''
    shared_xml = f'''<?xml version="1.0" encoding="UTF-8" standalone="yes"?>
<sst xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main" count="{shared_string_ref_count}" uniqueCount="{len(shared_strings)}">{shared_items}</sst>'''

    with zipfile.ZipFile(path, "w", compression=zipfile.ZIP_DEFLATED) as xlsx:
        xlsx.writestr("[Content_Types].xml", content_types)
        xlsx.writestr("_rels/.rels", rels)
        xlsx.writestr("xl/workbook.xml", workbook_xml)
        xlsx.writestr("xl/_rels/workbook.xml.rels", workbook_rels)
        xlsx.writestr("xl/worksheets/sheet1.xml", sheet_xml)
        xlsx.writestr("xl/sharedStrings.xml", shared_xml)
        xlsx.writestr("xl/styles.xml", styles)


def write_outputs(rows, options):
    date_key = options.target_date.strftime("%Y-%m-%d")
    output_dir = options.output_dir / date_key
    output_dir.mkdir(parents=True, exist_ok=True)
    jsonl_path = output_dir / f"summaries_{date_key}.jsonl"
    csv_path = output_dir / f"summaries_{date_key}.csv"
    xlsx_path = output_dir / f"summaries_{date_key}.xlsx"

    write_jsonl(rows, jsonl_path)
    write_csv(rows, csv_path)
    if options.write_xlsx:
        write_xlsx(rows, xlsx_path)
    return {
        "jsonl": jsonl_path,
        "csv": csv_path,
        "xlsx": xlsx_path if options.write_xlsx else None,
    }


def print_summary(rows, paths, input_path):
    status_counts = {}
    for row in rows:
        status = row.get("summary_status", "")
        status_counts[status] = status_counts.get(status, 0) + 1
    print(f"\n[Agent B] input: {input_path}")
    print(f"[Agent B] summarized articles: {len(rows)}")
    print(f"[Agent B] status: {json.dumps(status_counts, ensure_ascii=False, sort_keys=True)}")
    for label, path in paths.items():
        if path:
            print(f"[Agent B] {label}: {path}")


def main():
    args = parse_args()
    target_date = target_date_from_arg(args.date)
    input_path = resolve_input_path(args, target_date)
    options = AgentBOptions(
        target_date=target_date,
        input_path=input_path,
        output_dir=Path(args.output_dir).resolve(),
        model=args.model,
        batch_size=args.batch_size,
        limit=args.limit,
        fallback_only=args.fallback_only,
        write_xlsx=not args.no_xlsx,
        retry_attempts=args.retry_attempts,
        retry_base_delay=args.retry_base_delay,
        inter_batch_delay=args.inter_batch_delay,
        fallback_models=args.fallback_models,
        openai_model=args.openai_model,
        disable_openai_fallback=args.disable_openai_fallback,
    )
    rows = load_agent_a_rows(input_path)
    summary_rows = summarize_rows(rows, options)
    paths = write_outputs(summary_rows, options)
    print_summary(summary_rows, paths, input_path)


if __name__ == "__main__":
    main()
