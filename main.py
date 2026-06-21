import argparse
import concurrent.futures
import hashlib
import html
import http.cookiejar
import imaplib
import json
import os
import re
import time
import warnings
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime, timedelta, timezone
from email import message_from_bytes
from email.header import decode_header
from email.utils import parsedate_to_datetime
from html.parser import HTMLParser
from pathlib import Path
from urllib.parse import urlparse
from xml.etree import ElementTree
from bs4 import BeautifulSoup, XMLParsedAsHTMLWarning

try:
    import requests
except ImportError:  # pragma: no cover - fallback for bundled runtime
    requests = None

try:
    from googlenewsdecoder import gnewsdecoder
except ImportError:  # pragma: no cover - optional improvement for Google News RSS links
    gnewsdecoder = None

try:
    import agent_c
except ImportError:  # pragma: no cover - Agent C is optional for legacy runs
    agent_c = None

KST = timezone(timedelta(hours=9))
BASE_DIR = Path(__file__).resolve().parent
OUTPUT_FILE = BASE_DIR / "index.html"
SHARE_OUTPUT_FILE = BASE_DIR / "share_index.html"
ARCHIVE_JS_FILE = BASE_DIR / "archive_list.js"

HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,image/webp,*/*;q=0.8",
    "Accept-Language": "ko-KR,ko;q=0.9,en-US;q=0.8,en;q=0.7",
}

def extract_charset_from_content_type(content_type):
    match = re.search(r"charset\s*=\s*['\"]?([^;,'\"\s]+)", content_type or "", flags=re.IGNORECASE)
    return match.group(1).strip() if match else None

def decode_response_body(body, charset=None):
    body = body or b""
    candidates = []
    seen = set()

    def add_candidate(value):
        value = re.sub(r"\s+", " ", str(value or "")).strip("'\" ").lower()
        if not value or value in seen:
            return
        seen.add(value)
        candidates.append(value)

    add_candidate(charset)
    head = body[:4096]
    for match in re.findall(br"charset\s*=\s*['\"]?\s*([A-Za-z0-9._-]+)", head, flags=re.IGNORECASE):
        try:
            add_candidate(match.decode("ascii", errors="ignore"))
        except Exception:
            pass
    for fallback in ("utf-8", "cp949", "euc-kr"):
        add_candidate(fallback)

    best_text = ""
    best_score = None
    for index, encoding_name in enumerate(candidates):
        try:
            text = body.decode(encoding_name, errors="replace")
        except LookupError:
            continue
        replacement_count = text.count("\ufffd")
        hangul_count = len(re.findall(r"[가-힣]", text))
        suspicious_count = sum(1 for ch in text if "\u0080" <= ch <= "\u00ff")
        score = (replacement_count * 1000 + suspicious_count - min(hangul_count, 500), index)
        if best_score is None or score < best_score:
            best_score = score
            best_text = text

    return best_text if best_text else body.decode("utf-8", errors="replace")

def http_get_text(url, timeout=10, encoding=None):
    if requests is not None:
        res = requests.get(url, headers=HEADERS, timeout=timeout)
        charset = encoding or extract_charset_from_content_type(res.headers.get("content-type", ""))
        return decode_response_body(res.content, charset)

    req = urllib.request.Request(url, headers=HEADERS)
    with urllib.request.urlopen(req, timeout=timeout) as response:
        body = response.read()
        charset = encoding or response.headers.get_content_charset()
        return decode_response_body(body, charset)

def http_get_json(url, timeout=10):
    if requests is not None:
        res = requests.get(url, headers=HEADERS, timeout=timeout)
        return res.json()
    return json.loads(http_get_text(url, timeout=timeout))

def http_get_reader_text(url, timeout=30):
    reader_headers = {"User-Agent": "Mozilla/5.0", "Accept": "text/plain"}
    if requests is not None:
        response = requests.get(url, headers=reader_headers, timeout=timeout)
        response.raise_for_status()
        return response.text
    request = urllib.request.Request(url, headers=reader_headers)
    with urllib.request.urlopen(request, timeout=timeout) as response:
        return response.read().decode("utf-8", errors="replace")

MAX_IMPACT_NEWS = 5
MAX_GLOBAL_IMPACT_NEWS_PER_SOURCE = 2
MAX_NEWS_PER_CATEGORY = 3
MAX_CANDIDATE_NEWS_PER_CATEGORY = 30
MAX_RANKED_ISSUES_PER_CATEGORY = 8
MAX_VCAC_NEWS_PER_SOURCE = 8
MAX_AI_NEWS_PER_SOURCE = 8
SUMMARY_LINE_COUNT = 3
SUMMARY_MAX_CHARS = 145
SUMMARY_INPUT_MAX_CHARS = 20000
SUMMARY_BATCH_ITEM_MAX_CHARS = 6500
SUMMARY_BATCH_SIZE = 5
GEMINI_SUMMARY_MODEL = "gemini-2.5-flash"
SUMMARY_CACHE_FILE = BASE_DIR / "summary_cache.json"
INDUSTRY_TREND_CACHE_FILE = BASE_DIR / "industry_trend_cache.json"
INDUSTRY_SOURCE_CACHE_FILE = BASE_DIR / "industry_source_cache.json"
MCKINSEY_WEEK_IN_CHARTS_URL = "https://www.mckinsey.com/featured-insights/week-in-charts"
BAIN_INSIGHTS_URL = "https://www.bain.com/insights/"
BAIN_INSIGHTS_FEED_URL = "https://www.bain.com/insights/GetFeedItems"
BCG_PUBLICATIONS_URL = "https://www.bcg.com/publications"
BCG_PUBLICATIONS_READER_URL = "https://r.jina.ai/https://www.bcg.com/publications"
BCG_SITEMAP_READER_URL = "https://r.jina.ai/https://www.bcg.com/google_sitemap-content.xml"
KPMG_INSIGHTS_URL = "https://kpmg.com/kr/ko/insights.html"
DELOITTE_INSIGHTS_URL = "https://www.deloitte.com/kr/ko/our-thinking/deloitte-insights.html"
AI_SUMMARY_PROMPT_VERSION = "editor-v1"
AI_SUMMARY_MIN_INTERVAL_SECONDS = 4.0
SINGLE_ITEM_NEWSLETTER_SOURCES = {"Bloomberg Green", "CTVC"}
NEWSLETTER_IMAP_TIMEOUT_SECONDS = 30

GLOBAL_IMPACT_FEEDS = [
    ("ImpactAlpha", "https://impactalpha.com/feed/")
]

IMPACTON_ALLOWED_SECTIONS = {"산업", "정책", "투자·평가", "투자.평가"}

VCAC_SOURCE_PRIORITY = ("유니콘팩토리", "딜사이트", "스타트업레시피", "플래텀", "벤처스퀘어")

VCAC_BRANDING = {
    "유니콘팩토리": ("unicorn", "https://menu.mt.co.kr/ucfactory/images/meta_unicornfactory.png"),
    "딜사이트": ("dealsite", "https://dealsite.co.kr/images/favicon.svg"),
    "스타트업레시피": ("recipe", "https://startuprecipe.co.kr/wp-content/uploads/2025/05/StartupRecipe_logo-removebg-preview.png"),
    "플래텀": ("platum", "https://cdn.platum.kr/wp-content/uploads/2024/11/Platum-logo.svg"),
    "벤처스퀘어": ("venturesquare", "https://www.venturesquare.net/wp-content/uploads/2026/04/cropped-vs-symbol-color-192x192.png"),
}

AI_SOURCE_PRIORITY = ("AI News", "AI TIMES", "MarketingTech", "The Batch Data Points", "The Batch Weekly Issues")

AI_BRANDING = {
    "AI News": ("ai-news", "https://www.artificialintelligence-news.com/wp-content/uploads/2024/02/AINews-logo-300x75.png"),
    "AI TIMES": ("aitimes", "https://cdn.aitimes.com/image/logo/translogo_20250624031234.png"),
    "MarketingTech": ("marketingtech", "https://www.marketingtechnews.net/wp-content/uploads/2020/09/marketing-icon.png"),
    "The Batch Data Points": ("batch", "https://www.deeplearning.ai/_next/image?url=%2F_next%2Fstatic%2Fmedia%2Fthe-batch-logo.0b7c10a2.png&w=1080&q=75"),
    "The Batch Weekly Issues": ("batch-weekly", "https://www.deeplearning.ai/_next/image?url=%2F_next%2Fstatic%2Fmedia%2Fthe-batch-logo.0b7c10a2.png&w=1080&q=75"),
}

INDUSTRY_SOURCE_PRIORITY = ("KPMG", "Deloitte")

INDUSTRY_SOURCE_BRANDING = {
    "KPMG": ("kpmg", "https://kpmg.com/content/experience-fragments/kpmgpublic/kr/ko/site/header/master/_jcr_content/root/header_v2_copy/logo.coreimg.svg/1754641605270/logo.svg"),
    "Deloitte": ("deloitte", "https://www.deloitte.com/content/dam/assets-shared/logos/png/a-d/deloitte-print.png"),
}

AI_RSS_SOURCE_CONFIGS = [
    {
        "source": "MarketingTech",
        "feeds": ["https://www.marketingtechnews.net/categories/ai-intelligent-marketing/feed/"],
        "context": "MarketingTech의 AI 및 지능형 마케팅 섹션 기사입니다.",
    },
]

VCAC_RSS_SOURCE_CONFIGS = [
    {
        "source": "플래텀",
        "feeds": ["https://platum.kr/archives/category/investment/feed"],
        "use_browser_headers": True,
        "context": "플래텀 투자 섹션의 스타트업 투자 및 회수 소식입니다.",
    },
    {
        "source": "스타트업레시피",
        "feeds": [
            "https://startuprecipe.co.kr/archives/invest-newsletter/feed",
            "https://startuprecipe.co.kr/archives/category/news/feed",
        ],
        "context": "스타트업레시피의 뉴스레터와 뉴스레시피 기반 스타트업 생태계 소식입니다.",
    },
    {
        "source": "벤처스퀘어",
        "feeds": [
            "https://www.venturesquare.net/category/guide/startups/feed/",
            "https://www.venturesquare.net/category/news-contents/news-trends/trend/feed/",
        ],
        "context": "벤처스퀘어의 스타트업 가이드와 스타트업 트렌드 기사입니다.",
    },
]

VCAC_LISTING_SOURCE_CONFIGS = [
    {
        "source": "유니콘팩토리",
        "pages": ["https://www.unicornfactory.co.kr/money/investment"],
        "link_pattern": r"/article/\d+",
        "listing_attempts": 3,
        "listing_timeout": 35,
        "fallback_google_query": (
            "site:unicornfactory.co.kr/article "
            "(투자 OR 유치 OR 펀딩 OR 인수 OR 합병 OR M&A OR IPO OR 상장 OR 엑시트)"
        ),
        "context": "유니콘팩토리 투자·회수 섹션의 스타트업 투자 및 회수 소식입니다.",
    },
]

DEALSITE_CATEGORY_CONFIGS = (
    {
        "name": "대체투자",
        "code": "075000",
        "keywords": (
            "투자유치", "후속투자", "출자사업", "펀드 결성", "블라인드펀드", "벤처캐피탈",
            "사모펀드", "PEF", "VC", "GP", "LP", "엑시트", "회수", "IPO", "출자", "투자",
        ),
    },
    {
        "name": "인수합병",
        "code": "080000",
        "keywords": (
            "인수합병", "M&A", "매각전", "경영권", "원매자", "예비입찰", "본입찰", "우선협상",
            "주식매매계약", "SPA", "공개매수", "실사", "매각", "인수", "합병", "지분",
        ),
    },
)

SUMMARY_SKIP_KEYWORDS = (
    "무단전재", "재배포", "저작권", "copyright", "구독", "광고", "로그인",
    "이미지 확대", "재판매 및 db 금지", "댓글", "기사 공유", "기사를 공유합니다",
    "음성재생", "음성으로 듣기", "이동 통신망", "글자 수", "translated by",
    "관련 키워드", "관련 기사", "ⓒ", "저작권자", "기사 제공처", "등록기자",
    "기자에게 문의", "카카오톡", "페이스북", "url공유", "이메일에 공유",
    "가장작게", "가장크게", "기사 듣기", "북마크", "추천기사", "에디터 픽",
    "ai기능", "핵심요약", "추천질문", "관련종목", "ai해설",
    "연설하고 있다", "기념촬영", "사진 제공",
)
BLOCKED_SOURCE_DOMAINS = ("blog.naver.com", "tistory.com", "youtube.com", "netballnz.co.nz")
SPAM_NEWS_KEYWORDS = (
    "카지노", "먹튀", "토토", "바카라", "슬롯", "도박", "스포츠토토", "온라인카지노",
    "casino", "gambling", "betting", "sportsbook", "blackjack", "roulette",
)
GOOGLE_NEWS_DECODE_CACHE = {}
ARTICLE_BODY_CACHE = {}
SUMMARY_ENV = {}
SUMMARY_CACHE = {}
SUMMARY_CACHE_DIRTY = False
SUMMARY_AI_DISABLED_REASON = ""
SUMMARY_LAST_CALL_TS = 0.0
STORY_TOKEN_STOPWORDS = {
    "기사", "보도", "속보", "단독", "관련", "통해", "대한", "이번", "지난", "이날", "오늘",
    "기자", "뉴스", "발표", "예상", "전망", "추진", "착수", "확인", "정리", "내용", "소식",
    "update", "updated", "report", "reports", "reported", "news", "today",
}

SEARCH_SECTIONS = [
    {
        "id": "macro", "label": "거시경제",
        "groups": [
            {
                "title": "미국",
                "categories": [
                    {"name": "경제지표", "query": "미국 (PCE OR CPI OR GDP OR 고용지표 OR 실업률 OR 물가 OR 소매판매)", "context": "미국 경기 흐름 기사입니다."},
                    {"name": "관세", "query": "미국 (관세 OR 보호무역 OR USTR OR 통상압박 OR 수입제재 OR 트럼프 관세)", "context": "미국 무역정책 기사입니다."},
                    {"name": "통화정책", "query": "미국 (연준 OR FOMC OR 기준금리 OR 파월 OR 통화정책 OR 금리 인하 OR 금리 인상)", "context": "연준 금리 경로 기사입니다."},
                    {"name": "외교", "query": "미국 (외교 OR 대외제재 OR 미중 갈등 OR 대중 제재 OR 동맹 OR 반도체 규제)", "context": "미국 대외관계 기사입니다."},
                ],
            },
            {
                "title": "한국",
                "categories": [
                    {"name": "경제지표", "query": "한국 (소비자물가 OR GDP OR 성장률 OR 고용 동향 OR 수출입 동향 OR 실업률)", "context": "한국 실물 경제 기사입니다."},
                    {"name": "통화정책", "query": "한국은행 OR 금통위 OR 기준금리 OR 한은 통화정책 OR 이창용", "context": "한국은행 기준금리 기사입니다."},
                ],
            },
            {
                "title": "유럽",
                "categories": [{"name": "통화정책", "query": "ECB OR 유럽중앙은행 OR 유로존 금리 OR 라가르드 통화정책", "context": "ECB 정책 흐름 기사입니다."}],
            },
            {
                "title": "중국",
                "categories": [{"name": "통화정책", "query": "중국 (인민은행 OR LPR OR 지급준비율 OR 지준율 인하 OR 경기 부양)", "context": "중국 인민은행 유동성 기사입니다."}],
            },
        ],
    },
]

MACRO_ALLOWED_SOURCE_MAP = {
    "yna.co.kr": "연합뉴스",
    "mk.co.kr": "매일경제",
    "hankyung.com": "한국경제",
    "chosun.com": "조선일보",
}

MACRO_ALLOWED_SOURCE_QUERY = " OR ".join(
    f"site:{domain}" for domain in MACRO_ALLOWED_SOURCE_MAP
)

NAV_SECTIONS = (
    ("indicators", "주요 지표"),
    ("impact", "임팩트"),
    ("vcac", "VC/AC/대체투자"),
    ("ai", "AI"),
    ("macro", "거시경제"),
    ("industrytrend", "산업트랜드"),
    ("industry", "MBB 인사이트"),
    ("theme", "강세 테마"),
)

# ==========================================
# 🌟 금융 지표 30일 추이 데이터 수집 (Yahoo Finance API 활용)
# ==========================================
def fetch_historical_chart_data(ticker, range_str="30d"):
    try:
        url = f"https://query1.finance.yahoo.com/v8/finance/chart/{ticker}?interval=1d&range={range_str}"
        data = http_get_json(url, timeout=10)
        result = data["chart"]["result"][0]
        timestamps = result.get("timestamp", [])
        closes = result.get("indicators", {}).get("quote", [{}])[0].get("close", [])
        
        # Convert timestamps to dates "MM/DD"
        dates = []
        for ts in timestamps:
            dt = datetime.fromtimestamp(ts, tz=timezone(timedelta(hours=9)))
            dates.append(dt.strftime("%m/%d"))
            
        # Clean None values in closes
        cleaned_closes = []
        last_valid = None
        for val in closes:
            if val is not None:
                cleaned_closes.append(round(val, 3) if ticker == "^TNX" else (round(val, 2) if val > 10 else round(val, 4)))
                last_valid = cleaned_closes[-1]
            else:
                cleaned_closes.append(last_valid if last_valid is not None else 0.0)
                
        # Return last 30 data points
        return {
            "dates": dates[-30:],
            "values": cleaned_closes[-30:]
        }
    except Exception as e:
        print(f"Error fetching historical for {ticker}: {e}")
        return {"dates": [], "values": []}

def normalize_flow_value(text):
    text = normalize_space(text).replace("억", "").replace("백만", "").replace("천주", "")
    if not text:
        return ""
    return text

def flow_text_from_row(cells):
    if len(cells) < 4:
        return ""
    return f"개인 {normalize_flow_value(cells[1])} / 외국인 {normalize_flow_value(cells[2])} / 기관 {normalize_flow_value(cells[3])}"

def parse_market_flow_from_html(html_text, market_name):
    soup = BeautifulSoup(html_text, "html.parser")
    market_patterns = {
        "KOSPI": re.compile(r"(KOSPI|코스피|종합주가지수)", re.I),
        "KOSDAQ": re.compile(r"(KOSDAQ|코스닥)", re.I),
    }
    pattern = market_patterns[market_name]

    for table in soup.find_all("table"):
        rows = table.find_all("tr")
        for row in rows:
            cells = [normalize_space(cell.get_text(" ", strip=True)) for cell in row.find_all(["th", "td"])]
            if len(cells) < 4:
                continue
            if not pattern.search(cells[0]):
                continue
            candidate = flow_text_from_row(cells)
            if candidate:
                return candidate

    flat_text = normalize_space(soup.get_text(" ", strip=True))
    flat_text = flat_text.replace("개인", " 개인 ").replace("외국인", " 외국인 ").replace("기관", " 기관 ")
    match = re.search(
        rf"{pattern.pattern}.*?개인\s*([+\-]?\d[\d,]*)\s*.*?외국인\s*([+\-]?\d[\d,]*)\s*.*?기관\s*([+\-]?\d[\d,]*)",
        flat_text,
        flags=re.I | re.S,
    )
    if match:
        p, f, i = match.groups()
        return f"개인 {p} / 외국인 {f} / 기관 {i}"
    return "개인/외국인/기관 수급을 불러오지 못했습니다."

def fetch_market_flows():
    result = {
        "KOSPI": "개인/외국인/기관 수급을 불러오지 못했습니다.",
        "KOSDAQ": "개인/외국인/기관 수급을 불러오지 못했습니다.",
    }

    try:
        html_text = http_get_text("https://finance.naver.com/sise/", timeout=10, encoding="euc-kr")
        soup = BeautifulSoup(html_text, "html.parser")
        mapping = {
            "KOSPI": "tab_sel1_deal_trend",
            "KOSDAQ": "tab_sel2_deal_trend",
        }
        for market_name, element_id in mapping.items():
            ul = soup.select_one(f"ul#{element_id}")
            if not ul:
                continue
            values = {}
            for li in ul.select("li"):
                title_node = li.select_one(".tit")
                value_node = li.select_one(".val em")
                if not title_node or not value_node:
                    continue
                title = normalize_space(title_node.get_text(" ", strip=True))
                value = normalize_space(value_node.get_text(" ", strip=True))
                if title in {"개인", "외국인", "기관"}:
                    values[title] = value
            if {"개인", "외국인", "기관"} <= set(values):
                result[market_name] = f"개인 {values['개인']} / 외국인 {values['외국인']} / 기관 {values['기관']}"
    except Exception:
        pass

    for market_name in ("KOSPI", "KOSDAQ"):
        if "불러오지 못했습니다" in result[market_name]:
            for url in (
                "https://finance.naver.com/sise/",
                "https://finance.naver.com/sise/sise_index.naver?code=KOSPI",
                "https://finance.naver.com/sise/sise_index.naver?code=KOSDAQ",
            ):
                try:
                    html_text = http_get_text(url, timeout=10, encoding="euc-kr")
                    parsed = parse_market_flow_from_html(html_text, market_name)
                    if "불러오지 못했습니다" not in parsed:
                        result[market_name] = parsed
                        break
                except Exception:
                    continue
    return result

# ==========================================
# 🌟 대시보드 크롤링 (Yahoo API로 에러 제로화!)
# ==========================================
def fetch_dashboard_data():
    dashboard = {
        "us_10y": "조회 불가",
        "fx_info": "조회 불가",
        "kospi_info": "조회 불가", "kosdaq_info": "조회 불가",
        "kospi_flow": "개인/외국인/기관 수급을 불러오지 못했습니다.",
        "kosdaq_flow": "개인/외국인/기관 수급을 불러오지 못했습니다.",
        "theme_name": "강세테마 대기중"
    }
    print("\n[Dashboard] 금융 대시보드 데이터 수집 중...")
    
    # 🌟 1. 미국 10년물 국채 금리 (Yahoo Finance API 활용)
    # 기호: ^TNX (10-Year T-Note)
    try:
        url = "https://query1.finance.yahoo.com/v8/finance/chart/^TNX?interval=1d&range=2d"
        data = http_get_json(url, timeout=10)
        yield_val = data["chart"]["result"][0]["meta"]["regularMarketPrice"]
        dashboard["us_10y"] = f"{yield_val:.3f}%"
    except Exception as e: print("US 10Y Error:", e)

    # 🌟 2. 원/달러 환율 (Yahoo Finance API 활용)
    # 기호: KRW=X
    try:
        url = "https://query1.finance.yahoo.com/v8/finance/chart/KRW=X?interval=1d&range=2d"
        data = http_get_json(url, timeout=10)
        fx_val = data["chart"]["result"][0]["meta"]["regularMarketPrice"]
        dashboard["fx_info"] = f"종가: {fx_val:,.2f}원"
    except Exception as e: print("FX Error:", e)

    # 🌟 3. 코스피 / 코스닥 (네이버 메인화면 수치 크롤링 - 가장 확실함)
    try:
        soup = BeautifulSoup(http_get_text("https://finance.naver.com/", timeout=10, encoding="euc-kr"), 'html.parser')
        
        # 코스피
        kospi_val = soup.select_one(".kospi_area .num").text.strip()
        dashboard["kospi_info"] = kospi_val
        
        # 코스닥
        kosdaq_val = soup.select_one(".kosdaq_area .num").text.strip()
        dashboard["kosdaq_info"] = kosdaq_val
    except Exception as e: print("Index Error:", e)

    # 4. 코스피 / 코스닥 수급
    try:
        market_flows = fetch_market_flows()
        dashboard["kospi_flow"] = market_flows.get("KOSPI", dashboard["kospi_flow"])
        dashboard["kosdaq_flow"] = market_flows.get("KOSDAQ", dashboard["kosdaq_flow"])
    except Exception as e: print("Flow Error:", e)

    return dashboard

# ==========================================
# 🌟 국내 강세 테마 Top 1 수집 (네이버 금융 크롤링 & 구글 뉴스 검색 연동)
# ==========================================
def fetch_strong_theme():
    theme = {
        "name": "강세테마 대기중",
        "rate": "0.00%",
        "desc": "국내 강세테마 수집 대기중입니다.",
        "stocks": [],
        "news": []
    }
    print("\n[Theme] 국내 강세 테마 수집 중...")
    try:
        soup = BeautifulSoup(http_get_text("https://finance.naver.com/sise/theme.naver", timeout=10, encoding="euc-kr"), 'html.parser')
        
        table = soup.select_one(".type_1.theme")
        if not table:
            return theme
            
        rows = table.select("tr")
        for row in rows:
            a_tag = row.select_one("a[href*='sise_group_detail.naver']")
            if a_tag:
                theme_name = a_tag.text.strip()
                theme_href = a_tag.get("href")
                tds = row.select("td")
                change_rate = tds[1].text.strip() if len(tds) > 1 else "0.00%"
                
                theme["name"] = theme_name
                theme["rate"] = change_rate
                
                # Fetch details
                detail_url = "https://finance.naver.com" + theme_href
                d_soup = BeautifulSoup(http_get_text(detail_url, timeout=10, encoding="euc-kr"), 'html.parser')
                
                # Extract theme description
                desc_td = d_soup.select_one(".type_1 td[style*='padding-left']")
                theme_desc = ""
                if desc_td:
                    info_p = desc_td.select_one(".info_txt")
                    if info_p:
                        theme_desc = info_p.text.strip()
                
                theme["desc"] = brief_company_overview(theme_desc if theme_desc else f"{theme_name} 관련 강세 테마입니다.", theme_name)
                
                # Extract related stocks
                stock_table = d_soup.select_one(".type_5")
                stocks = []
                if stock_table:
                    s_rows = stock_table.select("tr")
                    for s_row in s_rows:
                        s_a = s_row.select_one(".name a")
                        if s_a:
                            s_name = s_a.text.strip()
                            s_code = ""
                            s_href = s_a.get("href", "")
                            if "code=" in s_href:
                                s_code = s_href.split("code=")[1]
                            tds_s = s_row.select("td")
                            if len(tds_s) > 4:
                                s_reason = brief_company_overview(tds_s[1].text.strip().replace("\n", " ").replace("기업개요", "").replace("테마 관련", ""), s_name)
                                s_price = tds_s[2].text.strip()
                                s_rate = tds_s[4].text.strip()
                                stocks.append({
                                    "name": s_name,
                                    "code": s_code,
                                    "reason": s_reason,
                                    "price": s_price,
                                    "rate": s_rate
                                })
                                if len(stocks) >= 5:
                                    break
                theme["stocks"] = stocks
                
                # Fetch related news via Google News
                news_list = []
                try:
                    query = urllib.parse.quote(f"({theme_name}) -블로그 -카페 -blog -cafe when:2d")
                    rss_text = fetch_text(f"https://news.google.com/rss/search?q={query}&hl=ko&gl=KR")
                    for item in ElementTree.fromstring(rss_text).findall(".//item")[:3]:
                        title, source_name = parse_google_news_item(item)
                        source_name = normalize_source_name(source_name)
                        google_link = item.findtext("link", "")
                        article_link = resolve_google_news_url(google_link)
                        link = article_link or google_link
                        try:
                            pub_date = parsedate_to_datetime(item.findtext("pubDate", "")).astimezone(KST).strftime("%Y.%m.%d")
                        except:
                            pub_date = datetime.now(KST).strftime("%Y.%m.%d")
                        
                        desc_text = strip_tags(item.findtext("description", ""))
                        article_body = fetch_article_body_text(article_link)
                        summary_source = article_body if len(article_body) >= 180 else desc_text
                        news_list.append({
                            "title": title,
                            "link": link,
                            "source": source_name,
                            "date": pub_date,
                            "summary": make_three_line_summary(title, summary_source, source_name, f"{theme_name} 관련 강세 테마 뉴스입니다."),
                            "_summary_source": summary_source,
                            "_summary_context": f"{theme_name} 관련 강세 테마 뉴스입니다.",
                        })
                except Exception as ne:
                    print(f"Theme News Error for {theme_name}: {ne}")
                
                theme["news"] = news_list
                break  # Only Top 1 theme!
    except Exception as e:
        print("Theme Crawl Error:", e)
        
    return theme

def load_industry_trend_cache():
    if not INDUSTRY_TREND_CACHE_FILE.exists():
        return {}
    try:
        payload = json.loads(INDUSTRY_TREND_CACHE_FILE.read_text(encoding="utf-8"))
        return payload if isinstance(payload, dict) else {}
    except Exception:
        return {}

def save_industry_trend_cache(payload):
    if payload:
        INDUSTRY_TREND_CACHE_FILE.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")

def load_industry_source_cache():
    if not INDUSTRY_SOURCE_CACHE_FILE.exists():
        return {}
    try:
        payload = json.loads(INDUSTRY_SOURCE_CACHE_FILE.read_text(encoding="utf-8"))
        return payload if isinstance(payload, dict) else {}
    except Exception:
        return {}

def save_industry_source_cache(payload):
    if payload:
        INDUSTRY_SOURCE_CACHE_FILE.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")

def normalize_mckinsey_url(url):
    if not url:
        return ""
    url = urllib.parse.urljoin("https://www.mckinsey.com", url)
    parts = urllib.parse.urlsplit(url)
    return urllib.parse.urlunsplit((parts.scheme, parts.netloc, parts.path, "", ""))

def parse_mckinsey_date(text):
    text = normalize_space(text)
    for pattern in (r"([A-Z][a-z]+ \d{1,2}, \d{4})", r"(\d{4}-\d{2}-\d{2})"):
        match = re.search(pattern, text)
        if not match:
            continue
        candidate = match.group(1)
        for fmt in ("%B %d, %Y", "%Y-%m-%d"):
            try:
                return datetime.strptime(candidate, fmt).date()
            except Exception:
                pass
    return None

def format_dot_date(date_obj):
    return date_obj.strftime("%Y.%m.%d") if date_obj else ""

def parse_dot_date_value(text):
    text = normalize_space(text)
    if not text:
        return None
    for fmt in ("%Y.%m.%d", "%Y-%m-%d"):
        try:
            return datetime.strptime(text, fmt).date()
        except Exception:
            pass
    return None

def slugify_mckinsey_title(title):
    title = html.unescape(normalize_space(title))
    title = re.sub(r"\s*-\s*McKinsey.*$", "", title, flags=re.IGNORECASE)
    title = title.replace("’", "").replace("'", "")
    title = re.sub(r"[^A-Za-z0-9]+", "-", title)
    return title.strip("-").lower()

def extract_latest_mckinsey_week_url():
    query = urllib.parse.quote("site:mckinsey.com/featured-insights/week-in-charts")
    rss_url = f"https://news.google.com/rss/search?q={query}&hl=en-US&gl=US&ceid=US:en"
    rss_text = fetch_source_text(rss_url, timeout=20)
    root = ElementTree.fromstring(rss_text)
    candidates = []
    for item in root.findall(".//item"):
        title = normalize_space(item.findtext("title", ""))
        google_link = normalize_space(item.findtext("link", ""))
        if not title or " - McKinsey" not in title:
            continue
        if title.lower().startswith("the week in charts"):
            continue
        pub_dt = parse_datetime_string(item.findtext("pubDate", ""))
        if not pub_dt or not google_link:
            continue
        candidates.append({
            "title": title.rsplit(" - ", 1)[0],
            "google_link": google_link,
            "published_date": format_dot_date(pub_dt.date()) if pub_dt else "",
            "published_iso": pub_dt.date().isoformat() if pub_dt else "",
        })

    candidates.sort(key=lambda item: (item.get("published_iso", ""), item.get("title", "")), reverse=True)
    for candidate in candidates[:8]:
        resolved = normalize_mckinsey_url(resolve_google_news_url(candidate.get("google_link", "")))
        if "/featured-insights/week-in-charts/" in resolved:
            candidate["source_url"] = resolved
            return candidate

        slug = slugify_mckinsey_title(candidate.get("title", ""))
        if slug:
            candidate["source_url"] = f"https://www.mckinsey.com/featured-insights/week-in-charts/{slug}"
            return candidate
    return {}

def translate_known_mckinsey_description(title, description):
    title_key = normalize_space(title).casefold()
    if "the quantum leap for communication" in title_key:
        return (
            "퀀텀 시장은 투자자 관심, 주요 수직 산업의 성장, 기술 혁신, 상업 고객 확대 등 여러 흐름에 힘입어 빠르게 성장하고 있습니다. "
            "현재 양자 통신 시장은 정부 수요가 중심이지만, 앞으로는 통신과 금융 서비스 같은 상업 플레이어가 성장을 이끌 가능성이 큽니다. "
            "McKinsey 연구에 따르면 전체 양자 통신 시장은 2035년까지 110억~150억 달러 규모에 이를 것으로 전망됩니다."
        )
    return description

def parse_mckinsey_chart_rows(image_description):
    text = normalize_space(image_description)
    rows = []
    if "$11.0 billion" in text or "$15.0 billion" in text:
        rows.append(["전체 시장 규모", "$0.9B-$1.0B", "$1.3B-$1.6B", "$3.5B-$4.6B", "$11.0B-$15.0B"])
    if "Government customers" in text:
        rows.extend([
            ["정부·국방 고객", "약 64%", "-", "-", "27-31%"],
            ["학계", "약 28%", "-", "-", "16-20%"],
            ["통신·클라우드·사이버보안", "약 2-6%", "-", "-", "16-26%"],
            ["금융 서비스", "약 1-5%", "-", "-", "14-24%"],
            ["헬스케어", "-", "-", "-", "6-10%"],
            ["기타 산업", "-", "-", "-", "3-7%"],
        ])
    return rows

def get_mckinsey_fallback_overrides(latest=None, cache=None):
    latest = latest or {}
    cache = cache or {}
    source_url = normalize_mckinsey_url(latest.get("source_url") or cache.get("source_url", ""))
    title_key = normalize_space(latest.get("title") or cache.get("title", "")).casefold()

    if "traumas-toll-on-the-workforce" in source_url or ("trauma" in title_key and "workforce" in title_key):
        image_description = (
            "Employees who report experiencing trauma describe lower levels of work performance "
            "and satisfaction as measured in six important areas."
        )
        return {
            "description_en": (
                "Employees who report experiencing trauma show lower levels of adaptability, "
                "learning, engagement, and other work outcomes."
            ),
            "description_ko": (
                "트라우마를 경험했다고 응답한 직원들은 적응력·학습·몰입 등 주요 업무 지표와 "
                "직무 만족도가 전반적으로 더 낮게 나타났습니다."
            ),
            "chart_image_url": (
                "https://www.mckinsey.com/~/media/mckinsey/business%20functions/"
                "people%20and%20organizational%20performance/our%20insights/"
                "how%20leaders%20can%20help%20their%20organizations%20metabolize%20strain/"
                "leadersstrain-ex1.svgz?cpy=Center&cq=50"
            ),
            "chart_image_alt": image_description,
            "image_description": image_description,
            "report_title": "How leaders can help their organizations metabolize strain",
            "report_url": (
                "https://www.mckinsey.com/capabilities/people-and-organizational-performance/"
                "our-insights/how-leaders-can-help-their-organizations-metabolize-strain"
            ),
        }
    return {}

def parse_mckinsey_week_article(article_html, source_url, fallback_meta=None):
    soup = BeautifulSoup(article_html, "html.parser")
    title = extract_page_title(soup) or (fallback_meta or {}).get("title", "")
    title = re.sub(r"\s*\|\s*McKinsey.*$", "", title).strip()
    page_text = normalize_space(soup.get_text(" ", strip=True))

    meta_desc = soup.find("meta", attrs={"name": "description"})
    description = normalize_space(meta_desc.get("content", "")) if meta_desc and meta_desc.get("content") else ""
    if not description and title and title in page_text:
        after_title = page_text.split(title, 1)[-1]
        match = re.search(r"([A-Z][a-z]+ \d{1,2}, \d{4})(.*?)(Image description:|To read the article|$)", after_title)
        if match:
            description = normalize_space(match.group(2))

    item_date = ""
    meta_date = soup.find("meta", attrs={"name": "itemdate"})
    if meta_date and meta_date.get("content"):
        dt = parse_datetime_string(meta_date.get("content"))
        item_date = format_dot_date(dt.date()) if dt else ""
    if not item_date:
        item_date = format_dot_date(parse_mckinsey_date(page_text))

    image_url = ""
    image_alt = ""
    img = soup.find("img", alt=True)
    if img:
        image_alt = normalize_space(img.get("alt", ""))
        image_url = urllib.parse.urljoin("https://www.mckinsey.com", img.get("src") or img.get("data-src") or "")

    image_description = ""
    desc_match = re.search(r"Image description:\s*(.*?)\s*(?:Note:|Source:|End of image description\.)", page_text, flags=re.IGNORECASE)
    if desc_match:
        image_description = normalize_space(desc_match.group(1))

    report_link = ""
    report_title = ""
    for anchor in soup.find_all("a", href=True):
        href = normalize_mckinsey_url(anchor.get("href", ""))
        text = normalize_space(anchor.get_text(" ", strip=True))
        if "/our-insights/" in href and "/week-in-charts/" not in href:
            report_link = href
            report_title = text
            break

    return {
        "source": "McKinsey",
        "title": title,
        "date": item_date or (fallback_meta or {}).get("published_date", ""),
        "source_url": normalize_mckinsey_url(source_url),
        "description_en": description,
        "description_ko": translate_known_mckinsey_description(title, description),
        "chart_image_url": image_url,
        "chart_image_alt": image_alt,
        "image_description": image_description,
        "chart_headers": ["구분", "2023", "2025", "2030", "2035"],
        "chart_rows": parse_mckinsey_chart_rows(image_description),
        "report_title": report_title,
        "report_url": report_link,
        "updated_at": datetime.now(KST).strftime("%Y-%m-%dT%H:%M:%S%z"),
    }

def fetch_mckinsey_article_text(url):
    last_error = None
    for fetcher, timeout in ((fetch_source_text, 20), (fetch_text, 30), (http_get_text, 30)):
        try:
            text = fetcher(url, timeout=timeout)
            if text:
                return text
        except Exception as e:
            last_error = e
    if last_error:
        raise last_error
    raise RuntimeError("Failed to fetch McKinsey article")

def build_mckinsey_fallback_item(latest, cache=None):
    cache = cache or {}
    same_source = cache.get("source_url") == latest.get("source_url")
    description_ko = (
        cache.get("description_ko")
        if same_source and cache.get("description_ko")
        else "McKinsey The Week in Charts 최신 기사입니다. 원문 링크에서 전체 차트와 설명을 확인하세요."
    )
    description_en = (
        cache.get("description_en")
        if same_source and cache.get("description_en")
        else "Latest McKinsey Week in Charts article. Open the source link to view the full chart and details."
    )
    title = latest.get("title") or cache.get("title", "")
    chart_image_url = cache.get("chart_image_url", "") if same_source else ""
    if chart_image_url.startswith("data:image/svg+xml;utf8,"):
        chart_image_url = ""
    chart_image_alt = cache.get("chart_image_alt", "") if same_source else ""
    item = {
        "source": "McKinsey",
        "title": title,
        "date": latest.get("published_date") or cache.get("date", ""),
        "source_url": latest.get("source_url") or cache.get("source_url", ""),
        "description_en": description_en,
        "description_ko": description_ko,
        "chart_image_url": chart_image_url,
        "chart_image_alt": chart_image_alt,
        "image_description": cache.get("image_description", "") if same_source else "",
        "chart_headers": cache.get("chart_headers", []),
        "chart_rows": cache.get("chart_rows", []),
        "report_title": cache.get("report_title", "") if same_source else "",
        "report_url": cache.get("report_url", "") if same_source else "",
        "updated_at": datetime.now(KST).strftime("%Y-%m-%dT%H:%M:%S%z"),
        "fetch_mode": "metadata-fallback",
    }
    for key, value in get_mckinsey_fallback_overrides(latest, cache).items():
        if value:
            item[key] = value
    return item

def parse_mbb_date(text):
    text = normalize_space(text)
    for fmt in ("%B %d, %Y", "%b %d, %Y", "%Y-%m-%d", "%Y.%m.%d"):
        try:
            return datetime.strptime(text, fmt).date()
        except Exception:
            pass
    return None

def should_exclude_bcg_item(title, source_url=""):
    title = normalize_space(title)
    source_url = normalize_space(source_url)
    han_count = sum(1 for ch in title if "\u4e00" <= ch <= "\u9fff")
    has_hangul = any("\uac00" <= ch <= "\ud7a3" for ch in title)
    if "china-global-fintech-report" in source_url.lower():
        return True
    if han_count >= 4 and not has_hangul:
        return True
    return False

def build_mbb_item(source, title, source_url, published_date, description="", image_url="", raw_text=""):
    summary_source = clean_summary_source_text(
        normalize_space(raw_text) or normalize_space(description),
        source=source,
        title=title,
    )
    context = f"{source}가 발행한 경영·산업 인사이트입니다."
    return {
        "source": source,
        "title": normalize_space(title),
        "date": format_dot_date(published_date),
        "source_url": source_url,
        "description_en": normalize_space(description),
        "description_ko": "",
        "summary": make_three_line_summary(title, summary_source, source, context),
        "_summary_source": summary_source,
        "_summary_context": context,
        "chart_image_url": image_url,
        "chart_image_alt": normalize_space(title),
        "report_title": "",
        "report_url": "",
        "updated_at": datetime.now(KST).strftime("%Y-%m-%dT%H:%M:%S%z"),
    }

def build_industry_source_item(source, title, link, published_date=None, description="", image_url="", raw_text=""):
    summary_source = clean_summary_source_text(
        normalize_space(raw_text) or normalize_space(description) or normalize_space(title),
        source=source,
        title=title,
    )
    context = f"{source} Insights 최신 발행 자료입니다."
    return {
        "source": source,
        "title": normalize_space(title),
        "link": link,
        "source_url": link,
        "date": format_dot_date(published_date) if published_date else "",
        "description_en": normalize_space(description),
        "summary": make_three_line_summary(title, summary_source, source, context),
        "_summary_source": summary_source,
        "_summary_context": context,
        "_cta_label": "원문 보기",
        "image_url": image_url,
        "updated_at": datetime.now(KST).strftime("%Y-%m-%dT%H:%M:%S%z"),
    }

def clean_consulting_insight_title(title):
    title = normalize_space(title)
    title = re.sub(r"\s*[|｜]\s*(?:Deloitte(?: Korea)?|KPMG(?: International)?)\s*$", "", title, flags=re.IGNORECASE)
    return normalize_space(title)

def extract_meta_content(soup, *candidates):
    for attrs in candidates:
        tag = soup.find("meta", attrs=attrs)
        if tag and tag.get("content"):
            return normalize_space(tag.get("content"))
    return ""

def parse_article_date_from_html(html_text):
    dt = extract_html_datetime(html_text)
    if dt:
        return dt.date()
    for pattern in (
        r'"datePublished"\s*:\s*"([^"]+)"',
        r'"dateModified"\s*:\s*"([^"]+)"',
        r'Published Time:\s*([^\n<]+)',
    ):
        match = re.search(pattern, html_text, flags=re.IGNORECASE)
        if not match:
            continue
        dt = parse_datetime_string(match.group(1))
        if dt:
            return dt.date()
        parsed = parse_mbb_date(match.group(1))
        if parsed:
            return parsed
        for fmt in ("%d %b %Y", "%d %B %Y"):
            try:
                return datetime.strptime(match.group(1), fmt).date()
            except Exception:
                pass
    return None

def extract_best_selector_text(soup, selectors, min_chars=120):
    best_text = ""
    for selector in selectors:
        for node in soup.select(selector):
            candidate = clean_article_text(node.get_text(" ", strip=True))
            if len(candidate) >= min_chars and len(candidate) > len(best_text):
                best_text = candidate
    return best_text

def extract_consulting_article_body_text(soup, title, source=""):
    title = normalize_space(title)
    page_text = clean_article_text(soup.get_text(" ", strip=True))
    if not page_text:
        return ""
    source_key = compact_text(source)
    content = ""
    if source_key == "kpmg":
        content = extract_best_selector_text(
            soup,
            ("main .cmp-text", ".cmp-text", "main .text", ".text", "main"),
            min_chars=120,
        )
    elif source_key == "deloitte":
        content = extract_best_selector_text(
            soup,
            (".cmp-text", ".article-copy", ".article-content", "[itemprop='articleBody']", "main article"),
            min_chars=120,
        )
    if not content:
        content = page_text
    if title and title in content:
        content = content.rsplit(title, 1)[-1]
    for marker in (
        "Deloitte Insights 인사이트 리포트 구독 신청",
        "유용한 정보가 있으신가요?",
        "Let's connect",
        "Explore more",
        "보고서 Download",
        "Media 사보(Channel)",
        "Contacts Request for Proposal",
        "Careers Career site 바로가기",
    ):
        if marker in content:
            content = content.split(marker, 1)[0]
    return clean_summary_source_text(content, source=source, title=title)[:SUMMARY_INPUT_MAX_CHARS]

def fetch_consulting_article_metadata(url, title_fallback="", description_fallback="", image_fallback="", source=""):
    html_text = http_get_text(url, timeout=30)
    soup = BeautifulSoup(html_text, "html.parser")
    title = clean_consulting_insight_title(extract_page_title(soup) or title_fallback)
    description = (
        extract_meta_content(
            soup,
            {"name": "description"},
            {"property": "og:description"},
            {"name": "twitter:description"},
        )
        or normalize_space(description_fallback)
    )
    image_url = (
        extract_meta_content(
            soup,
            {"property": "og:image"},
            {"name": "twitter:image"},
            {"name": "thumbnail"},
        )
        or image_fallback
    )
    published_date = parse_article_date_from_html(html_text)
    body_text = extract_consulting_article_body_text(soup, title, source=source)
    return {
        "title": title,
        "description": description,
        "image_url": urllib.parse.urljoin(url, image_url),
        "published_date": published_date,
        "raw_text": normalize_space(f"{description} {body_text}") or description,
    }

def parse_kpmg_listing_items(page_html):
    soup = BeautifulSoup(page_html, "html.parser")
    candidates = []
    seen = set()
    excluded = {
        normalize_mckinsey_url("https://kpmg.com/kr/ko/insights.html"),
        normalize_mckinsey_url("https://kpmg.com/kr/ko/insights/eri.html"),
        normalize_mckinsey_url("https://kpmg.com/kr/ko/insights/aci.html"),
        normalize_mckinsey_url("https://kpmg.com/kr/ko/insights/tkc.html"),
    }
    for teaser in soup.select(".cmp-teaser"):
        title_link = teaser.select_one('.cmp-teaser__title-link[href]')
        if not title_link:
            continue
        source_url = normalize_mckinsey_url(urllib.parse.urljoin("https://kpmg.com", title_link.get("href", "")))
        title = normalize_space(title_link.get_text(" ", strip=True))
        if not title or title.lower() == "read more" or source_url in excluded or source_url in seen:
            continue
        seen.add(source_url)
        description_node = teaser.select_one(".cmp-teaser__description")
        image_node = teaser.select_one(".cmp-teaser__image img")
        image_url = urllib.parse.urljoin("https://kpmg.com", (image_node.get("src") or "") if image_node else "")
        candidates.append({
            "title": title,
            "url": source_url,
            "description": normalize_space(description_node.get_text(" ", strip=True)) if description_node else "",
            "image_url": image_url,
        })
    return candidates

def fetch_kpmg_latest_from_sitemap():
    sitemap_xml = http_get_text("https://kpmg.com/kr/ko/sitemap.xml", timeout=40)
    root = ElementTree.fromstring(sitemap_xml)
    namespace = {"sm": "http://www.sitemaps.org/schemas/sitemap/0.9"}
    candidates = []
    for node in root.findall("sm:url", namespace):
        loc = normalize_space(node.findtext("sm:loc", "", namespace))
        lastmod = normalize_space(node.findtext("sm:lastmod", "", namespace))
        if not loc.endswith(".html") or "/kr/ko/insights/eri/" not in loc:
            continue
        if any(token in loc for token in ("/past-reports", "/eri.html")):
            continue
        dt = parse_datetime_string(lastmod)
        candidates.append((dt, loc))
    candidates.sort(key=lambda entry: entry[0] or datetime.min.replace(tzinfo=KST), reverse=True)
    return candidates[0][1] if candidates else ""

def parse_deloitte_listing_items(page_html):
    soup = BeautifulSoup(page_html, "html.parser")
    candidates = []
    seen = set()
    excluded_paths = (
        "/kr/ko/our-thinking/deloitte-insights.html",
        "/kr/ko/our-thinking/deloitte-insights-publications.html",
        "/kr/ko/our-thinking/deloitte-global-economic-review.html",
        "/kr/ko/our-thinking/mobile-app-kakao.html",
        "/kr/ko/our-thinking/industry-thinking.html",
        "/kr/ko/our-thinking/insights-archive.html",
        "/kr/ko/our-thinking/deloitte-at-ces.html",
    )
    promos = soup.select(".promo.cmp-promo--featured-primary")
    if not promos:
        promos = [promo for promo in soup.select(".promo") if "nav-promo-v3" not in (promo.get("class") or [])]
    for promo in promos:
        link = promo.find("a", href=True)
        title_node = promo.select_one(".cmp-promo__content__title")
        if not link or not title_node:
            continue
        source_url = urllib.parse.urljoin("https://www.deloitte.com", link.get("href", ""))
        if any(source_url.endswith(path) or path in source_url for path in excluded_paths) or source_url in seen:
            continue
        seen.add(source_url)
        title = normalize_space(title_node.get_text(" ", strip=True))
        if not title:
            continue
        description = normalize_space(" ".join(
            node.get_text(" ", strip=True) for node in promo.select(".cmp-promo__content__desc p")
        ))
        image_node = promo.find("img")
        image_url = urllib.parse.urljoin("https://www.deloitte.com", (image_node.get("src") or "") if image_node else "")
        candidates.append({
            "title": title,
            "url": source_url,
            "description": description,
            "image_url": image_url,
        })
    return candidates

def choose_latest_consulting_item(source, candidates, max_candidates=12):
    best_item = None
    inspected = candidates[:max_candidates]
    for order, candidate in enumerate(inspected):
        metadata = fetch_consulting_article_metadata(
            candidate.get("url", ""),
            title_fallback=candidate.get("title", ""),
            description_fallback=candidate.get("description", ""),
            image_fallback=candidate.get("image_url", ""),
            source=source,
        )
        summary_source = metadata.get("raw_text") or metadata.get("description") or candidate.get("description", "")
        if source == "Deloitte" and summary_source.count("더 알아보기") >= 2:
            summary_source = metadata.get("description") or candidate.get("description", "")
        item = build_industry_source_item(
            source,
            metadata.get("title") or candidate.get("title", ""),
            candidate.get("url", ""),
            metadata.get("published_date"),
            metadata.get("description") or candidate.get("description", ""),
            metadata.get("image_url") or candidate.get("image_url", ""),
            summary_source,
        )
        item["_sort_order"] = order
        item["_published_date_obj"] = metadata.get("published_date")
        if best_item is None:
            best_item = item
            continue
        best_date = best_item.get("_published_date_obj")
        item_date = item.get("_published_date_obj")
        if item_date and (not best_date or item_date > best_date):
            best_item = item
        elif item_date == best_date and order < best_item.get("_sort_order", 9999):
            best_item = item
        elif item_date and not best_date:
            best_item = item
    if best_item:
        best_item.pop("_sort_order", None)
        best_item.pop("_published_date_obj", None)
    return best_item

def fetch_kpmg_industry_source_item():
    latest_url = fetch_kpmg_latest_from_sitemap()
    if latest_url:
        metadata = fetch_consulting_article_metadata(latest_url, source="KPMG")
        item = build_industry_source_item(
            "KPMG",
            metadata.get("title", ""),
            latest_url,
            metadata.get("published_date"),
            metadata.get("description", ""),
            metadata.get("image_url", ""),
            metadata.get("raw_text", ""),
        )
        if item.get("title"):
            return item

    page_html = http_get_text(KPMG_INSIGHTS_URL, timeout=30)
    candidates = parse_kpmg_listing_items(page_html)
    if not candidates:
        raise RuntimeError("KPMG insights listing returned no candidates")
    item = choose_latest_consulting_item("KPMG", candidates, max_candidates=8)
    if not item:
        raise RuntimeError("KPMG insights latest article could not be determined")
    return item

def fetch_deloitte_industry_source_item():
    page_html = http_get_text(DELOITTE_INSIGHTS_URL, timeout=30)
    candidates = parse_deloitte_listing_items(page_html)
    if not candidates:
        raise RuntimeError("Deloitte insights listing returned no candidates")
    item = choose_latest_consulting_item("Deloitte", candidates, max_candidates=16)
    if not item:
        raise RuntimeError("Deloitte insights latest article could not be determined")
    return item

def fetch_industry_source_trend():
    cache = load_industry_source_cache()
    cached_by_source = {item.get("source", ""): item for item in cache.get("items", []) if isinstance(item, dict)}
    items = []
    source_fetchers = (
        ("KPMG", fetch_kpmg_industry_source_item),
        ("Deloitte", fetch_deloitte_industry_source_item),
    )
    for source, fetcher in source_fetchers:
        try:
            item = fetcher()
            if item:
                items.append(item)
            print(f"  - {source} 산업트랜드: {1 if item else 0}건")
        except Exception as exc:
            fallback = cached_by_source.get(source)
            if fallback:
                items.append(fallback)
            print(f"  - {source} 산업트랜드 fetch failed, cache {1 if fallback else 0}건 사용: {exc}")
    items.sort(key=lambda item: INDUSTRY_SOURCE_PRIORITY.index(item.get("source")) if item.get("source") in INDUSTRY_SOURCE_PRIORITY else 99)
    payload = {
        "items": items,
        "updated_at": datetime.now(KST).strftime("%Y-%m-%dT%H:%M:%S%z"),
    }
    save_industry_source_cache(payload)
    return items

def fetch_mckinsey_items(target_date):
    query = urllib.parse.quote("site:mckinsey.com/featured-insights/week-in-charts")
    rss_url = f"https://news.google.com/rss/search?q={query}&hl=en-US&gl=US&ceid=US:en"
    root = ElementTree.fromstring(fetch_source_text(rss_url, timeout=20))
    candidates = []
    seen = set()
    for node in root.findall(".//item"):
        title = normalize_space(node.findtext("title", ""))
        pub_dt = parse_datetime_string(node.findtext("pubDate", ""))
        google_link = normalize_space(node.findtext("link", ""))
        if not pub_dt or pub_dt.date() != target_date or not google_link or " - McKinsey" not in title:
            continue
        if title.lower().startswith("the week in charts"):
            continue
        title = title.rsplit(" - ", 1)[0]
        source_url = normalize_mckinsey_url(resolve_google_news_url(google_link))
        if "/featured-insights/week-in-charts/" not in source_url:
            slug = slugify_mckinsey_title(title)
            source_url = f"{MCKINSEY_WEEK_IN_CHARTS_URL}/{slug}" if slug else ""
        if not source_url or source_url in seen:
            continue
        seen.add(source_url)
        candidates.append({"title": title, "source_url": source_url, "published_date": format_dot_date(target_date)})

    items = []
    for candidate in candidates:
        try:
            article_html = fetch_mckinsey_article_text(candidate["source_url"])
            item = parse_mckinsey_week_article(article_html, candidate["source_url"], candidate)
        except Exception:
            item = build_mckinsey_fallback_item(candidate)
        item_date = parse_dot_date_value(item.get("date", ""))
        if item.get("title") and item_date == target_date:
            summary_source = item.get("description_ko") or item.get("description_en") or item.get("image_description", "")
            item["summary"] = make_three_line_summary(
                item.get("title", ""), summary_source, "McKinsey", "McKinsey의 The Week in Charts 인사이트입니다."
            )
            item["_summary_source"] = summary_source
            item["_summary_context"] = "McKinsey의 The Week in Charts 인사이트입니다."
            items.append(item)
    return items

def parse_bain_feed_items(payload, target_date):
    raw_items = []
    featured = payload.get("featuredResult") if isinstance(payload, dict) else None
    if isinstance(featured, dict):
        raw_items.append(featured)
    raw_items.extend(payload.get("results", []) if isinstance(payload, dict) else [])
    items, seen = [], set()
    for entry in raw_items:
        published_date = parse_mbb_date(entry.get("date", ""))
        source_url = urllib.parse.urljoin("https://www.bain.com", entry.get("url", ""))
        if published_date != target_date or not entry.get("title") or not source_url or source_url in seen:
            continue
        seen.add(source_url)
        image_data = entry.get("imageSrc") or {}
        image_url = urllib.parse.urljoin("https://www.bain.com", image_data.get("large", ""))
        items.append(build_mbb_item(
            "Bain & Company", entry.get("title", ""), source_url, published_date,
            entry.get("description", ""), image_url,
        ))
    return items

def fetch_bain_items(target_date):
    query = urllib.parse.urlencode({
        "start": 0, "results": 40, "filters": "", "searchValue": "", "isInPreviewMode": "False"
    })
    feed_url = f"{BAIN_INSIGHTS_FEED_URL}?{query}"
    feed_headers = {**HEADERS, "Referer": BAIN_INSIGHTS_URL}
    if requests is not None:
        response = requests.get(feed_url, headers=feed_headers, timeout=30)
        response.raise_for_status()
        payload = response.json()
    else:
        request = urllib.request.Request(feed_url, headers=feed_headers)
        with urllib.request.urlopen(request, timeout=30) as response:
            payload = json.loads(response.read().decode("utf-8", errors="replace"))
    return parse_bain_feed_items(payload, target_date)

def parse_bcg_publication_items(page_html, target_date):
    soup = BeautifulSoup(page_html, "html.parser")
    items, seen = [], set()
    for promo in soup.select(".Promo"):
        date_node = promo.select_one(".Promo-date")
        title_node = promo.select_one('.Promo-title a[href*="/publications/"]')
        if not date_node or not title_node:
            continue
        published_date = parse_mbb_date(date_node.get_text(" ", strip=True))
        source_url = urllib.parse.urljoin("https://www.bcg.com", title_node.get("href", ""))
        if published_date != target_date or source_url in seen:
            continue
        title = normalize_space(title_node.get_text(" ", strip=True))
        if should_exclude_bcg_item(title, source_url):
            continue
        seen.add(source_url)
        description_node = promo.select_one(".Promo-description")
        image_node = promo.select_one("img")
        description = description_node.get_text(" ", strip=True) if description_node else ""
        image_url = urllib.parse.urljoin(
            "https://www.bcg.com", (image_node.get("src") or image_node.get("data-src") or "") if image_node else ""
        )
        items.append(build_mbb_item(
            "BCG", title, source_url, published_date, description, image_url,
        ))
    return items

def parse_bcg_publication_markdown(markdown_text, target_date):
    recent_section = markdown_text.split("## Most Recent Insights", 1)[-1]
    recent_section = recent_section.split("## Featured Campaigns", 1)[0]
    pattern = re.compile(
        r'(?:!\[Image[^\]]*\]\((?P<image>https?://[^)]+)\)\s*)?'
        r'(?:\[[^\]]+\]\(https?://[^)]+\)\s*)?'
        r'Article\s+(?P<date>[A-Z][a-z]+\s+\d{1,2},\s+\d{4})\s+'
        r'\[(?P<title>[^\]]+)\]\((?P<url>https?://www\.bcg\.com/publications/[^)]+)\)\s+'
        r'(?P<description>.*?)(?=\n\s*\[Learn More\])',
        flags=re.DOTALL,
    )
    items, seen = [], set()
    for match in pattern.finditer(recent_section):
        published_date = parse_mbb_date(match.group("date"))
        source_url = match.group("url")
        if published_date != target_date or source_url in seen:
            continue
        title = normalize_space(match.group("title"))
        if should_exclude_bcg_item(title, source_url):
            continue
        seen.add(source_url)
        items.append(build_mbb_item(
            "BCG",
            title,
            source_url,
            published_date,
            normalize_space(match.group("description")),
            match.group("image") or "",
        ))
    return items

def parse_bcg_sitemap_candidates(markdown_text, target_date, window_days=2):
    pattern = re.compile(
        rf'\[(?P<url>https://www\.bcg\.com/publications/{target_date.year}/[^\]]+)\]'
        r'\([^)]*\)\s+(?P<date>\d{4}-\d{2}-\d{2})T'
    )
    lower_bound = target_date - timedelta(days=window_days)
    upper_bound = target_date + timedelta(days=1)
    candidates = []
    seen = set()
    for match in pattern.finditer(markdown_text):
        modified_date = parse_mbb_date(match.group("date"))
        source_url = match.group("url")
        if not modified_date or not (lower_bound <= modified_date <= upper_bound) or source_url in seen:
            continue
        seen.add(source_url)
        candidates.append(source_url)
    return candidates

def clean_bcg_markdown_text(markdown_text):
    content = markdown_text.split("Markdown Content:", 1)[-1]
    if "## Key Takeaways" in content:
        content = content.split("## Key Takeaways", 1)[-1]
    elif "### Key Takeaways" in content:
        content = content.split("### Key Takeaways", 1)[-1]
    elif re.search(r"\bArticle\s+[A-Z][a-z]+\s+\d{1,2},\s+\d{4}", content):
        content = re.split(r"\bArticle\s+[A-Z][a-z]+\s+\d{1,2},\s+\d{4}.*?\n", content, maxsplit=1)[-1]
    for marker in (
        "Save It For Later",
        "Weekly Insights Subscription",
        "Subscribe Stay ahead",
        "Related Content",
        "## Authors",
        "### Authors",
        "Contact Us",
    ):
        if marker in content:
            content = content.split(marker, 1)[0]
    content = re.sub(r'!\[[^\]]*\]\([^)]+\)', ' ', content)
    content = re.sub(r'\[([^\]]+)\]\([^)]+\)', r'\1', content)
    content = re.sub(r'^#{1,6}\s+', '', content, flags=re.MULTILINE)
    content = re.sub(r'(?m)^\s*[*-]\s+', '', content)
    content = re.sub(r'[_*`]+', '', content)
    return clean_summary_source_text(normalize_space(content), source="BCG")

def parse_bcg_reader_article(markdown_text, source_url, target_date):
    published_match = re.search(r'^Published Time:\s*(\d{4}-\d{2}-\d{2})', markdown_text, flags=re.MULTILINE)
    if not published_match or parse_mbb_date(published_match.group(1)) != target_date:
        return None
    title_match = re.search(r'^Title:\s*(.+)$', markdown_text, flags=re.MULTILINE)
    title = normalize_space(title_match.group(1)) if title_match else ""
    if should_exclude_bcg_item(title, source_url):
        return None
    raw_text = clean_bcg_markdown_text(markdown_text)
    if not title or not raw_text:
        return None
    raw_text = raw_text[:SUMMARY_INPUT_MAX_CHARS]
    description = truncate_text(raw_text, 320)
    return build_mbb_item("BCG", title, source_url, target_date, description, raw_text=raw_text)

def fetch_bcg_items_from_sitemap(target_date):
    sitemap_markdown = http_get_reader_text(BCG_SITEMAP_READER_URL, timeout=90)
    candidates = parse_bcg_sitemap_candidates(sitemap_markdown, target_date)

    def fetch_candidate(source_url):
        try:
            article_markdown = http_get_reader_text(f"https://r.jina.ai/{source_url}", timeout=60)
            return parse_bcg_reader_article(article_markdown, source_url, target_date)
        except Exception:
            return None

    items = []
    with concurrent.futures.ThreadPoolExecutor(max_workers=6) as executor:
        futures = [executor.submit(fetch_candidate, url) for url in candidates]
        for future in concurrent.futures.as_completed(futures):
            item = future.result()
            if item:
                items.append(item)
    items.sort(key=lambda item: item.get("title", ""))
    return items

def fetch_bcg_items_from_google_news(target_date):
    query = urllib.parse.quote(f"site:bcg.com/publications/{target_date.year}")
    rss_url = f"https://news.google.com/rss/search?q={query}&hl=en-US&gl=US&ceid=US:en"
    root = ElementTree.fromstring(fetch_source_text(rss_url, timeout=20))
    items, seen = [], set()
    for node in root.findall(".//item"):
        pub_dt = parse_datetime_string(node.findtext("pubDate", ""))
        title = normalize_space(node.findtext("title", ""))
        if not pub_dt or pub_dt.date() != target_date or " - BCG" not in title:
            continue
        title = re.sub(r"\s+-\s+BCG.*$", "", title, flags=re.IGNORECASE)
        source_url = resolve_google_news_url(normalize_space(node.findtext("link", "")))
        if "/publications/" not in source_url or source_url in seen or should_exclude_bcg_item(title, source_url):
            continue
        seen.add(source_url)
        description = strip_tags(node.findtext("description", ""))
        items.append(build_mbb_item("BCG", title, source_url, target_date, description))
    return items

def fetch_bcg_items(target_date):
    listing_items = []
    direct_error = None
    try:
        page_html = http_get_text(BCG_PUBLICATIONS_URL, timeout=30)
        if "Access Denied" in page_html or "Reference #" in page_html:
            raise RuntimeError("BCG blocked the listing request")
        direct_items = parse_bcg_publication_items(page_html, target_date)
        if direct_items:
            listing_items = direct_items
    except Exception as exc:
        direct_error = exc
    reader_error = None
    if not listing_items:
        try:
            markdown_text = http_get_reader_text(BCG_PUBLICATIONS_READER_URL, timeout=60)
            if markdown_text:
                listing_items = parse_bcg_publication_markdown(markdown_text, target_date)
        except Exception as exc:
            reader_error = exc

    sitemap_error = None
    try:
        sitemap_items = fetch_bcg_items_from_sitemap(target_date)
    except Exception as exc:
        sitemap_items = []
        sitemap_error = exc

    merged = {}
    for item in listing_items + sitemap_items:
        if item.get("source_url"):
            merged[item["source_url"]] = item
    if merged:
        return sorted(merged.values(), key=lambda item: item.get("title", ""))

    try:
        return fetch_bcg_items_from_google_news(target_date)
    except Exception as exc:
        raise RuntimeError(
            f"BCG listing, sitemap, and RSS fetch failed: {direct_error}; {reader_error}; {sitemap_error}; {exc}"
        ) from exc

def fetch_industry_trend(target_date):
    cache = load_industry_trend_cache()
    target_dot = format_dot_date(target_date)
    cached_items = cache.get("items", []) if cache.get("date") == target_dot else []
    cached_by_source = {}
    for item in cached_items:
        cached_by_source.setdefault(item.get("source", ""), []).append(item)

    # McKinsey publishes weekly. Prefer the dedicated cross-day cache, while
    # retaining compatibility with caches created before that key existed.
    prev_mckinsey = cache.get("mckinsey_last_known") or [
        item for item in cache.get("items", []) if item.get("source") == "McKinsey"
    ]

    items = []
    source_fetchers = (
        ("McKinsey", fetch_mckinsey_items),
        ("Bain & Company", fetch_bain_items),
        ("BCG", fetch_bcg_items),
    )
    for source, fetcher in source_fetchers:
        try:
            source_items = fetcher(target_date)
            if source == "BCG":
                source_items = [
                    item for item in source_items
                    if not should_exclude_bcg_item(item.get("title", ""), item.get("source_url", ""))
                ]
            if not source_items and source == "McKinsey" and prev_mckinsey:
                items.extend(prev_mckinsey)
                print(f"  - McKinsey MBB insights: 신규 없음, 이전 게시물 {len(prev_mckinsey)}건 유지")
            else:
                items.extend(source_items)
                if source == "McKinsey" and source_items:
                    prev_mckinsey = source_items
                print(f"  - {source} MBB insights: {len(source_items)}건")
        except Exception as exc:
            fallback = cached_by_source.get(source, [])
            if source == "McKinsey" and not fallback:
                fallback = prev_mckinsey
            if source == "BCG":
                fallback = [
                    item for item in fallback
                    if not should_exclude_bcg_item(item.get("title", ""), item.get("source_url", ""))
                ]
            items.extend(fallback)
            print(f"  - {source} MBB insights fetch failed, cache {len(fallback)}건 사용: {exc}")

    items.sort(key=lambda item: (item.get("source", ""), item.get("title", "")))
    payload = {
        "date": target_dot,
        "items": items,
        "mckinsey_last_known": prev_mckinsey,
        "updated_at": datetime.now(KST).strftime("%Y-%m-%dT%H:%M:%S%z"),
    }
    save_industry_trend_cache(payload)
    return items

# ==========================================
# 기본 함수들 (필터링 및 텍스트 정리)
# ==========================================
def parse_args():
    parser = argparse.ArgumentParser()
    parser.add_argument("--date", help="뉴스 수집 및 달력 표시 날짜 (YYYY-MM-DD). 기본값은 KST 기준 어제입니다.")
    parser.add_argument("--news-date", help="--date와 동일한 별칭입니다. 둘 다 있으면 --news-date가 우선합니다.")
    return parser.parse_args()

def parse_date_arg(date_arg):
    return datetime.strptime(date_arg.strip().replace(".", "-"), "%Y-%m-%d").date()

def get_target_date(date_arg=None):
    if not date_arg: return (datetime.now(KST) - timedelta(days=1)).date()
    return parse_date_arg(date_arg)

def get_news_date(args):
    return get_target_date(args.news_date or args.date)

def load_env():
    env = {
        key: value
        for key, value in os.environ.items()
        if value is not None
    }
    env_path = BASE_DIR / ".env"
    if not env_path.exists():
        return env
    for raw_line in env_path.read_text(encoding="utf-8", errors="ignore").splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        key = key.strip()
        if key and key not in env:
            env[key] = value.strip().strip('"').strip("'")
    return env

def normalize_space(text): return re.sub(r"\s+", " ", text or "").strip()

def split_google_news_title(raw_title):
    title = normalize_space(raw_title)
    source = "Google News"
    if " - " in title:
        head, tail = title.rsplit(" - ", 1)
        if head.strip() and tail.strip():
            title = head.strip()
            source = tail.strip()
    return title, source

def parse_google_news_item(item):
    raw_title = item.findtext("title", "")
    title, fallback_source = split_google_news_title(raw_title)
    source_name = normalize_space(item.findtext("source", "")) or fallback_source
    return title, source_name

def normalize_source_name(source_name):
    source_name = normalize_space(source_name)
    source_map = {
        "v.daum.net": "파이낸셜뉴스",
    }
    return source_map.get(source_name, source_name)

def normalize_news_netloc(url):
    netloc = urllib.parse.urlparse(url or "").netloc.lower()
    if netloc.startswith("www."):
        netloc = netloc[4:]
    return netloc

def get_macro_source_domain(url):
    netloc = normalize_news_netloc(url)
    for domain in MACRO_ALLOWED_SOURCE_MAP:
        if netloc == domain or netloc.endswith("." + domain):
            return domain
    return ""

def is_allowed_macro_source(url):
    return bool(get_macro_source_domain(url))

def normalize_macro_source_name(url, fallback_source_name=""):
    domain = get_macro_source_domain(url)
    if domain:
        return MACRO_ALLOWED_SOURCE_MAP[domain]
    return normalize_macro_source_name_by_hint(fallback_source_name)

def is_blocked_domain(url):
    netloc = normalize_news_netloc(url)
    return any(netloc == domain or netloc.endswith("." + domain) for domain in BLOCKED_SOURCE_DOMAINS)

def has_spam_news_signal(*parts):
    text = normalize_space(" ".join(str(part or "") for part in parts)).lower()
    return any(keyword.lower() in text for keyword in SPAM_NEWS_KEYWORDS)

def should_skip_search_item(section_id, category_name, source_name, title="", link=""):
    normalized = normalize_source_name(source_name)
    if is_blocked_domain(link):
        return True
    if has_spam_news_signal(title, source_name, link):
        return True
    if section_id == "macro" and category_name == "외교" and normalized == "브런치":
        return True
    return False

MACRO_SOURCE_NAME_HINTS = {
    "연합뉴스": "연합뉴스",
    "매일경제": "매일경제",
    "스타투데이": "매일경제",
    "한국경제": "한국경제",
    "조선일보": "조선일보",
    "조선비즈": "조선일보",
    "chosunbiz": "조선일보",
    "the chosun daily": "조선일보",
}

MACRO_MATCH_RULES = {
    ("미국", "경제지표"): {
        "precheck_any": ("cpi", "ppi", "pce", "gdp", "고용", "실업률", "소매판매", "산업생산", "수입물가", "소비자물가", "생산자물가", "물가", "비농업", "임금"),
        "required_groups": (
            ("미국", "美", "fed", "fomc", "연준", "파월"),
            ("cpi", "ppi", "pce", "gdp", "고용", "실업률", "소매판매", "산업생산", "수입물가", "소비자물가", "생산자물가", "물가", "비농업", "임금"),
        ),
    },
    ("미국", "관세"): {
        "precheck_any": ("관세", "통상", "tariff", "ustr", "보호무역", "수입제재", "대중 제재"),
        "required_groups": (
            ("미국", "美", "트럼프", "백악관", "ustr"),
            ("관세", "통상", "tariff", "ustr", "보호무역", "수입제재", "제재"),
        ),
    },
    ("미국", "통화정책"): {
        "precheck_any": ("연준", "fomc", "fed", "파월", "기준금리", "금리 인하", "금리 인상", "미국채"),
        "required_groups": (
            ("연준", "fomc", "fed", "파월", "미국채", "미국 국채"),
            ("기준금리", "금리", "통화정책", "금리 인하", "금리 인상", "동결", "인하", "인상"),
        ),
    },
    ("미국", "외교"): {
        "precheck_any": ("외교", "제재", "미중", "동맹", "반도체", "백악관", "국무부", "협상", "g7"),
        "required_groups": (
            ("미국", "美", "트럼프", "백악관", "국무부", "워싱턴", "g7"),
            ("외교", "제재", "미중", "동맹", "반도체", "협상", "안보", "수출통제"),
        ),
        "exclude_any": ("월드컵", "심판", "축구", "야구", "농구", "재개발", "재건축"),
    },
    ("한국", "경제지표"): {
        "precheck_any": ("소비자물가", "gdp", "성장률", "고용", "실업률", "수출", "수출입", "ict", "무역수지"),
        "required_groups": (
            ("소비자물가", "gdp", "성장률", "고용", "실업률", "수출", "수출입", "ict", "무역수지"),
        ),
    },
    ("한국", "통화정책"): {
        "precheck_any": ("한국은행", "한은", "금통위", "이창용", "기준금리", "통화정책"),
        "required_groups": (
            ("한국은행", "한은", "금통위", "이창용"),
        ),
    },
    ("유럽", "통화정책"): {
        "precheck_any": ("ecb", "유럽중앙은행", "유로존", "라가르드"),
        "required_groups": (
            ("ecb", "유럽중앙은행", "유로존", "라가르드"),
        ),
    },
    ("중국", "통화정책"): {
        "precheck_any": ("중국", "中", "인민은행", "lpr", "지급준비율", "지준율", "경기 부양", "위안화"),
        "required_groups": (
            ("중국", "中", "인민은행", "lpr", "지급준비율", "지준율", "경기 부양", "위안화"),
        ),
    },
}

def contains_macro_token(text, tokens):
    return any(token.lower() in text for token in tokens)

def is_macro_news_candidate(group_title, category_name, *parts):
    rule = MACRO_MATCH_RULES.get((group_title, category_name))
    if not rule:
        return True
    haystack = normalize_space(" ".join(str(part or "") for part in parts)).lower()
    exclude_any = rule.get("exclude_any", ())
    if exclude_any and contains_macro_token(haystack, exclude_any):
        return False
    precheck_any = rule.get("precheck_any", ())
    if precheck_any and not contains_macro_token(haystack, precheck_any):
        return False
    return True

def is_macro_news_match(group_title, category_name, *parts):
    rule = MACRO_MATCH_RULES.get((group_title, category_name))
    if not rule:
        return True
    haystack = normalize_space(" ".join(str(part or "") for part in parts)).lower()
    exclude_any = rule.get("exclude_any", ())
    if exclude_any and contains_macro_token(haystack, exclude_any):
        return False
    required_groups = rule.get("required_groups", ())
    return all(contains_macro_token(haystack, tokens) for tokens in required_groups)

def normalize_macro_source_name_by_hint(source_name):
    normalized = normalize_source_name(source_name)
    lowered = normalized.lower()
    for hint, canonical in MACRO_SOURCE_NAME_HINTS.items():
        if hint.lower() in lowered:
            return canonical
    return normalized

def is_allowed_macro_source_name(source_name):
    return normalize_macro_source_name_by_hint(source_name) in set(MACRO_ALLOWED_SOURCE_MAP.values())

def build_shareable_html(html_text):
    return html_text.replace('<script src="archive_list.js"></script>', "")

def is_valid_vcac_title(title):
    t = title.replace(" ", "").lower()
    return any(k in t for k in ["투자", "유치", "펀딩", "조달", "지분", "펀드", "결성", "출자", "vc", "ac", "인수", "합병", "m&a", "ipo", "상장"])

def is_similar_title(t1, t2, threshold=0.40):
    s1, s2 = re.sub(r'\W+', '', t1).lower(), re.sub(r'\W+', '', t2).lower()
    if not s1 or not s2: return False
    bg1, bg2 = set(s1[i:i+2] for i in range(len(s1)-1)), set(s2[i:i+2] for i in range(len(s2)-1))
    if not bg1 or not bg2: return False
    return (2.0 * len(bg1.intersection(bg2))) / (len(bg1) + len(bg2)) >= threshold

def is_domestic_news(title, summary, source):
    text = (title + " " + " ".join(summary)).lower()
    domestic_keywords = [
        "국내", "서울", "코스피", "코스닥", "원전", "반도체", "전력",
        "에너지", "정책", "금융", "정부", "산업", "기업", "수출", "주가",
        "한국"
    ]
    global_keywords = [
        "global", "us ", "u.s.", "fed", "ecb", "europe", "eu",
        "china", "india", "climate", "carbon", "cop"
    ]
    dom_count = sum(1 for kw in domestic_keywords if kw in text)
    glob_count = sum(1 for kw in global_keywords if kw in text)
    if dom_count == glob_count:
        return False
    return dom_count > glob_count

def fetch_text(url, timeout=15):
    with urllib.request.urlopen(urllib.request.Request(url, headers=HEADERS), timeout=timeout) as response:
        body = response.read()
        return decode_response_body(body, response.headers.get_content_charset())

SOURCE_FETCH_HEADERS = {
    "User-Agent": "Mozilla/5.0",
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
}

def fetch_source_text(url, timeout=20):
    with urllib.request.urlopen(urllib.request.Request(url, headers=SOURCE_FETCH_HEADERS), timeout=timeout) as response:
        body = response.read()
        return decode_response_body(body, response.headers.get_content_charset())

def extract_json_payload(text):
    text = normalize_space(text)
    if text.startswith("```"):
        text = re.sub(r"^```(?:json)?", "", text, flags=re.IGNORECASE).strip()
        text = re.sub(r"```$", "", text).strip()
    try:
        return json.loads(text)
    except Exception:
        pass
    match = re.search(r"\{.*\}", text, flags=re.DOTALL)
    if not match:
        return {}
    try:
        return json.loads(match.group(0))
    except Exception:
        return {}

def env_flag(env, key, default=True):
    value = str((env or {}).get(key, "")).strip().lower()
    if not value:
        return default
    return value not in {"0", "false", "no", "off", "disable", "disabled"}

def load_summary_cache():
    if not SUMMARY_CACHE_FILE.exists():
        return {}
    try:
        payload = json.loads(SUMMARY_CACHE_FILE.read_text(encoding="utf-8"))
        return payload if isinstance(payload, dict) else {}
    except Exception:
        return {}

def save_summary_cache():
    global SUMMARY_CACHE_DIRTY
    if not SUMMARY_CACHE_DIRTY:
        return
    SUMMARY_CACHE_FILE.write_text(json.dumps(SUMMARY_CACHE, ensure_ascii=False, indent=2), encoding="utf-8")
    SUMMARY_CACHE_DIRTY = False

def configure_summary_generator(env):
    global SUMMARY_ENV, SUMMARY_CACHE, SUMMARY_CACHE_DIRTY, SUMMARY_AI_DISABLED_REASON, SUMMARY_LAST_CALL_TS
    SUMMARY_ENV = env or {}
    SUMMARY_CACHE = load_summary_cache() if env_flag(SUMMARY_ENV, "AI_SUMMARY_ENABLED", True) else {}
    SUMMARY_CACHE_DIRTY = False
    SUMMARY_AI_DISABLED_REASON = ""
    SUMMARY_LAST_CALL_TS = 0.0

def fit_summary_input(text, limit=SUMMARY_INPUT_MAX_CHARS):
    text = clean_article_text(text)
    if len(text) <= limit:
        return text
    head_len = int(limit * 0.72)
    tail_len = max(0, limit - head_len - 40)
    return normalize_space(f"{text[:head_len]} [...본문 일부 생략...] {text[-tail_len:]}")

def summary_cache_key(model, title, source, text):
    payload = json.dumps(
        {
            "version": AI_SUMMARY_PROMPT_VERSION,
            "model": model,
            "title": normalize_space(title),
            "source": normalize_space(source),
            "text": text,
        },
        ensure_ascii=False,
        sort_keys=True,
    )
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()

def normalize_summary_lines(value):
    if isinstance(value, dict):
        value = value.get("summary")
    if not isinstance(value, list):
        return []
    lines = []
    seen = set()
    for item in value:
        line = normalize_space(str(item))
        line = re.sub(r"^\s*[-*•]?\s*\d*[.)]?\s*", "", line).strip()
        if not line or len(line) < 12:
            continue
        if line.casefold() in seen:
            continue
        seen.add(line.casefold())
        lines.append(truncate_text(line, SUMMARY_MAX_CHARS))
        if len(lines) >= SUMMARY_LINE_COUNT:
            break
    return lines if len(lines) == SUMMARY_LINE_COUNT else []

def build_editor_summary_prompt(title, article_text, source="", context=""):
    return f"""
[System Prompt]
너는 지금부터 뉴스 기사의 핵심을 완벽하게 파악하는 20년차 수석 에디터야.
주어지는 기사 제목과 본문을 읽고, 다음 규칙을 엄격하게 지켜서 정확히 3줄로 요약해 줘.

[규칙]
반드시 JSON 형식으로만 출력할 것. 다른 설명, 마크다운, 코드블록은 금지.
JSON schema: {{"summary": ["1줄", "2줄", "3줄"]}}
기사에 없는 내용은 절대 유추하거나 추가하지 말 것. 객관적 사실만 반영할 것.
각 줄은 '입니다/습니다' 체로 명확하고 간결하게 끝낼 것.
문맥상 가장 중요한 결론이나 원인을 반드시 포함할 것.
기사에서 육하원칙(누가, 언제, 어디서, 무엇을, 어떻게, 왜)을 먼저 분석한 뒤 가장 중요한 핵심만 추릴 것.
세 줄은 서로 다른 정보를 담아야 하며, 제목을 그대로 반복하지 말 것.
본문이 부족하거나 일부만 제공된 경우에도 제공된 정보 안에서만 요약할 것.

[좋은 요약 예시 1]
기사 제목: 스튜어드십 코드 10년 만에 개편…기관투자자 ESG 책임 확대
{{"summary": ["도입 10년 만에 개정된 한국 스튜어드십 코드에 따라 기관투자자의 수탁자 책임 범위가 상장주식에서 채권, 부동산, 해외자산 등 전 자산군으로 확대됩니다.", "수탁자 책임 활동 시 고려해야 할 요소를 기존 지배구조(G)를 넘어 환경 및 사회(E·S) 문제까지 넓혀 ESG 책임을 강화했습니다.", "복수 기관의 공동관여 원칙과 위탁기관 관리 의무, 체계적인 이행점검 제도를 신설해 스튜어드십 코드의 실효성을 높일 예정입니다."]}}

[좋은 요약 예시 2]
기사 제목: "복잡해서 안 본다"…영국 FCA, 투자상품 기후공시 손질
{{"summary": ["영국 금융감독청(FCA)은 투자자들이 이해하기 어렵고 활용도가 낮다는 평가를 받은 TCFD 기반 상품 단위 기후공시 의무를 폐지하기로 했습니다.", "개인투자자에게는 상품 안내 자료로 기후 리스크를 쉽게 설명하고, 기관투자자에게는 주요 배출량 데이터를 요청 시 제공하는 맞춤형 체계로 전환됩니다.", "자산운용사 차원의 기업 단위 기후 리스크 공시는 유지되며, FCA는 이번 개편으로 업계 비용 부담을 줄이고 정보의 실용성을 높일 계획입니다."]}}

[기사 정보]
출처: {source or "알 수 없음"}
카테고리 맥락: {context or "뉴스 기사"}
기사 제목: {title}
기사 본문:
\"\"\"{article_text}\"\"\"
""".strip()

def throttle_summary_call():
    global SUMMARY_LAST_CALL_TS
    try:
        min_interval = float(SUMMARY_ENV.get("AI_SUMMARY_MIN_INTERVAL_SECONDS", AI_SUMMARY_MIN_INTERVAL_SECONDS))
    except Exception:
        min_interval = AI_SUMMARY_MIN_INTERVAL_SECONDS
    if min_interval <= 0:
        return
    now = time.monotonic()
    elapsed = now - SUMMARY_LAST_CALL_TS
    if SUMMARY_LAST_CALL_TS and elapsed < min_interval:
        time.sleep(min_interval - elapsed)
    SUMMARY_LAST_CALL_TS = time.monotonic()

def call_gemini_json(api_key, model, prompt, timeout=55):
    body = {
        "contents": [{"role": "user", "parts": [{"text": prompt}]}],
        "generationConfig": {
            "temperature": 0.15,
            "responseMimeType": "application/json",
        },
    }
    url = f"https://generativelanguage.googleapis.com/v1beta/models/{urllib.parse.quote(model)}:generateContent?key={urllib.parse.quote(api_key)}"
    request = urllib.request.Request(
        url,
        data=json.dumps(body, ensure_ascii=False).encode("utf-8"),
        headers={**HEADERS, "Content-Type": "application/json"},
        method="POST",
    )
    throttle_summary_call()
    with urllib.request.urlopen(request, timeout=timeout) as response:
        data = json.loads(response.read().decode("utf-8", errors="replace"))
    text = " ".join(
        part.get("text", "")
        for candidate in data.get("candidates", [])
        for part in candidate.get("content", {}).get("parts", [])
    )
    return extract_json_payload(text)

def generate_editor_summary_with_gemini(title, raw_text="", source="", context=""):
    global SUMMARY_CACHE_DIRTY, SUMMARY_AI_DISABLED_REASON
    if SUMMARY_AI_DISABLED_REASON:
        return []
    if not env_flag(SUMMARY_ENV, "AI_SUMMARY_ENABLED", True):
        return []
    api_key = SUMMARY_ENV.get("GEMINI_API_KEY", "")
    if not api_key:
        return []

    article_text = fit_summary_input(raw_text)
    if len(article_text) < 80:
        return []

    primary_model = SUMMARY_ENV.get("GEMINI_SUMMARY_MODEL") or SUMMARY_ENV.get("GEMINI_MODEL") or GEMINI_SUMMARY_MODEL
    model_candidates = list(dict.fromkeys([primary_model, "gemini-2.5-flash-lite"]))
    last_error = None

    rate_limited_models = []
    for model in model_candidates:
        cache_key = summary_cache_key(model, title, source, article_text)
        cached = normalize_summary_lines(SUMMARY_CACHE.get(cache_key))
        if cached:
            return cached

        prompt = build_editor_summary_prompt(title, article_text, source, context)
        for attempt in range(2):
            try:
                parsed = call_gemini_json(api_key, model, prompt)
                lines = normalize_summary_lines(parsed)
                if lines:
                    SUMMARY_CACHE[cache_key] = {
                        "title": normalize_space(title),
                        "source": normalize_space(source),
                        "summary": lines,
                    }
                    SUMMARY_CACHE_DIRTY = True
                    return lines
                last_error = "invalid JSON summary shape"
            except urllib.error.HTTPError as e:
                last_error = f"HTTP {e.code}"
                if e.code == 429:
                    rate_limited_models.append(model)
                    time.sleep(4.0 * (attempt + 1))
                    break
                if e.code in {400, 401, 403}:
                    SUMMARY_AI_DISABLED_REASON = last_error
                    print(f"  - AI summary disabled: {last_error}")
                    return []
            except Exception as e:
                last_error = e
            time.sleep(1.2 * (attempt + 1))

    if len(rate_limited_models) == len(model_candidates):
        SUMMARY_AI_DISABLED_REASON = "HTTP 429"
        print("  - AI summary disabled: HTTP 429")
        return []

    if env_flag(SUMMARY_ENV, "AI_SUMMARY_DEBUG", False):
        print(f"  - AI summary failed ({source}): {last_error}")
    return []

def env_int(env, key, default):
    try:
        return max(1, int(str((env or {}).get(key, default)).strip()))
    except Exception:
        return default

def build_batch_editor_summary_prompt(items):
    payload = [
        {
            "id": item["id"],
            "source": item["source"],
            "context": item["context"],
            "title": item["title"],
            "body": item["text"],
        }
        for item in items
    ]
    return f"""
[System Prompt]
너는 지금부터 뉴스 기사의 핵심을 완벽하게 파악하는 20년차 수석 에디터야.
아래 여러 개의 기사 제목과 본문을 각각 읽고, 각 기사마다 정확히 3줄로 요약해 줘.

[규칙]
반드시 JSON 형식으로만 출력할 것. 다른 설명, 마크다운, 코드블록은 금지.
JSON schema: {{"items": [{{"id": "기사 id", "summary": ["1줄", "2줄", "3줄"]}}]}}
입력으로 받은 모든 id에 대해 결과를 반환할 것.
기사에 없는 내용은 절대 유추하거나 추가하지 말 것. 객관적 사실만 반영할 것.
각 줄은 '입니다/습니다' 체로 명확하고 간결하게 끝낼 것.
문맥상 가장 중요한 결론이나 원인을 반드시 포함할 것.
기사별로 육하원칙(누가, 언제, 어디서, 무엇을, 어떻게, 왜)을 먼저 분석한 뒤 가장 중요한 핵심만 추릴 것.
세 줄은 서로 다른 정보를 담아야 하며, 제목을 그대로 반복하지 말 것.
본문이 부족하거나 일부만 제공된 경우에도 제공된 정보 안에서만 요약할 것.

[좋은 요약 예시]
{{"items": [{{"id": "sample-1", "summary": ["영국 금융감독청(FCA)은 투자자들이 이해하기 어렵고 활용도가 낮다는 평가를 받은 TCFD 기반 투자상품 단위 기후공시 의무를 폐지합니다.", "개인투자자에게는 상품 안내 자료로 기후 리스크를 설명하고, 기관투자자에게는 요청 시 주요 배출량 데이터를 제공하는 맞춤형 체계로 전환됩니다.", "자산운용사 차원의 기업 단위 기후 리스크 공시는 유지되며, FCA는 이번 개편으로 업계 비용 부담을 줄이고 정보의 실용성을 높일 계획입니다."]}}]}}

[기사 목록]
{json.dumps(payload, ensure_ascii=False)}
""".strip()

def normalize_batch_summary_payload(parsed):
    if not isinstance(parsed, dict):
        return {}
    entries = parsed.get("items") or parsed.get("summaries") or parsed.get("results") or []
    if isinstance(entries, dict):
        entries = [
            {"id": key, "summary": value}
            for key, value in entries.items()
        ]
    if not isinstance(entries, list):
        return {}
    normalized = {}
    for entry in entries:
        if not isinstance(entry, dict):
            continue
        item_id = normalize_space(str(entry.get("id", "")))
        lines = normalize_summary_lines(entry)
        if item_id and lines:
            normalized[item_id] = lines
    return normalized

def iter_news_items_for_summary(strong_theme, domestic_impact, global_impact, search_sections, industry_trend=None, industry_source_trend=None):
    seen_object_ids = set()

    def yield_once(news_item):
        if not isinstance(news_item, dict):
            return
        object_id = id(news_item)
        if object_id in seen_object_ids:
            return
        seen_object_ids.add(object_id)
        yield news_item

    for news in (strong_theme or {}).get("news", []):
        yield from yield_once(news)
    for news in domestic_impact or []:
        yield from yield_once(news)
    for news in global_impact or []:
        yield from yield_once(news)
    industry_items = industry_trend if isinstance(industry_trend, list) else (industry_trend or {}).get("items", [])
    for news in industry_items:
        yield from yield_once(news)
    source_items = industry_source_trend if isinstance(industry_source_trend, list) else (industry_source_trend or {}).get("items", [])
    for news in source_items:
        yield from yield_once(news)
    for section in search_sections or []:
        for group in section.get("groups", []):
            for category in group.get("categories", []):
                for news in category.get("news", []):
                    yield from yield_once(news)

def get_news_summary_text(news):
    return news.get("_summary_source") or " ".join(str(line) for line in news.get("summary", []))

def get_news_summary_context(news):
    return news.get("_summary_context") or f"{news.get('source', '원문')} 보도입니다."

def apply_ai_summary_batch(batch, model, api_key):
    prompt = build_batch_editor_summary_prompt(batch)
    parsed = call_gemini_json(api_key, model, prompt, timeout=90)
    return normalize_batch_summary_payload(parsed)

def apply_ai_summaries_to_news(strong_theme, domestic_impact, global_impact, search_sections, industry_trend=None, industry_source_trend=None):
    global SUMMARY_CACHE_DIRTY
    if not env_flag(SUMMARY_ENV, "AI_SUMMARY_ENABLED", True):
        return
    api_key = SUMMARY_ENV.get("GEMINI_API_KEY", "")
    if not api_key:
        return

    primary_model = SUMMARY_ENV.get("GEMINI_SUMMARY_MODEL") or SUMMARY_ENV.get("GEMINI_MODEL") or GEMINI_SUMMARY_MODEL
    model_candidates = list(dict.fromkeys([primary_model, "gemini-2.5-flash-lite"]))
    batch_size = env_int(SUMMARY_ENV, "AI_SUMMARY_BATCH_SIZE", SUMMARY_BATCH_SIZE)
    item_limit = env_int(SUMMARY_ENV, "AI_SUMMARY_BATCH_ITEM_MAX_CHARS", SUMMARY_BATCH_ITEM_MAX_CHARS)
    max_429_batches = env_int(SUMMARY_ENV, "AI_SUMMARY_MAX_429_BATCHES", 2)

    candidates = []
    cached_count = 0
    skipped_count = 0
    for index, news in enumerate(iter_news_items_for_summary(
        strong_theme, domestic_impact, global_impact, search_sections, industry_trend, industry_source_trend
    ), 1):
        raw_text = fit_summary_input(get_news_summary_text(news), item_limit)
        if len(raw_text) < 80:
            skipped_count += 1
            continue

        cached = []
        for model in model_candidates:
            cached = normalize_summary_lines(SUMMARY_CACHE.get(summary_cache_key(model, news.get("title", ""), news.get("source", ""), raw_text)))
            if cached:
                break
        if cached:
            news["summary"] = cached
            news["_summary_mode"] = "ai-cache"
            cached_count += 1
            continue

        candidates.append({
            "id": f"n{len(candidates) + 1}",
            "news": news,
            "title": normalize_space(news.get("title", "")),
            "source": normalize_space(news.get("source", "")),
            "context": normalize_space(get_news_summary_context(news)),
            "text": raw_text,
        })

    if not candidates:
        if cached_count:
            print(f"  - AI summary cache applied: {cached_count}건")
        return

    print(f"\n[Summary] AI 배치 요약 중... 대상 {len(candidates)}건, 캐시 {cached_count}건, 제외 {skipped_count}건")
    success_count = 0
    fallback_count = 0
    rate_limited_chunks = 0

    for start in range(0, len(candidates), batch_size):
        chunk = candidates[start:start + batch_size]
        chunk_done = False
        chunk_rate_limited = False
        last_error = None
        for model in model_candidates:
            try:
                result_map = apply_ai_summary_batch(chunk, model, api_key)
                if not result_map:
                    last_error = "empty or invalid JSON"
                    continue
                for item in chunk:
                    lines = result_map.get(item["id"])
                    if not lines:
                        continue
                    news = item["news"]
                    news["summary"] = lines
                    news["_summary_mode"] = f"ai-batch:{model}"
                    SUMMARY_CACHE[summary_cache_key(model, item["title"], item["source"], item["text"])] = {
                        "title": item["title"],
                        "source": item["source"],
                        "summary": lines,
                    }
                    success_count += 1
                chunk_done = True
                break
            except urllib.error.HTTPError as e:
                last_error = f"HTTP {e.code}"
                if e.code == 429:
                    chunk_rate_limited = True
                    print(f"  - AI summary batch rate-limited ({model}, {start + 1}-{start + len(chunk)}): HTTP 429")
                    time.sleep(10 + 5 * (rate_limited_chunks + 1))
                    continue
                if e.code in {400, 401, 403}:
                    print(f"  - AI summary batch stopped ({model}): HTTP {e.code}")
                    fallback_count += len(chunk)
                    chunk_done = True
                    break
            except Exception as e:
                last_error = e
                continue

        if not chunk_done:
            fallback_count += len(chunk)
            print(f"  - AI summary batch fallback ({start + 1}-{start + len(chunk)}): {last_error}")
            if chunk_rate_limited:
                rate_limited_chunks += 1
        else:
            rate_limited_chunks = 0

        if rate_limited_chunks >= max_429_batches:
            remaining = len(candidates) - (start + len(chunk))
            if remaining > 0:
                fallback_count += remaining
                print(f"  - AI summary paused after {rate_limited_chunks} rate-limited batches; remaining {remaining}건은 기존 요약 유지")
            break

    if success_count:
        SUMMARY_CACHE_DIRTY = True
    print(f"[Summary] 완료: AI {success_count}건, 캐시 {cached_count}건, fallback {fallback_count}건")

def resolve_google_news_url(url):
    if "news.google.com" not in (url or ""):
        return url
    if url in GOOGLE_NEWS_DECODE_CACHE:
        return GOOGLE_NEWS_DECODE_CACHE[url]
    decoded_url = url
    if gnewsdecoder is not None:
        try:
            decoded = gnewsdecoder(url)
            if decoded.get("status") and decoded.get("decoded_url"):
                decoded_url = decoded["decoded_url"]
        except Exception:
            decoded_url = url
    GOOGLE_NEWS_DECODE_CACHE[url] = decoded_url
    return decoded_url

def strip_tags(raw_html):
    return normalize_space(html.unescape(re.sub(r"<[^>]+>", " ", re.sub(r"<(script|style)\b[^>]*>.*?</\1>", " ", raw_html, flags=re.IGNORECASE | re.DOTALL))))

def compact_text(text):
    return "".join(ch for ch in normalize_space(text).lower() if ch.isalnum())

def normalize_story_text(text):
    normalized = strip_tags(text).lower()
    replacements = {
        "美 ": "미국 ",
        " 美": " 미국",
        "中 ": "중국 ",
        " 中": " 중국",
        "韓 ": "한국 ",
        " 韓": " 한국",
        "日 ": "일본 ",
        " 日": " 일본",
        "u.s.": "미국",
        "u.s": "미국",
        "us ": "미국 ",
        "eu ": "유럽 ",
    }
    for old, new in replacements.items():
        normalized = normalized.replace(old, new)
    return normalize_space(normalized)

def extract_story_tokens(*parts):
    text = normalize_story_text(" ".join(part for part in parts if part))
    tokens = []
    for token in re.findall(r"[a-z]{2,}|\d+(?:[.,]\d+)?|[가-힣]{2,}", text):
        if token in STORY_TOKEN_STOPWORDS:
            continue
        tokens.append(token)
    return tokens

def is_duplicate_story(title_a, text_a, title_b, text_b):
    title_similar = is_similar_title(title_a, title_b, threshold=0.28)
    tokens_a = set(extract_story_tokens(title_a, text_a))
    tokens_b = set(extract_story_tokens(title_b, text_b))
    if not tokens_a or not tokens_b:
        return title_similar
    shared = tokens_a.intersection(tokens_b)
    overlap = len(shared) / max(1, min(len(tokens_a), len(tokens_b)))
    if overlap >= 0.72:
        return True
    if title_similar and (overlap >= 0.45 or len(shared) >= 4):
        return True
    return False

def dedupe_news_items(news_items):
    deduped = []
    for item in news_items:
        item_text = " ".join(item.get("summary", []))
        if any(
            is_similar_title(item.get("title", ""), existing.get("title", ""), threshold=0.20)
            or is_duplicate_story(item.get("title", ""), item_text, existing.get("title", ""), " ".join(existing.get("summary", [])))
            for existing in deduped
        ):
            continue
        deduped.append(item)
    return deduped

ISSUE_ACTION_SIGNALS = (
    "발표", "결정", "확정", "시행", "공개", "체결", "승인", "통과", "출범", "착수",
    "인상", "인하", "동결", "제재", "관세", "투자", "인수", "합병", "상장", "파산",
    "announces", "launches", "approves", "raises", "cuts", "acquires", "merger",
)
ISSUE_OFFICIAL_SIGNALS = (
    "정부", "기획재정부", "산업부", "금융위", "한국은행", "금통위", "연준", "fomc",
    "백악관", "미 재무부", "ustr", "ecb", "유럽중앙은행", "인민은행", "국회", "법원",
)
ISSUE_LOW_VALUE_SIGNALS = (
    "전망", "가능성", "관측", "예상", "관련주", "수혜주", "급등주", "주목", "알아보니",
    "왜", "어떻게", "칼럼", "기고", "오피니언", "인터뷰", "홍보", "이벤트", "모집",
)
ISSUE_SOURCE_SCORES = {
    "연합뉴스": 20,
    "한국경제": 18,
    "매일경제": 18,
    "조선일보": 17,
    "조선비즈": 17,
    "reuters": 20,
    "bloomberg": 19,
    "associated press": 19,
}

def issue_title_tokens(title):
    return {
        token for token in extract_story_tokens(title)
        if len(token) >= 2 and token not in {"미국", "한국", "중국", "유럽", "정부", "기업"}
    }

def is_same_news_issue(title_a, title_b):
    if is_similar_title(title_a, title_b, threshold=0.30):
        return True
    tokens_a = issue_title_tokens(title_a)
    tokens_b = issue_title_tokens(title_b)
    if not tokens_a or not tokens_b:
        return False
    shared = tokens_a.intersection(tokens_b)
    overlap = len(shared) / max(1, min(len(tokens_a), len(tokens_b)))
    return (len(shared) >= 2 and overlap >= 0.60) or len(shared) >= 4

def load_recent_briefing_titles(target_date, lookback_days=3):
    titles = []
    for days_ago in range(1, lookback_days + 1):
        archive_path = BASE_DIR / f"archive_{(target_date - timedelta(days=days_ago)).strftime('%Y-%m-%d')}.html"
        if not archive_path.exists():
            continue
        try:
            soup = BeautifulSoup(archive_path.read_text(encoding="utf-8", errors="ignore"), "html.parser")
            titles.extend(
                normalize_space(anchor.get_text(" ", strip=True))
                for anchor in soup.select(".news-title a")
                if normalize_space(anchor.get_text(" ", strip=True))
            )
        except Exception:
            continue
    return titles

def source_authority_score(source_name):
    lowered = normalize_space(source_name).lower()
    for source_hint, score in ISSUE_SOURCE_SCORES.items():
        if source_hint.lower() in lowered:
            return score
    return 12

def score_issue_candidate(item, previous_titles=None):
    title = normalize_space(item.get("title", ""))
    raw_text = normalize_space(item.get("_summary_source", ""))
    text = f"{title} {raw_text[:1800]}".lower()
    score = source_authority_score(item.get("source", ""))
    score += 18  # category rules already admitted this candidate
    if any(signal in text for signal in ISSUE_ACTION_SIGNALS):
        score += 12
    if any(signal in text for signal in ISSUE_OFFICIAL_SIGNALS):
        score += 10
    if re.search(r"\d+(?:[.,]\d+)?\s*(?:%|조|억|만|bp|p|달러|원|명|건)", text, flags=re.IGNORECASE):
        score += 8
    if len(raw_text) >= 500:
        score += 5
    if any(signal in title.lower() for signal in ISSUE_LOW_VALUE_SIGNALS):
        score -= 14
    if previous_titles and any(is_same_news_issue(title, old_title) for old_title in previous_titles):
        score -= 18
        item["_repeated_issue"] = True
    return max(0, min(100, score))

def cluster_and_rank_issues(news_items, previous_titles=None):
    clusters = []
    for item in news_items:
        item["_base_importance_score"] = score_issue_candidate(item, previous_titles)
        matched_cluster = None
        for cluster in clusters:
            if any(is_same_news_issue(item.get("title", ""), member.get("title", "")) for member in cluster):
                matched_cluster = cluster
                break
        if matched_cluster is None:
            clusters.append([item])
        else:
            matched_cluster.append(item)

    ranked = []
    for cluster in clusters:
        sources = {normalize_space(item.get("source", "")).casefold() for item in cluster if item.get("source")}
        coverage_bonus = min(28, max(0, len(sources) - 1) * 12)
        representative = max(
            cluster,
            key=lambda item: (
                item.get("_base_importance_score", 0),
                source_authority_score(item.get("source", "")),
                len(item.get("_summary_source", "")),
            ),
        )
        representative["_coverage_count"] = len(sources)
        representative["_related_articles"] = [
            {"title": item.get("title", ""), "source": item.get("source", ""), "link": item.get("link", "")}
            for item in cluster if item is not representative
        ]
        representative["_importance_score"] = min(
            100,
            representative.get("_base_importance_score", 0) + coverage_bonus,
        )
        ranked.append(representative)
    return sorted(
        ranked,
        key=lambda item: (
            item.get("_importance_score", 0),
            item.get("_coverage_count", 0),
            source_authority_score(item.get("source", "")),
        ),
        reverse=True,
    )

def refine_issue_ranking_with_gemini(items, section_label, group_title, category_name):
    if not items:
        return items
    api_key = SUMMARY_ENV.get("GEMINI_API_KEY", "")
    if not api_key or str(SUMMARY_ENV.get("AI_ISSUE_RANKING_ENABLED", "1")).lower() in {"0", "false", "no", "off"}:
        return items
    candidates = items[:MAX_RANKED_ISSUES_PER_CATEGORY]
    payload = [
        {
            "id": f"i{index}",
            "title": item.get("title", ""),
            "source": item.get("source", ""),
            "coverage": item.get("_coverage_count", 1),
            "rule_score": item.get("_importance_score", 0),
            "text": normalize_space(item.get("_summary_source", ""))[:700],
        }
        for index, item in enumerate(candidates, 1)
    ]
    prompt = (
        "한국어 뉴스 브리핑의 주요 이슈 편집자 역할을 하라. 후보가 지정 카테고리에 직접 관련되는지와 "
        "실제 정책 결정·공식 발표·시장/산업 영향이 큰 주요 뉴스인지 평가하라. 단순 전망, 칼럼, 홍보, "
        "관련주 기사는 낮게 평가하라. JSON만 반환하라.\n"
        f"섹션: {section_label} / 그룹: {group_title} / 카테고리: {category_name}\n"
        "스키마: {\"items\":[{\"id\":\"i1\",\"relevance\":0,\"importance\":0,\"reason\":\"짧은 이유\"}]}\n"
        f"후보: {json.dumps(payload, ensure_ascii=False)}"
    )
    model = SUMMARY_ENV.get("GEMINI_MODEL", GEMINI_SUMMARY_MODEL)
    body = {
        "contents": [{"role": "user", "parts": [{"text": prompt}]}],
        "generationConfig": {"temperature": 0.1, "responseMimeType": "application/json"},
    }
    url = f"https://generativelanguage.googleapis.com/v1beta/models/{urllib.parse.quote(model)}:generateContent?key={urllib.parse.quote(api_key)}"
    try:
        request = urllib.request.Request(
            url,
            data=json.dumps(body, ensure_ascii=False).encode("utf-8"),
            headers={**HEADERS, "Content-Type": "application/json"},
            method="POST",
        )
        with urllib.request.urlopen(request, timeout=45) as response:
            data = json.loads(response.read().decode("utf-8", errors="replace"))
        response_text = " ".join(
            part.get("text", "")
            for candidate in data.get("candidates", [])
            for part in candidate.get("content", {}).get("parts", [])
        )
        parsed = extract_json_payload(response_text)
        assessments = {
            entry.get("id"): entry
            for entry in parsed.get("items", [])
            if isinstance(entry, dict) and entry.get("id")
        }
        if not assessments:
            return items
        for index, item in enumerate(candidates, 1):
            assessment = assessments.get(f"i{index}", {})
            if not assessment:
                continue
            relevance = max(0, min(100, int(assessment.get("relevance", 50))))
            importance = max(0, min(100, int(assessment.get("importance", 50))))
            rule_score = item.get("_importance_score", 0)
            item["_ai_relevance"] = relevance
            item["_ai_importance"] = importance
            item["_ranking_reason"] = normalize_space(assessment.get("reason", ""))[:160]
            item["_importance_score"] = round(rule_score * 0.70 + relevance * 0.10 + importance * 0.20, 1)
            if relevance < 45:
                item["_importance_score"] -= 25
        return sorted(candidates, key=lambda item: item.get("_importance_score", 0), reverse=True)
    except Exception as exc:
        print(f"  - issue ranking AI fallback ({group_title}/{category_name}): {exc}")
        return items

def rank_existing_section_categories(section, previous_titles=None, limit=MAX_NEWS_PER_CATEGORY):
    for group in section.get("groups", []):
        for category in group.get("categories", []):
            news_items = category.get("news", [])
            if not news_items:
                continue
            if category.get("preserve_selection"):
                category["news"] = news_items[:limit]
                print(
                    f"  - {section.get('label', section.get('id', ''))}/{category.get('name', '')}: "
                    f"균형 선택 {len(category['news'])}건 유지"
                )
                continue
            ranked = cluster_and_rank_issues(news_items, previous_titles)
            category["news"] = ranked[:limit]
            print(
                f"  - {section.get('label', section.get('id', ''))}/{category.get('name', '')}: "
                f"후보 {len(news_items)}건 → 주요 뉴스 {len(category['news'])}건"
            )
    return section

def rank_news_by_source(news_items, previous_titles=None, limit=MAX_NEWS_PER_CATEGORY):
    grouped = {}
    source_order = []
    for item in news_items:
        source = item.get("source", "")
        if source not in grouped:
            grouped[source] = []
            source_order.append(source)
        grouped[source].append(item)
    selected = []
    for source in source_order:
        ranked = cluster_and_rank_issues(grouped[source], previous_titles)
        selected.extend(ranked[:limit])
    return selected

def count_selected_news(strong_theme, impact_news, search_sections):
    count = len((strong_theme or {}).get("news", [])) + len(impact_news or [])
    count += sum(
        len(category.get("news", []))
        for section in search_sections or []
        for group in section.get("groups", [])
        for category in group.get("categories", [])
    )
    return count

def truncate_text(text, limit=90):
    text = normalize_space(text)
    if len(text) <= limit:
        return text
    return text[: max(0, limit - 1)].rstrip() + "…"

def summary_source_family(source):
    key = compact_text(source)
    if "kpmg" in key:
        return "kpmg"
    if "deloitte" in key:
        return "deloitte"
    if "bcg" in key:
        return "bcg"
    return key

def split_into_summary_candidates(text):
    text = normalize_space(text)
    if not text:
        return []
    text = re.sub(r"\s*[|/]\s*", ". ", text)
    text = re.sub(r"[•·▪■◆▶]", ". ", text)
    text = re.sub(r"\.\.\.|…", ". ", text)
    text = re.sub(r"\s+(?=\d+\.\s)", "|", text)
    text = re.sub(r"([.!?])\s+", r"\1|", text)
    text = re.sub(r"(습니다|입니다|됩니다|했습니다|했다|한다|됐다|이다)\s+(?=[가-힣A-Za-z0-9])", r"\1|", text)
    raw_parts = [normalize_space(part) for part in text.split("|") if normalize_space(part)]
    candidates = []
    seen = set()
    for part in raw_parts:
        key = compact_text(part)
        if key and key not in seen:
            seen.add(key)
            candidates.append(part)
    return candidates

def compress_summary_sentence(sentence, limit=SUMMARY_MAX_CHARS):
    sentence = normalize_space(sentence)
    if len(sentence) <= limit:
        return sentence.rstrip(" ,;:-")
    clauses = re.split(r"(?<=[,;:])\s+|\s+-\s+|\s+—\s+", sentence)
    built = ""
    for clause in clauses:
        clause = normalize_space(clause)
        if not clause:
            continue
        candidate = f"{built} {clause}".strip() if built else clause
        if len(candidate) > limit:
            break
        built = candidate
    result = built or sentence
    if len(result) <= limit:
        return result.rstrip(" ,;:-")
    clipped = result[: max(0, limit - 1)].rstrip()
    if " " in clipped:
        clipped = clipped.rsplit(" ", 1)[0]
    clipped = clipped.rstrip(" ,;:-")
    return (clipped or result[: max(0, limit - 1)].rstrip(" ,;:-")) + "…"

def normalize_summary_candidate(sentence, source=""):
    sentence = normalize_space(sentence)
    sentence = re.sub(r"\bv\.daum\.net\b", "파이낸셜뉴스", sentence, flags=re.IGNORECASE)
    sentence = re.sub(r"^[가-힣]{2,5}\s기자\s+", "", sentence)
    sentence = re.sub(r"^\([^)]{2,30}\)\s*", "", sentence)
    sentence = re.sub(r"^\[[^\]]{2,40}\]\s*", "", sentence)
    sentence = re.sub(r"^.{0,90}?\d{4}[-./]\d{1,2}[-./]\d{1,2}\s+[가-힣]{2,5}\s+기자\s+", "", sentence)
    sentence = re.sub(r"^[가-힣A-Za-z·.\s]{2,20}\s기자\s*=\s*", "", sentence)
    sentence = re.sub(r"^[가-힣A-Za-z·.\s]{2,40}\s제공\s+", "", sentence)
    sentence = re.sub(r"^(송고|입력|수정)\s+\d{4}[-./년\s]\d{1,2}.*", "", sentence)
    sentence = re.sub(r"\b(Read more|Learn more|Visit page|Subscribe|Manage subscriptions|Download article)\b", "", sentence, flags=re.IGNORECASE)
    sentence = re.sub(r"\s{2,}", " ", sentence)
    if source:
        sentence = normalize_space(re.sub(rf"\s*[-|]?\s*{re.escape(source)}\s*$", "", sentence, flags=re.IGNORECASE))
    return sentence.strip(" -–—•·|")

def clean_summary_source_text(text, source="", title=""):
    text = clean_article_text(text)
    if not text:
        return ""
    family = summary_source_family(source)
    title = normalize_space(title)

    common_patterns = (
        r"https?://\S+",
        r"\[/?[^\]]+\]",
        r"\(\s*javascript:[^)]+\)",
        r"\b(Skip to Main|Skip to main content|Log in|Log error|View Profile|Edit Profile|Manage Subscriptions|My Saved Content|Logout)\b",
        r"\b(Visit Page|Save It For Later|Link copied)\b",
    )
    for pattern in common_patterns:
        text = re.sub(pattern, " ", text, flags=re.IGNORECASE)

    if family == "kpmg":
        for marker in (
            "Skip to main content",
            "Services Main menu",
            "Featured Services",
            "바로 가기",
            "전체 보기",
            "삼정회계법인 공식 홈페이지입니다",
        ):
            if marker in text and len(text.split(marker, 1)[0]) >= 100:
                text = text.split(marker, 1)[0]
                break
        text = re.sub(r"\bimport_contacts\b", " ", text, flags=re.IGNORECASE)

    elif family == "deloitte":
        text = re.sub(r"^바로가기:\s*.*?(?=(안정적이던|딜로이트는|제품 복잡성과|본 보고서가))", "", text)
        for marker in (
            "리포트 전문 다운로드",
            "PDF 카드뉴스 다운로드",
            "문의하기",
            "관련 인사이트",
            "추천 콘텐츠",
            "Contact us",
            "Let’s connect",
            "Let's connect",
        ):
            if marker in text and len(text.split(marker, 1)[0]) >= 100:
                text = text.split(marker, 1)[0]
                break

    elif family == "bcg":
        for marker in (
            "Featured Insights",
            "Weekly Insights Subscription",
            "Subscribe Stay ahead",
            "Manage Subscriptions",
            "Contact Us",
            "Privacy Policy",
        ):
            if marker in text and len(text.split(marker, 1)[0]) >= 100:
                text = text.split(marker, 1)[0]
                break
        text = re.sub(r"\b(BCG Skip to Main|Our Services|Industries|Capabilities|Featured Insights)\b", " ", text, flags=re.IGNORECASE)

    if title:
        text = re.sub(rf"^\s*{re.escape(title)}\s*[|｜-]?\s*(?:BCG|Deloitte Korea|KPMG(?: International)?)?\s*", "", text, flags=re.IGNORECASE)
    text = normalize_space(text)
    return " ".join(split_into_summary_candidates(text))[:SUMMARY_INPUT_MAX_CHARS]

def brief_company_overview(raw_text, stock_name="", limit=None):
    text = normalize_space(raw_text)
    if not text:
        return f"{stock_name} 관련 기업개요를 확인해 주세요." if stock_name else "기업개요를 확인해 주세요."

    text = re.sub(r"(기업개요|테마 관련|테마관련|관련주|수혜주|관련 원인|기업 해설)", " ", text)
    text = re.sub(r"\s+", " ", text).strip(" ,·/-")
    if stock_name and stock_name not in text:
        text = f"{stock_name} {text}"
    return truncate_text(text, limit) if limit else text

def parse_datetime_string(text):
    if not text:
        return None
    text = normalize_space(text)
    for candidate in (text, text.replace("Z", "+00:00")):
        try:
            dt = parsedate_to_datetime(candidate)
            if dt is not None:
                return dt.astimezone(KST)
        except Exception:
            pass
        try:
            dt = datetime.fromisoformat(candidate)
            if dt.tzinfo is None:
                dt = dt.replace(tzinfo=KST)
            return dt.astimezone(KST)
        except Exception:
            pass
    return None

def parse_display_date(text):
    text = normalize_space(text)
    if not text:
        return None
    for pattern in [
        r"\b(?:Jan|Feb|Mar|Apr|May|Jun|Jul|Aug|Sep|Sept|Oct|Nov|Dec)[a-z]*\.?\s+\d{1,2},\s+\d{4}\b",
        r"\b\d{4}[./-]\d{1,2}[./-]\d{1,2}\b",
    ]:
        match = re.search(pattern, text, flags=re.IGNORECASE)
        if not match:
            continue
        candidate = match.group(0).replace("Sept", "Sep")
        parsed = parse_datetime_string(candidate)
        if parsed:
            return parsed
        for fmt in ("%b %d, %Y", "%B %d, %Y", "%Y.%m.%d", "%Y-%m-%d", "%Y/%m/%d"):
            try:
                return datetime.strptime(candidate, fmt).replace(tzinfo=KST)
            except Exception:
                pass
    return None

def parse_aitimes_listing_date(text, reference_date):
    text = normalize_space(text)
    match = re.search(r"\b(\d{2})-(\d{2})\s+(\d{2}):(\d{2})\b", text)
    if not match:
        return None
    month, day, hour, minute = (int(match.group(i)) for i in range(1, 5))
    try:
        return datetime(reference_date.year, month, day, hour, minute, tzinfo=KST)
    except ValueError:
        return None

def extract_feed_item_date(item):
    for tag_name in ("pubdate", "published", "updated", "date", "dc:date"):
        tag = item.find(tag_name)
        if not tag or not tag.text:
            continue
        dt = parse_datetime_string(tag.text)
        if dt:
            return dt
    return None

def extract_feed_item_link(item):
    link_tag = item.find("link")
    if not link_tag:
        return ""
    href = link_tag.get("href", "").strip()
    if href:
        return href
    return normalize_space(link_tag.text)

def extract_feed_item_title(item):
    title_tag = item.find("title")
    if not title_tag:
        return ""
    return normalize_space(title_tag.text)

def extract_html_datetime(text):
    if not text:
        return None
    for pattern in [
        r'property=["\']article:published_time["\'][^>]*content=["\']([^"\']+)["\']',
        r'property=["\']article:modified_time["\'][^>]*content=["\']([^"\']+)["\']',
        r'name=["\']pubdate["\'][^>]*content=["\']([^"\']+)["\']',
        r'name=["\']date["\'][^>]*content=["\']([^"\']+)["\']',
        r'"datePublished"\s*:\s*"([^"]+)"',
        r'"dateModified"\s*:\s*"([^"]+)"',
        r'<time[^>]*datetime=["\']([^"\']+)["\']',
    ]:
        for candidate in re.findall(pattern, text, flags=re.IGNORECASE):
            dt = parse_datetime_string(candidate)
            if dt:
                return dt
    return None

def score_summary_candidate(sentence, index, role="generic"):
    sentence_lower = sentence.lower()
    score = 0
    length = len(sentence)
    if 35 <= length <= 120:
        score += 18
    elif 20 <= length <= 145:
        score += 10
    if index == 0:
        score += 14
    elif index < 3:
        score += 8
    if re.search(r"\d", sentence):
        score += 10
    if re.search(r"\b(?:ai|kpi|oem|cbam|scenario|scenarios|value|productivity|yield|market|growth|margin)\b", sentence_lower):
        score += 10
    for keyword in (
        "전환", "재편", "구조", "시나리오", "영향", "분석", "전략", "대응", "고려", "핵심",
        "도입", "규제", "위기", "기회", "시장", "성과", "생산성", "가치", "성장", "리스크",
    ):
        if keyword in sentence:
            score += 5
    if role == "lead":
        if any(token in sentence for token in ("핵심", "전환", "재편", "위기", "시장", "산업", "Players are", "Almost all", "딜로이트는", "본 보고서")):
            score += 10
    elif role == "evidence":
        if re.search(r"\d|%|시나리오|분석|영향|도출|increase|productivity|yield|value potential", sentence_lower):
            score += 14
    elif role == "implication":
        if any(token in sentence for token in ("전략", "대응", "고려", "시사", "출발점", "권고", "필요", "도움", "제시", "recommendations")):
            score += 14
    return score

def pick_summary_candidate(candidates, used_keys, role="generic"):
    best_sentence, best_score = "", None
    for index, sentence in enumerate(candidates):
        key = sentence.casefold()
        if key in used_keys:
            continue
        score = score_summary_candidate(sentence, index, role=role)
        if best_score is None or score > best_score:
            best_sentence, best_score = sentence, score
    return best_sentence

def combine_summary_sentences(first, second):
    first = normalize_space(first)
    second = normalize_space(second)
    if not first or not second:
        return first or second
    if compact_text(first) == compact_text(second):
        return first
    tails = []
    for chunk in re.split(r"(?<=[,;:])\s+", second):
        chunk = normalize_space(chunk)
        if 18 <= len(chunk) <= 60:
            tails.append(chunk)
            break
    tails.extend((
        re.split(r"(?<=[,;:])\s+", second, maxsplit=1)[0],
        compress_summary_sentence(second, limit=max(36, min(72, SUMMARY_MAX_CHARS - len(first) - 1))),
    ))
    for tail in tails:
        tail = normalize_space(tail)
        candidate = normalize_space(f"{first} {tail}")
        if (
            tail
            and len(tail) <= 72
            and len(candidate) <= min(115, SUMMARY_MAX_CHARS)
            and compact_text(first) not in compact_text(tail)
        ):
            return candidate
    return compress_summary_sentence(first)

def make_extractive_three_line_summary(title, raw_text="", source="", context=""):
    title = normalize_space(title)
    source = normalize_space(source)
    context = normalize_space(context)
    text = clean_summary_source_text(raw_text, source=source, title=title)
    title_key = compact_text(title)
    source_key = compact_text(source)
    candidates = []
    for sentence in split_into_summary_candidates(text):
        sentence = normalize_summary_candidate(sentence, source=source)
        sentence_key = compact_text(sentence)
        sentence_lower = sentence.lower()
        if len(sentence) < 15 or any(k.lower() in sentence_lower for k in SUMMARY_SKIP_KEYWORDS):
            continue
        if (
            sentence_key in {title_key, source_key}
            or sentence in title
            or (title_key and title_key in sentence_key)
            or (title_key and sentence_key and sentence_key in title_key and len(sentence_key) >= 8)
        ):
            continue
        candidates.append(sentence)

    lines, seen = [], set()
    lead = pick_summary_candidate(candidates, seen, role="lead")
    if lead:
        lead_key = lead.casefold()
        supporting = pick_summary_candidate(candidates[: min(4, len(candidates))], {lead_key}, role="generic")
        if (
            supporting
            and len(lead) < 80
            and summary_source_family(source) != "bcg"
            and not re.match(r"^[a-z]", supporting)
        ):
            lead = combine_summary_sentences(lead, supporting)
        lead = compress_summary_sentence(lead)
        lines.append(lead)
        seen.add(lead.casefold())
        seen.add(lead_key)
        if supporting:
            seen.add(supporting.casefold())

    for role in ("evidence", "implication", "generic"):
        if len(lines) >= SUMMARY_LINE_COUNT:
            break
        sentence = pick_summary_candidate(candidates, seen, role=role)
        if not sentence:
            continue
        sentence = compress_summary_sentence(sentence)
        key = sentence.casefold()
        if key not in seen:
            lines.append(sentence)
            seen.add(key)

    recomposed_fallbacks = []
    if source:
        recomposed_fallbacks.append(f"{source}가 짚은 핵심 쟁점과 영향 포인트를 함께 확인할 수 있습니다.")
    if title:
        recomposed_fallbacks.append(f"{truncate_text(title, 68)} 관련 핵심 내용을 정리한 자료입니다.")
    if context:
        recomposed_fallbacks.append(context)

    for fallback in recomposed_fallbacks:
        if len(lines) >= SUMMARY_LINE_COUNT:
            break
        fallback = compress_summary_sentence(fallback)
        if fallback and fallback.casefold() not in seen:
            lines.append(fallback)
            seen.add(fallback.casefold())

    return lines[:SUMMARY_LINE_COUNT]

def make_three_line_summary(title, raw_text="", source="", context=""):
    return make_extractive_three_line_summary(title, raw_text, source, context)

class ArticleLinkParser(HTMLParser):
    def __init__(self):
        super().__init__()
        self.links, self._href, self._text_parts, self._capture_depth = [], "", [], 0
    def handle_starttag(self, tag, attrs):
        attrs_dict = dict(attrs)
        if tag == "a" and "articleView.html" in attrs_dict.get("href", ""):
            self._href, self._text_parts, self._capture_depth = attrs_dict["href"], [], 1
        elif self._capture_depth: self._capture_depth += 1
    def handle_data(self, data):
        if self._capture_depth: self._text_parts.append(data)
    def handle_endtag(self, tag):
        if not self._capture_depth: return
        self._capture_depth -= 1
        if self._capture_depth == 0 and self._href:
            self.links.append((normalize_space(html.unescape(" ".join(self._text_parts))), self._href))

def extract_impact_date(article_html):
    dt = extract_html_datetime(article_html)
    return dt.strftime("%Y.%m.%d") if dt else None

def extract_impacton_section(soup):
    meta = soup.find("meta", attrs={"property": "article:section"})
    if meta and meta.get("content"):
        return normalize_space(meta.get("content"))
    header = soup.select_one(".article-view-header")
    if not header:
        return ""
    for link in header.find_all("a"):
        text = normalize_space(link.get_text(" ", strip=True))
        if text and text != "홈":
            return text
    return ""

def is_allowed_impacton_section(soup):
    section = extract_impacton_section(soup)
    return section in IMPACTON_ALLOWED_SECTIONS

def decode_mime_header(value):
    if not value:
        return ""
    parts = []
    for chunk, encoding in decode_header(value):
        if isinstance(chunk, bytes):
            parts.append(chunk.decode(encoding or "utf-8", errors="replace"))
        else:
            parts.append(chunk)
    return normalize_space("".join(parts))

def extract_newsletter_items_from_html(html_text, source_name, target_dot):
    soup = BeautifulSoup(html_text or "", "html.parser")
    items = []
    seen_links = set()
    blocked = (
        "unsubscribe",
        "preferences",
        "account",
        "login",
        "signup",
        "instagram.com",
        "linkedin.com",
        "facebook.com",
        "x.com",
        "twitter.com",
        "youtube.com",
        "mailto:",
    )
    blocked_titles = {
        "view in browser",
        "ctvc by sightline climate",
        "sightline climate",
    }
    for anchor in soup.find_all("a", href=True):
        link = anchor.get("href", "").strip()
        if not link.startswith("http"):
            continue
        low = link.lower()
        if any(token in low for token in blocked):
            continue
        title = normalize_space(anchor.get_text(" ", strip=True))
        if title.casefold() in blocked_titles:
            continue
        if len(title) < 12:
            continue
        if link in seen_links:
            continue
        seen_links.add(link)
        parent_text = normalize_space(anchor.parent.get_text(" ", strip=True))
        summary = make_three_line_summary(title, parent_text, source_name, f"{source_name} newsletter article.")
        items.append({
            "title": title,
            "link": link,
            "date": target_dot,
            "source": source_name,
            "summary": summary,
            "_summary_source": parent_text,
            "_summary_context": f"{source_name} newsletter article.",
        })
        if len(items) >= 6:
            break
    return items

def extract_newsletter_primary_link(html_text):
    soup = BeautifulSoup(html_text or "", "html.parser")
    blocked = (
        "unsubscribe",
        "preferences",
        "account",
        "login",
        "signup",
        "instagram.com",
        "linkedin.com",
        "facebook.com",
        "x.com",
        "twitter.com",
        "youtube.com",
        "mailto:",
    )
    for anchor in soup.find_all("a", href=True):
        link = anchor.get("href", "").strip()
        if not link.startswith("http"):
            continue
        if any(token in link.lower() for token in blocked):
            continue
        return link
    return ""

def fetch_newsletter_emails(gmail_user, gmail_password, target_date, seen_links, seen_titles):
    target_dot = target_date.strftime("%Y.%m.%d")
    source_rules = [
        ("CTVC", lambda subject, sender: "ctvc" in sender or "ctvc" in subject or "climate tech vc" in subject),
        ("Bloomberg Green", lambda subject, sender: "bloomberg green" in sender or "bloomberg green" in subject),
    ]
    collected = []
    mail = None
    try:
        mail = imaplib.IMAP4_SSL("imap.gmail.com", timeout=NEWSLETTER_IMAP_TIMEOUT_SECONDS)
        mail.login(gmail_user, gmail_password)
        mail.select("INBOX")
        since = (target_date - timedelta(days=1)).strftime("%d-%b-%Y")
        status, data = mail.search(None, f'(SINCE "{since}")')
        if status != "OK":
            return collected
        for num in reversed(data[0].split()):
            status, msg_data = mail.fetch(num, "(RFC822)")
            if status != "OK" or not msg_data or not msg_data[0]:
                continue
            msg = message_from_bytes(msg_data[0][1])
            subject = decode_mime_header(msg.get("Subject", "")).lower()
            sender = decode_mime_header(msg.get("From", "")).lower()
            msg_date = parse_datetime_string(msg.get("Date", ""))
            if msg_date and msg_date.strftime("%Y.%m.%d") != target_dot:
                continue

            source_name = None
            for candidate, matcher in source_rules:
                if matcher(subject, sender):
                    source_name = candidate
                    break
            if not source_name:
                continue

            html_body = ""
            text_body = ""
            if msg.is_multipart():
                for part in msg.walk():
                    content_type = part.get_content_type()
                    disposition = str(part.get("Content-Disposition", ""))
                    if "attachment" in disposition.lower():
                        continue
                    payload = part.get_payload(decode=True) or b""
                    charset = part.get_content_charset() or "utf-8"
                    decoded = payload.decode(charset, errors="replace")
                    if content_type == "text/html" and not html_body:
                        html_body = decoded
                    elif content_type == "text/plain" and not text_body:
                        text_body = decoded
            else:
                payload = msg.get_payload(decode=True) or b""
                charset = msg.get_content_charset() or "utf-8"
                decoded = payload.decode(charset, errors="replace")
                if msg.get_content_type() == "text/html":
                    html_body = decoded
                else:
                    text_body = decoded

            items = extract_newsletter_items_from_html(html_body or text_body, source_name, target_dot)
            primary_link = extract_newsletter_primary_link(html_body or text_body)
            body_text = strip_tags(html_body) if html_body else text_body
            subject_title = normalize_space(decode_mime_header(msg.get("Subject", "")))
            if primary_link and len(subject_title) >= 12:
                subject_item = {
                    "title": subject_title,
                    "link": primary_link,
                    "date": target_dot,
                    "source": source_name,
                    "summary": make_three_line_summary(subject_title, body_text, source_name, f"{source_name} newsletter lead story."),
                    "_summary_source": body_text,
                    "_summary_context": f"{source_name} newsletter lead story.",
                }
                if source_name in SINGLE_ITEM_NEWSLETTER_SOURCES:
                    items = [subject_item]
                else:
                    items = [subject_item] + [item for item in items if item["title"] != subject_title]
            for item in items:
                if item["link"] in seen_links or any(is_similar_title(item["title"], title) for title in seen_titles):
                    continue
                seen_links.add(item["link"])
                seen_titles.append(item["title"])
                collected.append(item)
    except Exception as e:
        print(f"  - Newsletter fetch failed: {e}")
    finally:
        if mail is not None:
            try:
                mail.logout()
            except Exception:
                pass
    return collected

# --- News Fetching Logic ---
def fetch_global_impact(target_date, seen_links, seen_titles):
    target_dot = target_date.strftime("%Y.%m.%d")
    global_news = []
    for source_name, feed_url in GLOBAL_IMPACT_FEEDS:
        try:
            feed_text = fetch_text(feed_url)
            count = 0
            for item in parse_rss_feed_items(feed_text):
                if count >= MAX_GLOBAL_IMPACT_NEWS_PER_SOURCE: break
                title = item.get("title", "")
                link = item.get("link", "")
                date_tag = item.get("date")
                
                if not title or not link: continue
                if date_tag and date_tag.strftime("%Y.%m.%d") != target_dot: continue
                if not date_tag:
                    article_dt = None
                    try:
                        article_dt = extract_html_datetime(fetch_text(link))
                    except Exception:
                        pass
                    if article_dt and article_dt.strftime("%Y.%m.%d") != target_dot:
                        continue
                    
                if any(is_similar_title(title, st) for st in seen_titles) or link in seen_links: continue
                
                desc_text = strip_tags(item.get("description", ""))
                article_body = fetch_article_body_text(link)
                summary_source = article_body if len(article_body) >= 180 else desc_text
                summary = make_three_line_summary(title, summary_source, source_name, "글로벌 기후/임팩트 최신 동향입니다.")
                
                seen_links.add(link); seen_titles.append(title)
                global_news.append({
                    "title": title,
                    "link": link,
                    "date": target_dot,
                    "source": source_name,
                    "summary": summary,
                    "_summary_source": summary_source,
                    "_summary_context": "글로벌 기후/임팩트 최신 동향입니다.",
                })
                count += 1
        except Exception as e: print(f"  - {source_name} 수집 실패: {e}")
    return global_news

def parse_sitemap_entries(xml_text):
    entries = []
    root = ElementTree.fromstring(xml_text)
    for url_node in root.findall(".//{*}url"):
        loc_node = url_node.find("{*}loc")
        if loc_node is None or not loc_node.text:
            continue
        lastmod_node = url_node.find("{*}lastmod")
        news_node = url_node.find("{*}news")
        pub_node = news_node.find("{*}publication_date") if news_node is not None else None
        title_node = news_node.find("{*}title") if news_node is not None else None
        entries.append({
            "loc": loc_node.text.strip(),
            "lastmod": lastmod_node.text.strip() if lastmod_node is not None and lastmod_node.text else "",
            "publication_date": pub_node.text.strip() if pub_node is not None and pub_node.text else "",
            "title": normalize_space(title_node.text) if title_node is not None and title_node.text else "",
        })
    return entries

def should_skip_news_url(url):
    parsed = urlparse(url)
    path = parsed.path.lower()
    query = parsed.query.lower()
    if is_blocked_domain(url):
        return True
    if has_spam_news_signal(url):
        return True
    if "/admin/" in path:
        return True
    if "/search/" in path:
        return True
    if "/wp-json/" in path:
        return True
    if "/page/" in path and "s=" in query:
        return True
    if "s=" in query or "rest_route=" in query:
        return True
    return False

def clean_article_text(text):
    text = normalize_space(html.unescape(strip_tags(text or "")))
    replacements = [
        (r"\b사진\s*확대\b", ""),
        (r"\bAI\s*기사요약\b", ""),
        (r"기사 제공처\s*:\s*[^./|]{0,80}", ""),
        (r"등록기자\s*:\s*[^./|]{0,80}", ""),
        (r"\[\s*기자에게 문의하기\s*\]", ""),
        (r"\[?\s*이\s*기사에\s*나온\s*스타트업에\s*대한\s*보다\s*다양한\s*기업정보는.*?데이터랩.*?볼\s*수\s*있습니다\.?\s*\]?", ""),
        (r"이메일로\s*만나보는\s*스타트업을\s*위한\s*레시피", ""),
        (r"기자\s+이름을\s+클릭하면\s+더\s+자세한\s+정보를\s+확인할\s+수\s+있어요!?", ""),
        (r"카카오톡\s+페이스북\s+엑스\s+URL공유", ""),
        (r"가장작게\s+작게\s+기본\s+크게\s+가장크게", ""),
    ]
    for pattern, repl in replacements:
        text = re.sub(pattern, repl, text, flags=re.IGNORECASE)
    return normalize_space(text)

def iter_json_objects(data):
    if isinstance(data, dict):
        yield data
        for value in data.values():
            yield from iter_json_objects(value)
    elif isinstance(data, list):
        for item in data:
            yield from iter_json_objects(item)

def parse_json_script(script):
    text = script.string or script.get_text()
    if not text:
        return None
    try:
        return json.loads(text)
    except Exception:
        return None

def normalize_schema_type(value):
    if isinstance(value, list):
        return " ".join(str(item).lower() for item in value)
    return str(value or "").lower()

def extract_structured_article_text(soup):
    candidates = []
    for script in soup.find_all("script"):
        script_type = (script.get("type") or "").lower()
        script_id = (script.get("id") or "").lower()
        if "json" not in script_type and script_id != "__next_data__":
            continue
        data = parse_json_script(script)
        if data is None:
            continue
        for obj in iter_json_objects(data):
            content_arrange = obj.get("contentArrange")
            if isinstance(content_arrange, list):
                parts = [
                    entry.get("content", "")
                    for entry in content_arrange
                    if isinstance(entry, dict) and entry.get("type") == "text" and entry.get("content")
                ]
                if parts:
                    candidates.append(" ".join(parts))

            schema_type = normalize_schema_type(obj.get("@type"))
            is_article = "article" in schema_type
            for key in ("articleBody", "bodyText", "contentText", "text"):
                value = obj.get(key)
                if isinstance(value, str) and len(value) >= 160:
                    candidates.append(value)
            description = obj.get("description")
            if is_article and isinstance(description, str) and len(description) >= 160:
                candidates.append(description)

    cleaned = [clean_article_text(candidate) for candidate in candidates]
    cleaned = [candidate for candidate in cleaned if len(candidate) >= 160]
    return max(cleaned, key=len) if cleaned else ""

def find_amp_url(soup, base_url):
    amp_link = soup.find("link", rel=lambda value: value and "amphtml" in value)
    if amp_link and amp_link.get("href"):
        return urllib.parse.urljoin(base_url, amp_link.get("href"))
    return ""

def extract_best_article_text(soup):
    structured_text = extract_structured_article_text(soup)

    priority_selectors = [
        "#article",
        "#article-view-content-div",
        "#news_body",
        "[itemprop='articleBody']",
        ".entry-content",
        ".elementor-widget-theme-post-content",
        ".article_view",
        ".news_view",
        ".news_detail_wrap",
        ".news_cnt_detail_wrap",
        ".article_view_content",
        "#articleWrap",
        ".story-news",
        "#dic_area",
        "#articeBody",
        "#articleBody",
        ".article_body",
        ".article-content",
    ]
    generic_selectors = [
        "article",
        "main",
        "[class*='prose']",
        "[class*='content']",
        "[class*='article']",
    ]

    def node_text(node):
        for tag in node.find_all(["script", "style", "noscript", "svg", "iframe", "button"]):
            tag.decompose()
        for tag in node.select(".news_detail_wrap > span:first-child"):
            tag.decompose()
        for tag in node.select(".mid_title, .thumb_area, figure, figcaption, .caption, .related, .relation, .recommend"):
            tag.decompose()
        return clean_article_text(node.get_text(" ", strip=True))

    for selector in priority_selectors:
        candidates = soup.select(selector)
        if not candidates:
            continue
        best = max(candidates, key=lambda node: len(normalize_space(node.get_text(" ", strip=True))))
        text = node_text(best)
        if len(text) >= 180:
            if len(structured_text) >= len(text):
                return structured_text
            return text

    if len(structured_text) >= 180:
        return structured_text

    candidates = []
    for selector in generic_selectors:
        candidates.extend(soup.select(selector))
    if not candidates:
        candidates = [soup.body or soup]
    best = max(candidates, key=lambda node: len(normalize_space(node.get_text(" ", strip=True))))
    return node_text(best) or clean_article_text(soup.get_text(" ", strip=True))

def fetch_article_body_text(url):
    if not url or "news.google.com" in url:
        return ""
    if url in ARTICLE_BODY_CACHE:
        return ARTICLE_BODY_CACHE[url]
    try:
        article_html = fetch_text(url, timeout=12)
        soup = BeautifulSoup(article_html, "html.parser")
        body = extract_best_article_text(soup)
        amp_url = find_amp_url(soup, url)
        if amp_url and amp_url != url and len(body) < 450:
            try:
                amp_html = fetch_text(amp_url, timeout=12)
                amp_body = extract_best_article_text(BeautifulSoup(amp_html, "html.parser"))
                if len(amp_body) > len(body):
                    body = amp_body
            except Exception:
                pass
        ARTICLE_BODY_CACHE[url] = body
        return body
    except Exception:
        ARTICLE_BODY_CACHE[url] = ""
        return ""

def extract_page_title(soup):
    meta = soup.find("meta", attrs={"property": "og:title"})
    if meta and meta.get("content"):
        return normalize_space(meta.get("content"))
    if soup.title and soup.title.string:
        return normalize_space(soup.title.string)
    h1 = soup.find("h1")
    if h1:
        return normalize_space(h1.get_text(" ", strip=True))
    return ""

def extract_page_author(soup):
    meta = soup.find("meta", attrs={"name": "author"})
    if meta and meta.get("content"):
        return normalize_space(meta.get("content"))
    meta = soup.find("meta", attrs={"property": "article:author"})
    if meta and meta.get("content"):
        return normalize_space(meta.get("content"))
    return ""

def fetch_sitemap_news_source(source_name, sitemap_url, target_date, seen_links, seen_titles, context, limit=None, delay_seconds=2.0):
    news_items = []
    try:
        sitemap_text = fetch_text(sitemap_url, timeout=20)
        entries = parse_sitemap_entries(sitemap_text)
        for entry in entries:
            if limit is not None and len(news_items) >= limit:
                break
            link = normalize_space(entry.get("loc", ""))
            if not link or should_skip_news_url(link) or link in seen_links:
                continue
            source_dt = entry.get("publication_date") or entry.get("lastmod")
            dt = parse_datetime_string(source_dt)
            if not dt or dt.strftime("%Y.%m.%d") != target_date.strftime("%Y.%m.%d"):
                continue
            try:
                article_html = fetch_text(link, timeout=20)
                soup = BeautifulSoup(article_html, "html.parser")
                title = extract_page_title(soup) or entry.get("title") or link
                body = extract_best_article_text(soup)
                summary = make_three_line_summary(title, body, source_name, context)
                news_items.append({
                    "title": title,
                    "link": link,
                    "date": target_date.strftime("%Y.%m.%d"),
                    "source": source_name,
                    "summary": summary,
                    "_summary_source": body,
                    "_summary_context": context,
                })
                seen_links.add(link)
                seen_titles.append(title)
            except Exception as e:
                print(f"  - {source_name} article failed: {e}")
            time.sleep(delay_seconds)
    except Exception as e:
        print(f"  - {source_name} sitemap failed: {e}")
    return news_items

def clean_tracking_url(url):
    if not url:
        return ""
    try:
        parts = urllib.parse.urlsplit(url)
        query_pairs = urllib.parse.parse_qsl(parts.query, keep_blank_values=True)
        cleaned_pairs = [
            (key, value)
            for key, value in query_pairs
            if not key.lower().startswith("utm_") and key.lower() not in {"fbclid", "gclid", "yclid"}
        ]
        return urllib.parse.urlunsplit((
            parts.scheme,
            parts.netloc,
            parts.path,
            urllib.parse.urlencode(cleaned_pairs, doseq=True),
            "",
        ))
    except Exception:
        return url

def clean_source_article_title(title, source_name):
    title = normalize_space(title)
    suffixes = [
        f" - {source_name}",
        " - 유니콘팩토리",
        " - 플래텀",
        " - AI타임스",
        " < 기사본문 - AI타임스",
        " - 뉴스레터로 만나는 스타트업 투자 리포트 ‘스타트업레시피’",
        " - 뉴스레터로 만나는 스타트업 투자 리포트 '스타트업레시피'",
    ]
    for suffix in suffixes:
        if title.endswith(suffix):
            title = title[: -len(suffix)]
    return normalize_space(title)

def build_source_news_item(
    source_name,
    title,
    link,
    target_date,
    seen_links,
    seen_titles,
    context,
    desc_text="",
    story_cache=None,
    date_tag=None,
    require_article_date=True,
    cta_label="",
    strict_story_dedupe=True,
    seen_title_threshold=0.40,
    cache_title_threshold=0.20,
):
    target_dot = target_date.strftime("%Y.%m.%d")
    link = clean_tracking_url(link)
    title = clean_source_article_title(title, source_name)
    if not title or not link or link in seen_links:
        return None
    if any(is_similar_title(title, st, threshold=seen_title_threshold) for st in seen_titles):
        return None

    if date_tag and date_tag.strftime("%Y.%m.%d") != target_dot:
        return None

    article_html = ""
    soup = None
    article_dt = date_tag
    try:
        article_fetcher = fetch_source_text if source_name in {"AI News", "AI TIMES", "MarketingTech"} else fetch_text
        article_html = article_fetcher(link, timeout=20)
        if not article_dt:
            article_dt = extract_html_datetime(article_html)
        soup = BeautifulSoup(article_html, "html.parser")
    except Exception as e:
        print(f"  - {source_name} article fetch failed: {e}")

    if require_article_date:
        if not article_dt or article_dt.strftime("%Y.%m.%d") != target_dot:
            return None
    elif article_dt and article_dt.strftime("%Y.%m.%d") != target_dot:
        return None

    body = extract_best_article_text(soup) if soup else ""
    if soup:
        title = clean_source_article_title(extract_page_title(soup) or title, source_name)
    summary_source = body if len(body) >= 180 else desc_text
    if story_cache is not None:
        for cached in story_cache:
            if is_similar_title(title, cached["title"], threshold=cache_title_threshold):
                return None
            if strict_story_dedupe and is_duplicate_story(title, summary_source, cached["title"], cached["text"]):
                return None

    seen_links.add(link)
    seen_titles.append(title)
    if story_cache is not None:
        story_cache.append({"title": title, "text": summary_source})

    item = {
        "title": title,
        "link": link,
        "date": (article_dt or target_date).strftime("%Y.%m.%d"),
        "source": source_name,
        "summary": make_three_line_summary(title, summary_source, source_name, context),
        "_summary_source": summary_source,
        "_summary_context": context,
    }
    if cta_label:
        item["_cta_label"] = cta_label
    return item

def xml_local_name(tag):
    return str(tag).rsplit("}", 1)[-1].lower()

def xml_child_text(node, *names):
    wanted = {name.lower() for name in names}
    for child in list(node):
        if xml_local_name(child.tag) in wanted and child.text:
            return normalize_space(child.text)
    return ""

def parse_rss_feed_items(feed_text):
    items = []
    try:
        root = ElementTree.fromstring(feed_text.lstrip("\ufeff"))
    except Exception:
        # Some WordPress feeds contain malformed markup inside an item.  The
        # HTML parser can recover, but it treats RSS <link> as an empty HTML
        # element. Rename text-style link tags before parsing so URLs survive.
        recoverable_feed = re.sub(r"<link\s*>", "<rss-link>", feed_text, flags=re.IGNORECASE)
        recoverable_feed = re.sub(r"</link\s*>", "</rss-link>", recoverable_feed, flags=re.IGNORECASE)
        with warnings.catch_warnings():
            warnings.simplefilter("ignore", XMLParsedAsHTMLWarning)
            soup = BeautifulSoup(recoverable_feed, "html.parser")
        for node in soup.find_all(["item", "entry"]):
            title_node = node.find("title")
            title = normalize_space(title_node.get_text(" ", strip=True)) if title_node else ""
            link_node = node.find("rss-link")
            if link_node is None:
                link_node = node.find("link")
            link = ""
            if link_node is not None:
                link = link_node.get("href", "").strip() or normalize_space(link_node.get_text(" ", strip=True))
            date_value = extract_feed_item_date(node)
            desc_node = node.find(["description", "summary", "encoded", "content"])
            desc_text = desc_node.get_text(" ", strip=True) if desc_node else ""
            categories = [
                normalize_space(category.get_text(" ", strip=True))
                for category in node.find_all("category")
                if category.get_text(" ", strip=True)
            ]
            items.append({
                "title": title,
                "link": link,
                "date": date_value,
                "description": desc_text,
                "categories": categories,
            })
        return items
    for node in root.iter():
        if xml_local_name(node.tag) not in {"item", "entry"}:
            continue
        title = xml_child_text(node, "title")
        link = ""
        for child in list(node):
            if xml_local_name(child.tag) != "link":
                continue
            href = child.attrib.get("href", "").strip()
            link = href or normalize_space(child.text or "")
            if link:
                break
        date_text = xml_child_text(node, "pubdate", "published", "updated", "date")
        desc_text = xml_child_text(node, "description", "summary", "encoded", "content")
        categories = [
            normalize_space(child.text or "")
            for child in list(node)
            if xml_local_name(child.tag) == "category" and child.text
        ]
        items.append({
            "title": title,
            "link": link,
            "date": parse_datetime_string(date_text),
            "description": desc_text,
            "categories": categories,
        })
    return items

def collect_listing_article_links(page_url, link_pattern, use_browser_headers=False, attempts=1, timeout=20):
    page_fetcher = fetch_text if use_browser_headers else fetch_source_text
    last_error = None
    for attempt in range(1, max(1, attempts) + 1):
        try:
            page_html = page_fetcher(page_url, timeout=timeout)
            soup = BeautifulSoup(page_html, "html.parser")
            items = []
            seen = set()
            for anchor in soup.find_all("a", href=True):
                href = anchor.get("href", "").strip()
                if not href or href.startswith(("javascript:", "mailto:", "#")):
                    continue
                link = clean_tracking_url(urllib.parse.urljoin(page_url, href))
                if not re.search(link_pattern, urllib.parse.urlparse(link).path):
                    continue
                if link in seen:
                    continue
                title = normalize_space(anchor.get_text(" ", strip=True))
                title = re.sub(r"^기사\s*이미지\s*", "", title)
                title = re.sub(r"^기사이미지\s*\d*\s*", "", title)
                title = re.sub(r"^\d+\s*", "", title)
                if len(title) < 6:
                    continue
                seen.add(link)
                items.append({"title": title, "link": link})
            return items
        except Exception as exc:
            last_error = exc
            if attempt < max(1, attempts):
                print(f"  - VC/AC/대체투자 listing retry {attempt}/{attempts} ({page_url}): {exc}")
                time.sleep(min(2 ** (attempt - 1), 4))
    print(f"  - VC/AC/대체투자 listing failed ({page_url}): {last_error}")
    return []


def fetch_vcac_google_news_fallback(
    config, target_date, seen_links, seen_titles, story_cache, limit=MAX_VCAC_NEWS_PER_SOURCE
):
    source_name = config["source"]
    context = config["context"]
    start_date = target_date.strftime("%Y-%m-%d")
    end_date = (target_date + timedelta(days=1)).strftime("%Y-%m-%d")
    target_dot = target_date.strftime("%Y.%m.%d")
    query = urllib.parse.quote(f"{config['fallback_google_query']} after:{start_date} before:{end_date}")
    rss_url = f"https://news.google.com/rss/search?q={query}&hl=ko&gl=KR&ceid=KR:ko"
    news_items = []
    try:
        root = ElementTree.fromstring(fetch_text(rss_url, timeout=25))
        for item in root.findall(".//item"):
            if len(news_items) >= limit:
                break
            try:
                published = parsedate_to_datetime(item.findtext("pubDate", "")).astimezone(KST)
            except Exception:
                continue
            if published.strftime("%Y.%m.%d") != target_dot:
                continue
            title, result_source = parse_google_news_item(item)
            if not is_valid_vcac_title(title):
                continue
            google_link = normalize_space(item.findtext("link", ""))
            article_link = resolve_google_news_url(google_link)
            resolved_host = normalize_news_netloc(article_link)
            if not resolved_host.endswith("unicornfactory.co.kr"):
                if "유니콘팩토리" not in normalize_space(result_source):
                    continue
                article_link = google_link
            desc_text = strip_tags(item.findtext("description", ""))
            news_item = build_vcac_news_item(
                source_name,
                title,
                article_link,
                target_date,
                seen_links,
                seen_titles,
                context,
                desc_text=desc_text,
                story_cache=story_cache,
                require_article_date=False,
            )
            if news_item:
                news_items.append(news_item)
    except Exception as exc:
        print(f"  - {source_name} Google News fallback failed: {exc}")
    if news_items:
        print(f"  - {source_name} Google News fallback: {len(news_items)}건")
    return news_items

def build_vcac_news_item(source_name, title, link, target_date, seen_links, seen_titles, context, desc_text="", story_cache=None, require_article_date=True):
    target_dot = target_date.strftime("%Y.%m.%d")
    link = clean_tracking_url(link)
    title = clean_source_article_title(title, source_name)
    if not title or not link or link in seen_links:
        return None
    if any(is_similar_title(title, st) for st in seen_titles):
        return None

    article_html = ""
    soup = None
    article_dt = None
    try:
        article_html = fetch_text(link, timeout=20)
        article_dt = extract_html_datetime(article_html)
        soup = BeautifulSoup(article_html, "html.parser")
    except Exception as e:
        print(f"  - {source_name} article fetch failed: {e}")

    if require_article_date:
        if not article_dt or article_dt.strftime("%Y.%m.%d") != target_dot:
            return None
    elif article_dt and article_dt.strftime("%Y.%m.%d") != target_dot:
        return None

    body = extract_best_article_text(soup) if soup else ""
    if soup:
        title = clean_source_article_title(extract_page_title(soup) or title, source_name)
    summary_source = body if len(body) >= 180 else desc_text
    if story_cache is not None and any(
        is_duplicate_story(title, summary_source, cached["title"], cached["text"])
        for cached in story_cache
    ):
        return None

    seen_links.add(link)
    seen_titles.append(title)
    if story_cache is not None:
        story_cache.append({"title": title, "text": summary_source})
    return {
        "title": title,
        "link": link,
        "date": target_dot,
        "source": source_name,
        "summary": make_three_line_summary(title, summary_source, source_name, context),
        "_summary_source": summary_source,
        "_summary_context": context,
    }

def fetch_vcac_rss_source(config, target_date, seen_links, seen_titles):
    source_name = config["source"]
    context = config["context"]
    target_dot = target_date.strftime("%Y.%m.%d")
    news_items = []
    story_cache = []
    for feed_url in config["feeds"]:
        if len(news_items) >= MAX_VCAC_NEWS_PER_SOURCE:
            break
        try:
            feed_fetcher = fetch_text if config.get("use_browser_headers", False) else fetch_source_text
            feed_text = feed_fetcher(feed_url, timeout=20)
            for item in parse_rss_feed_items(feed_text):
                if len(news_items) >= MAX_VCAC_NEWS_PER_SOURCE:
                    break
                title = item["title"]
                link = clean_tracking_url(item["link"])
                if not title or not link:
                    continue
                date_tag = item["date"]
                if date_tag and date_tag.strftime("%Y.%m.%d") != target_dot:
                    continue
                desc_text = strip_tags(item.get("description", ""))
                news_item = build_vcac_news_item(
                    source_name,
                    title,
                    link,
                    target_date,
                    seen_links,
                    seen_titles,
                    context,
                    desc_text=desc_text,
                    story_cache=story_cache,
                    require_article_date=not bool(date_tag),
                )
                if news_item:
                    news_items.append(news_item)
                time.sleep(0.8)
        except Exception as e:
            print(f"  - {source_name} RSS failed ({feed_url}): {e}")
    return dedupe_news_items(news_items)

def fetch_vcac_listing_source(config, target_date, seen_links, seen_titles):
    source_name = config["source"]
    context = config["context"]
    news_items = []
    story_cache = []
    for page_url in config["pages"]:
        if len(news_items) >= MAX_VCAC_NEWS_PER_SOURCE:
            break
        for item in collect_listing_article_links(
            page_url,
            config["link_pattern"],
            use_browser_headers=config.get("use_browser_headers", False),
            attempts=config.get("listing_attempts", 1),
            timeout=config.get("listing_timeout", 20),
        ):
            if len(news_items) >= MAX_VCAC_NEWS_PER_SOURCE:
                break
            news_item = build_vcac_news_item(
                source_name,
                item["title"],
                item["link"],
                target_date,
                seen_links,
                seen_titles,
                context,
                story_cache=story_cache,
                require_article_date=True,
            )
            if news_item:
                news_items.append(news_item)
            time.sleep(0.8)
    if config.get("fallback_google_query") and len(news_items) < MAX_VCAC_NEWS_PER_SOURCE:
        news_items.extend(
            fetch_vcac_google_news_fallback(
                config,
                target_date,
                seen_links,
                seen_titles,
                story_cache,
                limit=MAX_VCAC_NEWS_PER_SOURCE - len(news_items),
            )
        )
    return dedupe_news_items(news_items)

def fetch_dealsite_category_html(category_code, start_date, end_date):
    page_url = f"https://dealsite.co.kr/categories/{category_code}"
    api_url = "https://dealsite.co.kr/api/articles/categoryNews"
    params = {
        "categoryCode": category_code,
        "page": 0,
        "size": 30,
        "pageBlockSize": 10,
        "startDt": start_date.strftime("%Y-%m-%d"),
        "endDt": end_date.strftime("%Y-%m-%d"),
    }

    if requests is not None:
        session = requests.Session()
        page_response = session.get(page_url, headers=HEADERS, timeout=20)
        page_response.raise_for_status()
        page_soup = BeautifulSoup(
            decode_response_body(
                page_response.content,
                extract_charset_from_content_type(page_response.headers.get("content-type", "")),
            ),
            "html.parser",
        )
        token_node = page_soup.select_one('meta[name="_csrf"]')
        header_node = page_soup.select_one('meta[name="_csrf_header"]')
        if not token_node or not token_node.get("content"):
            raise RuntimeError("딜사이트 CSRF 토큰을 찾지 못했습니다.")
        csrf_header = header_node.get("content", "X-XSRF-TOKEN") if header_node else "X-XSRF-TOKEN"
        api_headers = {
            **HEADERS,
            "Accept": "application/json",
            "Referer": page_url,
            "X-Requested-With": "XMLHttpRequest",
            csrf_header: token_node["content"],
        }
        api_response = session.get(api_url, params=params, headers=api_headers, timeout=20)
        api_response.raise_for_status()
        return api_response.json().get("articlesHtml", "")

    cookie_jar = http.cookiejar.CookieJar()
    opener = urllib.request.build_opener(urllib.request.HTTPCookieProcessor(cookie_jar))
    with opener.open(urllib.request.Request(page_url, headers=HEADERS), timeout=20) as response:
        page_text = decode_response_body(response.read(), response.headers.get_content_charset())
    page_soup = BeautifulSoup(page_text, "html.parser")
    token_node = page_soup.select_one('meta[name="_csrf"]')
    header_node = page_soup.select_one('meta[name="_csrf_header"]')
    if not token_node or not token_node.get("content"):
        raise RuntimeError("딜사이트 CSRF 토큰을 찾지 못했습니다.")
    csrf_header = header_node.get("content", "X-XSRF-TOKEN") if header_node else "X-XSRF-TOKEN"
    request_url = f"{api_url}?{urllib.parse.urlencode(params)}"
    api_headers = {
        **HEADERS,
        "Accept": "application/json",
        "Referer": page_url,
        "X-Requested-With": "XMLHttpRequest",
        csrf_header: token_node["content"],
    }
    with opener.open(urllib.request.Request(request_url, headers=api_headers), timeout=20) as response:
        payload = json.loads(decode_response_body(response.read(), response.headers.get_content_charset()))
    return payload.get("articlesHtml", "")

def parse_dealsite_category_items(articles_html, category_name):
    items = []
    seen_article_ids = set()
    soup = BeautifulSoup(articles_html or "", "html.parser")
    for node in soup.select(".mnm-news"):
        title_node = node.select_one("a.ss-news-top-title[href]")
        if not title_node:
            continue
        link = urllib.parse.urljoin("https://dealsite.co.kr", title_node.get("href", ""))
        article_match = re.search(r"/articles/(\d+)", urllib.parse.urlparse(link).path)
        if not article_match or article_match.group(1) in seen_article_ids:
            continue
        title = normalize_space(title_node.get_text(" ", strip=True))
        date_nodes = node.select(".mnm-news-info span")
        date_text = normalize_space(date_nodes[-1].get_text(" ", strip=True)) if date_nodes else ""
        article_date = parse_datetime_string(date_text)
        if not title or not article_date:
            continue
        summary_node = node.select_one("a.mnm-news-txt")
        description = normalize_space(summary_node.get_text(" ", strip=True)) if summary_node else ""
        seen_article_ids.add(article_match.group(1))
        items.append({
            "title": title,
            "link": link,
            "date": article_date,
            "description": description,
            "source": "딜사이트",
            "_dealsite_category": category_name,
            "_article_id": article_match.group(1),
        })
    return items

def score_dealsite_candidate(item, category_config):
    text = normalize_space(f"{item.get('title', '')} {item.get('description', '')}").casefold()
    keyword_score = 0
    for index, keyword in enumerate(category_config.get("keywords", ())):
        if keyword.casefold() in text:
            keyword_score += 10 if index < 8 else 4
    if any(marker in text for marker in ("기자수첩", "칼럼", "인사", "부고")):
        keyword_score -= 25
    ranking_item = {
        "title": item.get("title", ""),
        "source": "딜사이트",
        "_summary_source": item.get("description", ""),
    }
    return score_issue_candidate(ranking_item) + keyword_score

def select_balanced_dealsite_candidates(category_candidates, limit=MAX_NEWS_PER_CATEGORY):
    ranked_by_category = {}
    for config in DEALSITE_CATEGORY_CONFIGS:
        name = config["name"]
        ranked = sorted(
            category_candidates.get(name, []),
            key=lambda item: (score_dealsite_candidate(item, config), item.get("date")),
            reverse=True,
        )
        for item in ranked:
            item["_dealsite_score"] = score_dealsite_candidate(item, config)
        ranked_by_category[name] = ranked

    selected = []
    selected_ids = set()
    category_order = sorted(
        DEALSITE_CATEGORY_CONFIGS,
        key=lambda config: len(ranked_by_category.get(config["name"], [])),
    )
    for config in category_order:
        for item in ranked_by_category.get(config["name"], []):
            if item["_article_id"] in selected_ids:
                continue
            selected.append(item)
            selected_ids.add(item["_article_id"])
            break

    remainder = sorted(
        (
            item
            for ranked in ranked_by_category.values()
            for item in ranked
            if item["_article_id"] not in selected_ids
        ),
        key=lambda item: (item.get("_dealsite_score", 0), item.get("date")),
        reverse=True,
    )
    for item in remainder:
        if len(selected) >= limit:
            break
        if item["_article_id"] in selected_ids:
            continue
        selected.append(item)
        selected_ids.add(item["_article_id"])
    return selected[:limit]

def build_dealsite_news_item(item, seen_links, seen_titles):
    title = normalize_space(item.get("title", ""))
    link = clean_tracking_url(item.get("link", ""))
    article_date = item.get("date")
    if not title or not link or not article_date or link in seen_links:
        return None
    article_html = ""
    soup = None
    try:
        article_html = fetch_text(link, timeout=20)
        soup = BeautifulSoup(article_html, "html.parser")
        title = clean_source_article_title(extract_page_title(soup) or title, "딜사이트")
    except Exception as exc:
        print(f"  - 딜사이트 article fetch failed ({link}): {exc}")
    body = extract_best_article_text(soup) if soup else ""
    summary_source = body if len(body) >= 180 else item.get("description", "")
    category_name = item.get("_dealsite_category", "")
    context = f"딜사이트 {category_name} 카테고리의 주요 거래 및 자본시장 기사입니다."
    seen_links.add(link)
    seen_titles.append(title)
    return {
        "title": title,
        "link": link,
        "date": article_date.strftime("%Y.%m.%d"),
        "source": "딜사이트",
        "summary": make_three_line_summary(title, summary_source, "딜사이트", context),
        "_summary_source": summary_source,
        "_summary_context": context,
        "_dealsite_category": category_name,
        "_dealsite_score": item.get("_dealsite_score", 0),
    }

def fetch_dealsite_vcac_source(target_date, seen_links, seen_titles):
    category_candidates = {}
    for config in DEALSITE_CATEGORY_CONFIGS:
        category_name = config["name"]
        try:
            exact_html = fetch_dealsite_category_html(config["code"], target_date, target_date)
            candidates = parse_dealsite_category_items(exact_html, category_name)
            category_candidates[category_name] = candidates
        except Exception as exc:
            print(f"  - 딜사이트/{category_name} 수집 실패: {exc}")
            category_candidates[category_name] = []

    selected = select_balanced_dealsite_candidates(category_candidates)
    news_items = []
    for item in selected:
        news_item = build_dealsite_news_item(item, seen_links, seen_titles)
        if news_item:
            news_items.append(news_item)
    category_counts = {
        config["name"]: sum(1 for item in news_items if item.get("_dealsite_category") == config["name"])
        for config in DEALSITE_CATEGORY_CONFIGS
    }
    print(f"  - 딜사이트: 대체투자 {category_counts['대체투자']}건 / 인수합병 {category_counts['인수합병']}건")
    return news_items

def fetch_vcac_sources(target_date, seen_links, seen_titles):
    source_news = {source_name: [] for source_name in VCAC_SOURCE_PRIORITY}
    for config in VCAC_LISTING_SOURCE_CONFIGS:
        source_news[config["source"]] = fetch_vcac_listing_source(config, target_date, seen_links, seen_titles)
    for config in VCAC_RSS_SOURCE_CONFIGS:
        source_news[config["source"]] = fetch_vcac_rss_source(config, target_date, seen_links, seen_titles)
    source_news["딜사이트"] = fetch_dealsite_vcac_source(target_date, seen_links, seen_titles)
    return {
        "id": "vcac",
        "label": "VC/AC/대체투자",
        "groups": [
            {
                "title": "VC/AC/대체투자",
                "categories": [
                    {
                        "name": source_name,
                        "news": source_news.get(source_name, []),
                        "preserve_selection": source_name == "딜사이트",
                    }
                    for source_name in VCAC_SOURCE_PRIORITY
                ],
            }
        ],
    }

def collect_ai_news_listing_items(page_url):
    items = []
    seen = set()
    try:
        page_html = fetch_source_text(page_url, timeout=20)
        soup = BeautifulSoup(page_html, "html.parser")
        for node in soup.select(".type-post"):
            text = normalize_space(node.get_text(" ", strip=True))
            date_tag = parse_display_date(text)
            article_links = []
            for anchor in node.find_all("a", href=True):
                href = anchor.get("href", "").strip()
                link = clean_tracking_url(urllib.parse.urljoin(page_url, href))
                parsed = urllib.parse.urlparse(link)
                if parsed.netloc != "www.artificialintelligence-news.com" or "/news/" not in parsed.path:
                    continue
                title = normalize_space(anchor.get_text(" ", strip=True))
                if title:
                    article_links.append((title, link))
            if not article_links:
                continue
            title, link = max(article_links, key=lambda item: len(item[0]))
            if link in seen or len(title) < 6:
                continue
            seen.add(link)
            items.append({"title": title, "link": link, "date": date_tag, "description": text})
    except Exception as e:
        print(f"  - AI News listing failed ({page_url}): {e}")
    return items

def collect_aitimes_listing_items(target_date, max_pages=3):
    items = []
    seen = set()
    base_url = "https://www.aitimes.com/news/articleList.html"
    target_day = target_date.date() if hasattr(target_date, "date") else target_date
    for page in range(1, max_pages + 1):
        page_url = f"{base_url}?page={page}&view_type=sm"
        try:
            page_html = fetch_source_text(page_url, timeout=20)
            soup = BeautifulSoup(page_html, "html.parser")
            page_items = []
            for node in soup.select("li.altlist-text-item, li.altlist-webzine-item"):
                text = normalize_space(node.get_text(" ", strip=True))
                date_tag = parse_aitimes_listing_date(text, target_date)
                title_anchor = None
                for anchor in node.find_all("a", href=True):
                    href = anchor.get("href", "").strip()
                    title = normalize_space(anchor.get_text(" ", strip=True))
                    if "articleView.html" in href and len(title) >= 6:
                        title_anchor = anchor
                        break
                if not title_anchor:
                    continue
                link = clean_tracking_url(urllib.parse.urljoin(page_url, title_anchor.get("href", "")))
                title = normalize_space(title_anchor.get_text(" ", strip=True))
                if not link or link in seen:
                    continue
                seen.add(link)
                item = {"title": title, "link": link, "date": date_tag, "description": text}
                items.append(item)
                page_items.append(item)
            if page_items and all(item.get("date") and item["date"].date() < target_day for item in page_items):
                break
        except Exception as e:
            print(f"  - AI TIMES listing failed ({page_url}): {e}")
            break
    return items

def collect_the_batch_listing_items(page_url, required_path_prefix="", exclude_issue_links=False):
    items = []
    seen = set()
    try:
        page_html = fetch_text(page_url, timeout=20)
        soup = BeautifulSoup(page_html, "html.parser")
        for article in soup.select("main article"):
            text = normalize_space(article.get_text(" ", strip=True))
            date_tag = parse_display_date(text)
            link = ""
            for anchor in article.find_all("a", href=True):
                href = anchor.get("href", "").strip()
                absolute = clean_tracking_url(urllib.parse.urljoin(page_url, href))
                path = urllib.parse.urlparse(absolute).path.rstrip("/")
                if not path.startswith("/the-batch/") or "/tag/" in path or path == "/the-batch":
                    continue
                if required_path_prefix and not path.startswith(required_path_prefix):
                    continue
                if exclude_issue_links and path.startswith("/the-batch/issue-"):
                    continue
                link = absolute
                break
            if not link or link in seen:
                continue
            title_node = article.find(["h1", "h2", "h3"])
            title = normalize_space(title_node.get_text(" ", strip=True)) if title_node else ""
            if not title:
                for anchor in article.find_all("a", href=True):
                    candidate = normalize_space(anchor.get_text(" ", strip=True))
                    if len(candidate) > len(title):
                        title = candidate
            if len(title) < 6:
                continue
            seen.add(link)
            items.append({"title": title, "link": link, "date": date_tag, "description": text})
    except Exception as e:
        print(f"  - The Batch listing failed ({page_url}): {e}")
    return items

def fetch_ai_rss_source(config, target_date, seen_links, seen_titles):
    source_name = config["source"]
    context = config["context"]
    target_dot = target_date.strftime("%Y.%m.%d")
    news_items = []
    story_cache = []
    for feed_url in config["feeds"]:
        if len(news_items) >= MAX_AI_NEWS_PER_SOURCE:
            break
        try:
            feed_text = fetch_source_text(feed_url, timeout=20)
            for item in parse_rss_feed_items(feed_text):
                if len(news_items) >= MAX_AI_NEWS_PER_SOURCE:
                    break
                date_tag = item["date"]
                if date_tag and date_tag.strftime("%Y.%m.%d") != target_dot:
                    continue
                news_item = build_source_news_item(
                    source_name,
                    item["title"],
                    item["link"],
                    target_date,
                    seen_links,
                    seen_titles,
                    context,
                    desc_text=strip_tags(item.get("description", "")),
                    story_cache=story_cache,
                    date_tag=date_tag,
                    require_article_date=not bool(date_tag),
                    strict_story_dedupe=False,
                    seen_title_threshold=0.50,
                    cache_title_threshold=0.50,
                )
                if news_item:
                    news_items.append(news_item)
                time.sleep(0.8)
        except Exception as e:
            print(f"  - {source_name} RSS failed ({feed_url}): {e}")
    return news_items

def fetch_ai_listing_items(source_name, listing_items, target_date, seen_links, seen_titles, context, limit=MAX_AI_NEWS_PER_SOURCE, cta_label="", accepted_dates=None, latest_on_or_before=False):
    target_dot = target_date.strftime("%Y.%m.%d")
    allowed_dates = set(accepted_dates or {target_dot})
    news_items = []
    story_cache = []
    for item in listing_items:
        if len(news_items) >= limit:
            break
        date_tag = item.get("date")
        item_target_date = target_date
        if date_tag:
            date_key = date_tag.strftime("%Y.%m.%d")
            if latest_on_or_before:
                if date_tag.date() > target_date:
                    continue
            elif date_key not in allowed_dates:
                continue
            item_target_date = date_tag.date()
        news_item = build_source_news_item(
            source_name,
            item.get("title", ""),
            item.get("link", ""),
            item_target_date,
            seen_links,
            seen_titles,
            context,
            desc_text=item.get("description", ""),
            story_cache=story_cache,
            date_tag=date_tag,
            require_article_date=not bool(date_tag),
            cta_label=cta_label,
            strict_story_dedupe=False,
            seen_title_threshold=0.50,
            cache_title_threshold=0.50,
        )
        if news_item:
            news_items.append(news_item)
        time.sleep(0.8)
    return news_items

def fetch_ai_sources(target_date, seen_links, seen_titles):
    source_news = {source_name: [] for source_name in AI_SOURCE_PRIORITY}
    source_news["AI News"] = fetch_ai_listing_items(
        "AI News",
        collect_ai_news_listing_items("https://www.artificialintelligence-news.com/"),
        target_date,
        seen_links,
        seen_titles,
        "AI News의 글로벌 AI 산업 및 기술 뉴스입니다.",
    )
    source_news["AI TIMES"] = fetch_ai_listing_items(
        "AI TIMES",
        collect_aitimes_listing_items(target_date),
        target_date,
        seen_links,
        seen_titles,
        "AI TIMES의 국내외 AI 산업, 기업, 기술 기사입니다.",
    )
    for config in AI_RSS_SOURCE_CONFIGS:
        source_news[config["source"]] = fetch_ai_rss_source(config, target_date, seen_links, seen_titles)
    source_news["The Batch Data Points"] = fetch_ai_listing_items(
        "The Batch Data Points",
        collect_the_batch_listing_items("https://www.deeplearning.ai/the-batch/tag/data-points", exclude_issue_links=True),
        target_date,
        seen_links,
        seen_titles,
        "DeepLearning.AI The Batch Data Points의 AI 주요 뉴스 브리핑입니다.",
    )
    source_news["The Batch Weekly Issues"] = fetch_ai_listing_items(
            "The Batch Weekly Issues",
            collect_the_batch_listing_items("https://www.deeplearning.ai/the-batch", required_path_prefix="/the-batch/issue-"),
            target_date,
            seen_links,
            seen_titles,
            "DeepLearning.AI The Batch의 주간 AI 이슈 요약입니다.",
            limit=1,
            latest_on_or_before=True,
            cta_label="원문 링크",
        )
    return {
        "id": "ai",
        "label": "AI",
        "groups": [
            {
                "title": "AI",
                "categories": [
                    {"name": source_name, "news": source_news.get(source_name, [])}
                    for source_name in AI_SOURCE_PRIORITY
                ],
            }
        ],
    }

def parse_causeartist_listing_items(html_text, page_url):
    soup = BeautifulSoup(html_text, "html.parser")
    items = []
    for anchor in soup.select('a[href^="/blog/"], a[href^="/case-studies/"], a[href^="/podcast/"]'):
        href = anchor.get("href", "").strip()
        if not href:
            continue
        link = urllib.parse.urljoin(page_url, href)
        if any(skip in link for skip in ["/about", "/contact", "/privacy", "/tag/", "/companies/", "/funders/"]):
            continue
        title_node = anchor.select_one("span.font-semibold")
        title = normalize_space(title_node.get_text(" ", strip=True)) if title_node else normalize_space(anchor.get_text(" ", strip=True))
        if not title or len(title) < 5:
            continue
        date_text = ""
        for span in anchor.find_all("span"):
            span_text = normalize_space(span.get_text(" ", strip=True))
            if re.match(r"^[A-Z][a-z]+ \d{1,2}, \d{4}$", span_text):
                date_text = span_text
                break
        if not date_text:
            continue
        items.append({"title": title, "link": link, "date_text": date_text})
    return items

def fetch_causeartist_news(target_date, seen_links, seen_titles):
    pages = [
        "https://www.causeartist.com/",
        "https://www.causeartist.com/blog",
        "https://www.causeartist.com/case-studies",
        "https://www.causeartist.com/podcast",
    ]
    news_items = []
    seen_page_links = set()
    for page_url in pages:
        try:
            page_html = fetch_text(page_url, timeout=20)
            for item in parse_causeartist_listing_items(page_html, page_url):
                if item["link"] in seen_page_links:
                    continue
                seen_page_links.add(item["link"])
                if item["link"] in seen_links:
                    continue
                try:
                    listed_dt = datetime.strptime(item["date_text"], "%b %d, %Y").date()
                except Exception:
                    continue
                if listed_dt != target_date:
                    continue
                if any(is_similar_title(item["title"], st) for st in seen_titles):
                    continue
                try:
                    article_html = fetch_text(item["link"], timeout=20)
                    soup = BeautifulSoup(article_html, "html.parser")
                    title = extract_page_title(soup) or item["title"]
                    body = extract_best_article_text(soup)
                    summary = make_three_line_summary(title, body, "Causeartist", "Global impact and sustainability content.")
                    news_items.append({
                        "title": title,
                        "link": item["link"],
                        "date": target_date.strftime("%Y.%m.%d"),
                        "source": "Causeartist",
                        "summary": summary,
                        "_summary_source": body,
                        "_summary_context": "Global impact and sustainability content.",
                    })
                    seen_links.add(item["link"])
                    seen_titles.append(title)
                except Exception as e:
                    print(f"  - Causeartist article failed: {e}")
            time.sleep(1.5)
        except Exception as e:
            print(f"  - Causeartist page failed ({page_url}): {e}")
    return news_items

def fetch_trellis_news(target_date, seen_links, seen_titles):
    try:
        from trellis_scraper import collect_yesterday_articles
    except Exception as e:
        print(f"  - Trellis module failed: {e}")
        return []
    news_items = []
    try:
        articles = collect_yesterday_articles(target_date=target_date, delay_seconds=3.5, limit=None)
        for article in articles:
            link = article.url
            if link in seen_links:
                continue
            if any(is_similar_title(article.title, st) for st in seen_titles):
                continue
            title = article.title or link
            body = article.content or article.description or ""
            summary = make_three_line_summary(title, body, "Trellis", "Global climate and impact news.")
            news_items.append({
                "title": title,
                "link": link,
                "date": target_date.strftime("%Y.%m.%d"),
                "source": "Trellis",
                "summary": summary,
                "_summary_source": body,
                "_summary_context": "Global climate and impact news.",
            })
            seen_links.add(link)
            seen_titles.append(title)
    except Exception as e:
        print(f"  - Trellis failed: {e}")
    return news_items

def fetch_google_news_for_category(target_date, section_id, group_title, category, seen_links, seen_titles, previous_titles=None, limit=MAX_NEWS_PER_CATEGORY, forced_source=None):
    candidates = []
    candidate_links = set()
    existing_titles = list(seen_titles)
    target_dot = target_date.strftime("%Y.%m.%d")
    start_date = target_date.strftime("%Y-%m-%d")
    end_date = (target_date + timedelta(days=1)).strftime("%Y-%m-%d")
    try:
        search_query = f"({category['query']})"
        if section_id == "macro":
            search_query = f"{search_query} ({MACRO_ALLOWED_SOURCE_QUERY})"
        query = urllib.parse.quote(
            f"{search_query} after:{start_date} before:{end_date} -블로그 -카페 -blog -cafe"
        )
        rss_text = fetch_text(f"https://news.google.com/rss/search?q={query}&hl=ko&gl=KR")
        for item in ElementTree.fromstring(rss_text).findall(".//item"):
            if len(candidates) >= MAX_CANDIDATE_NEWS_PER_CATEGORY:
                break
            title, source_name = parse_google_news_item(item)
            source_name = normalize_source_name(source_name)
            try:
                if parsedate_to_datetime(item.findtext("pubDate", "")).astimezone(KST).strftime("%Y.%m.%d") != target_dot:
                    continue
            except:
                continue
            desc_text = strip_tags(item.findtext("description", ""))
            if section_id == "macro" and not is_macro_news_match(group_title, category["name"], title):
                continue
            google_link = item.findtext("link", "")
            article_link = resolve_google_news_url(google_link)
            link = article_link or google_link
            if section_id == "macro":
                if not is_allowed_macro_source(link):
                    continue
                source_name = normalize_macro_source_name(link, source_name)
            
            if section_id == "vcac" and not is_valid_vcac_title(title):
                continue
            if should_skip_search_item(section_id, category["name"], source_name, title, link):
                continue
            if link in seen_links or google_link in seen_links or link in candidate_links or google_link in candidate_links:
                continue
            if any(is_similar_title(title, existing, threshold=0.40) for existing in existing_titles):
                continue
            article_body = fetch_article_body_text(article_link)
            summary_source = article_body if len(article_body) >= 180 else desc_text
            candidate_links.add(link)
            candidate_links.add(google_link)
            candidates.append({
                "title": title,
                "link": link,
                "source": forced_source or source_name,
                "date": target_dot,
                "_summary_source": summary_source,
                "_summary_context": category["context"],
                "_google_link": google_link,
            })
    except Exception as e:
        print("수집 오류:", e)
    ranked = cluster_and_rank_issues(candidates, previous_titles)
    ranked = refine_issue_ranking_with_gemini(ranked, section_id, group_title, category["name"])
    eligible = [item for item in ranked if item.get("_ai_relevance", 100) >= 45]
    selected = (eligible or ranked)[:limit]
    for news in selected:
        news["summary"] = make_three_line_summary(
            news.get("title", ""),
            news.get("_summary_source", ""),
            news.get("source", ""),
            category["context"],
        )
        seen_links.add(news.get("link", ""))
        if news.get("_google_link"):
            seen_links.add(news["_google_link"])
        seen_titles.append(news.get("title", ""))
    if candidates:
        print(
            f"  - {group_title}/{category['name']}: 후보 {len(candidates)}건 → "
            f"이슈 {len(ranked)}개 → 주요 뉴스 {len(selected)}건"
        )
    return selected

def fetch_search_sections(target_date, seen_links, seen_titles):
    previous_titles = load_recent_briefing_titles(target_date)
    results = [
        rank_existing_section_categories(
            fetch_vcac_sources(target_date, seen_links, seen_titles),
            previous_titles,
        ),
        rank_existing_section_categories(
            fetch_ai_sources(target_date, seen_links, seen_titles),
            previous_titles,
        ),
    ]
    for section in SEARCH_SECTIONS:
        section_result = {"id": section["id"], "label": section["label"], "groups": []}
        for group in section["groups"]:
            group_result = {"title": group["title"], "categories": []}
            for category in group["categories"]:
                news_list = fetch_google_news_for_category(
                    target_date,
                    section["id"],
                    group["title"],
                    category,
                    seen_links,
                    seen_titles,
                    previous_titles,
                )
                group_result["categories"].append({"name": category["name"], "news": news_list})
            section_result["groups"].append(group_result)
        results.append(section_result)
    return results

# ==========================================
# 🌟 HTML 렌더링
# ==========================================
def render_html(target_date, domestic_impact, global_impact, search_sections, target_dash, dashboard, strong_theme, chart_data, industry_trend=None, industry_source_trend=None):
    target_dot = target_date.strftime("%Y.%m.%d")
    current_kst = datetime.now(KST)
    updated_at = current_kst.strftime("%Y.%m.%d %H:%M")
    updated_dot = current_kst.strftime("%Y.%m.%d")
    section_map = {section["id"]: section for section in search_sections}

    counts = {s["id"]: sum(len(c["news"]) for g in s["groups"] for c in g["categories"]) for s in search_sections}
    counts["indicators"] = 4
    counts["impact"] = len(domestic_impact) + len(global_impact)
    counts["theme"] = 1 if strong_theme and strong_theme["name"] != "강세테마 대기중" else 0
    industry_items = industry_trend if isinstance(industry_trend, list) else (industry_trend or {}).get("items", [])
    industry_source_items = industry_source_trend if isinstance(industry_source_trend, list) else (industry_source_trend or {}).get("items", [])
    counts["industrytrend"] = len(industry_source_items)
    counts["industry"] = len(industry_items)

    chart_json = json.dumps(chart_data, ensure_ascii=False)
    esc = html.escape

    def source_key(source_name):
        return re.sub(r"[^0-9a-z가-힣]+", "-", normalize_space(source_name).lower()).strip("-") or "impact-source"

    def format_flow_html(flow_text):
        parts = []
        for piece in normalize_space(flow_text).split("/"):
            segment = normalize_space(piece)
            if not segment:
                continue
            if " " not in segment:
                parts.append(esc(segment))
                continue
            label, value = segment.split(" ", 1)
            css_class = "neutral"
            if value.startswith("+"):
                css_class = "up"
            elif value.startswith("-"):
                css_class = "down"
            parts.append(f'{esc(label)} <span class="flow-value {css_class}">{esc(value)}</span>')
        return " / ".join(parts)

    def render_news_card(news):
        summary_html = "".join(f"<li>{esc(str(line))}</li>" for line in news.get("summary", []))
        if not summary_html:
            summary_html = "<li>요약 정보가 없습니다.</li>"
        action_html = ""
        if news.get("_cta_label") and news.get("link"):
            action_html = (
                f'<div class="news-actions">'
                f'<a class="news-action-link" href="{esc(news.get("link", ""))}" target="_blank" rel="noopener noreferrer">'
                f'{esc(news.get("_cta_label", "원문 보기"))}'
                f'</a>'
                f'</div>'
            )
        return (
            f'<article class="news-card">'
            f'<div class="news-title"><a href="{esc(news.get("link", ""))}" target="_blank" rel="noopener noreferrer">{esc(news.get("title", ""))}</a></div>'
            f'<div class="news-date">출처: {esc(news.get("source", ""))} | 발행일: {esc(news.get("date", ""))}</div>'
            f'<ul class="news-summary">{summary_html}</ul>'
            f'{action_html}'
            f'</article>'
        )

    def render_news_list(news_items, empty_message):
        if not news_items:
            return f'<div class="empty-state">{esc(empty_message)}</div>'
        return "".join(render_news_card(news) for news in news_items)

    def render_industry_trend_section(items):
        items = items if isinstance(items, list) else (items or {}).get("items", [])

        def render_industry_item(item):
            chart_img = ""
            if item.get("source") == "McKinsey" and item.get("chart_image_url"):
                chart_img = (
                    f'<div class="industry-chart-image">'
                    f'<img src="{esc(item.get("chart_image_url", ""))}" alt="{esc(item.get("chart_image_alt") or item.get("title", ""))}" loading="lazy">'
                    f'</div>'
                )
            summary_lines = item.get("summary", [])
            summary_html = "".join(f"<li>{esc(str(line))}</li>" for line in summary_lines)
            description = item.get("description_ko") or item.get("description_en") or ""
            if not summary_html:
                summary_html = f"<li>{esc(description)}</li>" if description else ""
            source_label = item.get("source", "MBB")
            if item.get("source") == "McKinsey":
                report_link = ""
                if item.get("report_url"):
                    report_label = item.get("report_title") or "원본 보고서 보기"
                    report_link = (
                        f'<a class="industry-report-link" href="{esc(item.get("report_url", ""))}" target="_blank" rel="noopener noreferrer">'
                        f'원본 보고서 보기: {esc(report_label)}'
                        f'</a>'
                    )
                return f"""
                    <article class="industry-card">
                        <div class="industry-meta">
                            <span>McKinsey · The Week in Charts</span>
                            <span>{esc(item.get("date", ""))}</span>
                        </div>
                        <h3>{esc(item.get("title", ""))}</h3>
                        <p class="industry-description">{esc(description)}</p>
                        {chart_img}
                        <div class="industry-source-note">Source: {esc(source_label)}</div>
                        <div class="industry-actions">
                            <a class="industry-report-link secondary" href="{esc(item.get("source_url", ""))}" target="_blank" rel="noopener noreferrer">Week in Charts 원문 보기</a>
                            {report_link}
                        </div>
                    </article>
                """
            return f"""
                <article class="news-card mbb-news-card">
                    <div class="news-title"><a href="{esc(item.get("source_url", ""))}" target="_blank" rel="noopener noreferrer">{esc(item.get("title", ""))}</a></div>
                    <div class="news-date">출처: {esc(source_label)} | 발행일: {esc(item.get("date", ""))}</div>
                    <ul class="news-summary industry-summary">{summary_html}</ul>
                    {chart_img}
                </article>
            """

        if not items:
            body = '<div class="empty-state">해당 날짜에 발행된 MBB 인사이트가 없습니다.</div>'
        else:
            source_order = ("McKinsey", "Bain & Company", "BCG")
            source_groups = {source: [] for source in source_order}
            for item in items:
                source_groups.setdefault(item.get("source", "MBB"), []).append(item)
            ordered_sources = list(source_order) + [
                source for source in source_groups if source not in source_order
            ]
            default_source = next((source for source in ordered_sources if source_groups.get(source)), ordered_sources[0])
            mbb_branding = {
                "McKinsey": ("mckinsey", '<span class="impact-source-logo mbb-wordmark mbb-wordmark-mckinsey">McKinsey <small>&amp; Company</small></span>'),
                "Bain & Company": ("bain", '<span class="impact-source-logo mbb-logo-image"><img src="https://www.bain.com/contentassets/0b88e3e10a7b4592809517c28b75847e/logo_red_bain.svg" alt="Bain &amp; Company 로고" loading="lazy"></span>'),
                "BCG": ("bcg", '<span class="impact-source-logo mbb-wordmark mbb-wordmark-bcg">Boston Consulting Group</span>'),
            }
            source_buttons = []
            source_panels = []
            for source in ordered_sources:
                key = f"industry-{source_key(source)}"
                active_class = " active" if source == default_source else ""
                brand_class, logo_html = mbb_branding.get(
                    source,
                    ("generic", f'<span class="impact-source-logo fallback-logo">{esc(source)}</span>'),
                )
                panel_logo_html = logo_html.replace("impact-source-logo", "impact-source-logo panel-logo", 1)
                source_buttons.append(
                    f'<button class="impact-source-card impact-brand-{esc(brand_class)}{active_class}" data-source-target="{esc(key)}">'
                    f'{logo_html}'
                    f'<strong>{esc(source)}</strong>'
                    f'<span class="impact-source-count">{len(source_groups.get(source, []))}건</span>'
                    f'</button>'
                )
                source_body = "".join(render_industry_item(item) for item in source_groups.get(source, []))
                if not source_body:
                    source_body = '<div class="empty-state">해당 날짜에 발행된 인사이트가 없습니다.</div>'
                source_panels.append(
                    f'<div class="impact-news-panel{active_class}" data-source-panel="{esc(key)}">'
                    f'<div class="impact-panel-head">{panel_logo_html}<h3>{esc(source)}</h3></div>'
                    f'<div class="industry-list">{source_body}</div>'
                    f'</div>'
                )
            body = (
                f'<div class="impact-source-strip mbb-source-strip">{"".join(source_buttons)}</div>'
                f'<div class="impact-news-stage">{"".join(source_panels)}</div>'
            )
        return f"""
        <section id="section-industry" class="content-section source-tab-section">
            <div class="panel-shell">
                <div class="panel-header">
                    <div>
                        <div class="panel-kicker">MBB Insights</div>
                        <h2>MBB 인사이트</h2>
                    </div>
                    <div class="panel-count">{counts.get("industry", 0)}건</div>
                </div>
                {body}
            </div>
        </section>
        """

    def render_chart_panel(chart_id):
        return (
            f'<div class="chart-panel">'
            f'<div class="chart-canvas" id="{esc(chart_id)}"></div>'
            f'</div>'
        )

    def render_indicator_card(title, value, detail, chart_id, tone_class):
        detail_html = f'<div class="metric-detail">{detail}</div>' if detail else ""
        return (
            f'<article class="indicator-card {tone_class}">'
            f'<div class="indicator-summary">'
            f'<div class="metric-label">{esc(title)}</div>'
            f'<div class="metric-value">{esc(value)}</div>'
            f'{detail_html}'
            f'</div>'
            f'{render_chart_panel(chart_id)}'
            f'</article>'
        )

    def render_generic_section(section):
        section_blocks = []
        multi_group = len(section["groups"]) > 1
        for group in section["groups"]:
            category_blocks = []
            for category in group["categories"]:
                category_blocks.append(
                    f'<article class="story-card">'
                    f'<div class="story-label">{esc(category["name"])}</div>'
                    f'{render_news_list(category["news"], "수집된 뉴스가 없습니다.")}'
                    f'</article>'
                )
            body = "".join(category_blocks) or '<div class="empty-state">수집된 뉴스가 없습니다.</div>'
            if multi_group:
                section_blocks.append(
                    f'<div class="story-group">'
                    f'<div class="story-group-title">{esc(group["title"])}</div>'
                    f'{body}'
                    f'</div>'
                )
            else:
                section_blocks.append(body)
        return (
            f'<section id="section-{esc(section["id"])}" class="content-section">'
            f'<div class="panel-shell">'
            f'<div class="panel-header">'
            f'<div><div class="panel-kicker">{esc(section["label"])}</div><h2>{esc(section["label"])} 브리핑</h2></div>'
            f'<div class="panel-count">{counts.get(section["id"], 0)}건</div>'
            f'</div>'
            f'<div class="story-board">{"".join(section_blocks)}</div>'
            f'</div>'
            f'</section>'
        )

    impact_groups = {}
    for news in domestic_impact + global_impact:
        impact_groups.setdefault(news["source"], []).append(news)

    impact_priority = ["임팩트온", "소셜임팩트뉴스", "이로운넷", "Trellis", "Bloomberg Green", "CTVC"]
    impact_branding = {
        "임팩트온": ("impacton", "https://cdn.impacton.net/image/logo/toplogo_20230907040739.png"),
        "소셜임팩트뉴스": ("social", "https://cdn.socialimpactnews.net/image/logo/toplogo_20240401103136.png"),
        "이로운넷": ("eroun", "https://cdn.eroun.net/image/logo/toplogo_20250902112732.png"),
        "Trellis": ("trellis", "https://trellis.net/wp-content/themes/greenbiz/static/logo-trellis.svg"),
        "Bloomberg Green": ("bloomberg", "https://www.bloomberg.com/favicon.ico"),
        "CTVC": ("ctvc", "https://www.ctvc.co/assets/img/logo.svg?v=af7ed10043"),
        "ImpactAlpha": ("impactalpha", "https://impactalpha.com/wp-content/themes/impactalpha/assets/images/ia-subtitle-logo-color.svg"),
        "Causeartist": ("causeartist", "https://causeartist.com/favicon.png"),
    }

    def render_source_logo(source_name, branding, panel=False):
        brand_class, logo_url = branding.get(source_name, ("generic", ""))
        if not logo_url:
            return f'<span class="impact-source-logo fallback-logo">{esc(source_name)}</span>'
        panel_class = " panel-logo" if panel else ""
        return (
            f'<span class="impact-source-logo{panel_class}">'
            f'<img src="{esc(logo_url)}" alt="{esc(source_name)} 로고" loading="lazy">'
            f'</span>'
        )

    def render_source_tab_section(section_id, label, kicker, heading, source_groups, source_priority, branding, empty_source_message, empty_news_message, show_empty_sources=False):
        ordered_sources = [source for source in source_priority if show_empty_sources or source in source_groups]
        ordered_sources.extend(sorted(source for source in source_groups if source not in ordered_sources))
        ordered_sources = [source for source in ordered_sources if show_empty_sources or source_groups.get(source)]
        default_source = ordered_sources[0] if ordered_sources else ""
        total_count = sum(len(source_groups.get(source, [])) for source in ordered_sources)

        source_cards = []
        source_panels = []
        for source_name in ordered_sources:
            brand_class, _logo_url = branding.get(source_name, ("generic", ""))
            key = f"{section_id}-{source_key(source_name)}"
            active_class = " active" if source_name == default_source else ""
            source_cards.append(
                f'<button class="impact-source-card impact-brand-{esc(brand_class)}{active_class}" data-source-target="{esc(key)}">'
                f'{render_source_logo(source_name, branding)}'
                f'<strong>{esc(source_name)}</strong>'
                f'<span class="impact-source-count">{len(source_groups.get(source_name, []))}건</span>'
                f'</button>'
            )
            source_panels.append(
                f'<div class="impact-news-panel{active_class}" data-source-panel="{esc(key)}">'
                f'<div class="impact-panel-head">{render_source_logo(source_name, branding, panel=True)}<h3>{esc(source_name)}</h3></div>'
                f'{render_news_list(source_groups.get(source_name, []), empty_news_message)}'
                f'</div>'
            )

        if not source_cards:
            source_cards.append(f'<div class="empty-state">{esc(empty_source_message)}</div>')
            source_panels.append(f'<div class="impact-news-panel active"><div class="empty-state">{esc(empty_news_message)}</div></div>')

        return f"""
        <section id="section-{esc(section_id)}" class="content-section source-tab-section">
            <div class="panel-shell">
                <div class="panel-header">
                    <div>
                        <div class="panel-kicker">{esc(kicker)}</div>
                        <h2>{esc(heading)}</h2>
                    </div>
                    <div class="panel-count">{total_count}건</div>
                </div>
                <div class="impact-source-strip">
                    {"".join(source_cards)}
                </div>
                <div class="impact-news-stage">
                    {"".join(source_panels)}
                </div>
            </div>
        </section>
        """

    indicator_section_html = f"""
        <section id="section-indicators" class="content-section active">
            <div class="panel-shell">
                <div class="panel-header">
                    <div>
                        <div class="panel-kicker">Market Snapshot</div>
                        <h2>주요 지표</h2>
                    </div>
                    <div class="panel-count">{updated_dot} 기준</div>
                </div>
                <div class="indicators-board">
                    {render_indicator_card("미국 10년물 국채 금리", dashboard.get("us_10y", "-"), "", "chart-us-10y", "tone-blue")}
                    {render_indicator_card("원/달러 환율", dashboard.get("fx_info", "-"), "", "chart-fx", "tone-green")}
                    {render_indicator_card("코스피 지수", dashboard.get("kospi_info", "-"), format_flow_html(dashboard.get("kospi_flow", "")), "chart-kospi", "tone-amber")}
                    {render_indicator_card("코스닥 지수", dashboard.get("kosdaq_info", "-"), format_flow_html(dashboard.get("kosdaq_flow", "")), "chart-kosdaq", "tone-slate")}
                </div>
            </div>
        </section>
    """

    vcac_groups = {}
    if "vcac" in section_map:
        for group in section_map["vcac"].get("groups", []):
            for category in group.get("categories", []):
                vcac_groups[category["name"]] = category.get("news", [])

    ai_groups = {}
    if "ai" in section_map:
        for group in section_map["ai"].get("groups", []):
            for category in group.get("categories", []):
                ai_groups[category["name"]] = category.get("news", [])

    impact_section_html = render_source_tab_section(
        "impact",
        "임팩트",
        "Impact Briefing",
        "임팩트",
        impact_groups,
        impact_priority,
        impact_branding,
        "수집된 임팩트 소스가 없습니다.",
        "수집된 임팩트 뉴스가 없습니다.",
    )

    vcac_section_html = render_source_tab_section(
        "vcac",
        "VC/AC/대체투자",
        "Startup & Capital",
        "VC/AC/대체투자",
        vcac_groups,
        VCAC_SOURCE_PRIORITY,
        VCAC_BRANDING,
        "수집된 VC/AC/대체투자 소스가 없습니다.",
        "수집된 VC/AC/대체투자 뉴스가 없습니다.",
        show_empty_sources=True,
    )

    ai_section_html = render_source_tab_section(
        "ai",
        "AI",
        "AI Briefing",
        "AI",
        ai_groups,
        AI_SOURCE_PRIORITY,
        AI_BRANDING,
        "수집된 AI 소스가 없습니다.",
        "수집된 AI 뉴스가 없습니다.",
        show_empty_sources=True,
    )

    industry_source_groups = {}
    for item in industry_source_items:
        industry_source_groups.setdefault(item.get("source", "기타"), []).append(item)

    industry_source_section_html = render_source_tab_section(
        "industrytrend",
        "산업트랜드",
        "Industry Trend",
        "산업트랜드",
        industry_source_groups,
        INDUSTRY_SOURCE_PRIORITY,
        INDUSTRY_SOURCE_BRANDING,
        "수집된 산업트랜드 소스가 없습니다.",
        "수집된 산업트랜드 기사가 없습니다.",
        show_empty_sources=True,
    )

    industry_section_html = render_industry_trend_section(industry_trend or [])

    theme_rate = strong_theme.get("rate", "-")
    theme_rate_class = "up" if "+" in theme_rate else "down" if "-" in theme_rate else "neutral"
    stock_rows = []
    for stock in strong_theme.get("stocks", []):
        rate_class = "up" if "+" in stock.get("rate", "") else "down" if "-" in stock.get("rate", "") else "neutral"
        stock_rows.append(
            f'<tr>'
            f'<td class="stock-name-cell"><a href="https://finance.naver.com/item/main.naver?code={esc(stock.get("code", ""))}" target="_blank" rel="noopener noreferrer">{esc(stock.get("name", ""))}</a></td>'
            f'<td class="stock-price-cell">{esc(stock.get("price", "-"))}원</td>'
            f'<td class="stock-rate-cell"><span class="{rate_class}">{esc(stock.get("rate", "-"))}</span></td>'
            f'<td class="stock-reason-cell">{esc(stock.get("reason", ""))}</td>'
            f'</tr>'
        )

    theme_section_html = f"""
        <section id="section-theme" class="content-section">
            <div class="panel-shell">
                <div class="panel-header">
                    <div>
                        <div class="panel-kicker">Hot Theme</div>
                        <h2>강세 테마</h2>
                    </div>
                    <div class="panel-count">{esc(theme_rate)}</div>
                </div>
                <div class="theme-hero">
                    <div class="theme-title-wrap">
                        <span class="theme-badge">HOT THEME</span>
                        <h3>{esc(strong_theme.get("name", "강세 테마"))}</h3>
                    </div>
                    <div class="theme-rate theme-rate-{theme_rate_class}">{esc(theme_rate)}</div>
                </div>
                <div class="theme-summary-box">{esc(strong_theme.get("desc", "테마 설명이 없습니다."))}</div>
                <div class="story-group">
                    <div class="story-group-title">주요 대장 종목</div>
                    <div class="stocks-table-wrapper">
                        <table class="stocks-table">
                            <thead>
                                <tr>
                                    <th style="width: 20%;">종목명</th>
                                    <th style="width: 16%;">현재가</th>
                                    <th style="width: 14%;">등락률</th>
                                    <th>기업 해설</th>
                                </tr>
                            </thead>
                            <tbody>
                                {"".join(stock_rows) if stock_rows else "<tr><td colspan='4' class='empty-table'>수집된 종목이 없습니다.</td></tr>"}
                            </tbody>
                        </table>
                    </div>
                </div>
                <div class="story-group theme-news-group">
                    <div class="story-group-title">테마 관련 최신 뉴스</div>
                    {render_news_list(strong_theme.get("news", []), "관련 뉴스를 찾을 수 없습니다.")}
                </div>
            </div>
        </section>
    """

    generic_sections_html = "".join(
        render_generic_section(section_map[sid])
        for sid in ("macro",)
        if sid in section_map
    )

    sidebar_html = "".join(
        f'<button class="sidebar-tab{" active" if sid == "indicators" else ""}" data-target="section-{esc(sid)}">{esc(label)}</button>'
        for sid, label in NAV_SECTIONS
    )

    calendar_sidebar_html = """
            <section class="sidebar-calendar">
                <div class="calendar-box">
                    <div class="calendar-top">
                        <div class="calendar-month-label" id="calendar-month-label"></div>
                        <div class="calendar-nav">
                            <button id="calendar-prev" type="button" aria-label="이전 달">&#9664;</button>
                            <button id="calendar-next" type="button" aria-label="다음 달">&#9654;</button>
                        </div>
                    </div>
                    <div class="calendar-weekdays">
                        <span>일</span><span>월</span><span>화</span><span>수</span><span>목</span><span>금</span><span>토</span>
                    </div>
                    <div class="calendar-grid" id="calendar-grid"></div>
                    <div class="calendar-caption">날짜를 누르면 해당 일자의 브리핑으로 이동합니다.</div>
                </div>
            </section>
    """

    html_template = """<!DOCTYPE html>
<html lang="ko">
<head>
    <meta charset="UTF-8">
    <meta name="viewport" content="width=device-width, initial-scale=1.0">
    <title>오늘의 마켓 & 뉴스 브리핑</title>
    <script src="archive_list.js"></script>
    <link rel="preconnect" href="https://fonts.googleapis.com">
    <link rel="preconnect" href="https://fonts.gstatic.com" crossorigin>
    <link href="https://fonts.googleapis.com/css2?family=Inter:wght@400;500;600;700;800&family=Outfit:wght@500;600;700;800&family=Noto+Sans+KR:wght@400;500;700;800&display=swap" rel="stylesheet">
    <script src="https://cdn.jsdelivr.net/npm/apexcharts"></script>
    <style>
        :root {
            --bg-main: #f4f0ea;
            --bg-panel: rgba(255, 255, 255, 0.88);
            --bg-panel-strong: #ffffff;
            --text-main: #13181f;
            --text-muted: #6f7683;
            --border-strong: rgba(116, 148, 173, 0.42);
            --border-soft: rgba(116, 148, 173, 0.22);
            --shadow-soft: 0 18px 40px rgba(68, 92, 112, 0.09);
            --shadow-card: 0 12px 26px rgba(68, 92, 112, 0.08);
            --accent-teal: #0ea5b7;
            --accent-blue: #2563eb;
            --accent-green: #15803d;
            --accent-amber: #b45309;
            --accent-rose: #be185d;
            --accent-slate: #334155;
        }

        * { box-sizing: border-box; margin: 0; padding: 0; }

        body {
            min-height: 100vh;
            font-family: 'Inter', 'Noto Sans KR', sans-serif;
            color: var(--text-main);
            background:
                radial-gradient(circle at top left, rgba(14, 165, 183, 0.08), transparent 28%),
                radial-gradient(circle at top right, rgba(37, 99, 235, 0.08), transparent 24%),
                linear-gradient(180deg, #f8f4ee 0%, var(--bg-main) 100%);
            padding: 28px;
        }

        button {
            font: inherit;
        }

        .page-shell {
            max-width: 1500px;
            margin: 0 auto;
        }

        .top-banner {
            display: grid;
            grid-template-columns: minmax(220px, 260px) minmax(0, 1fr);
            gap: 22px;
            align-items: center;
            margin-bottom: 10px;
        }

        .brand-box {
            display: flex;
            flex-direction: column;
            justify-content: center;
            gap: 0;
        }

        .brand-logo {
            display: inline-flex;
            flex-direction: column;
            gap: 0;
            font-family: 'Outfit', 'Noto Sans KR', sans-serif;
            font-size: clamp(1.9rem, 3vw, 2.45rem);
            font-weight: 800;
            line-height: 0.88;
            letter-spacing: -0.04em;
        }

        .brand-logo span:last-child {
            display: inline-flex;
            align-items: flex-end;
            gap: 8px;
        }

        .brand-dot {
            width: 14px;
            height: 14px;
            border-radius: 999px;
            background: var(--accent-teal);
            box-shadow: 0 0 0 4px rgba(14, 165, 183, 0.18);
        }

        .title-box {
            display: grid;
            grid-template-columns: minmax(0, 1fr) auto;
            align-items: end;
            gap: 18px;
        }

        .title-box h1 {
            font-family: 'Outfit', 'Noto Sans KR', sans-serif;
            font-size: clamp(1.95rem, 4vw, 3.05rem);
            line-height: 1.02;
            letter-spacing: -0.04em;
            font-weight: 700;
        }

        .title-meta {
            color: var(--text-muted);
            font-size: 0.88rem;
            line-height: 1.5;
            text-align: right;
            white-space: nowrap;
        }

        .calendar-box {
            padding: 18px 18px 16px;
            display: flex;
            flex-direction: column;
            gap: 14px;
        }

        .sidebar-calendar {
            background: var(--bg-panel);
            border: 1px solid var(--border-strong);
            border-radius: 24px;
            box-shadow: var(--shadow-soft);
            backdrop-filter: blur(14px);
        }

        .calendar-top {
            display: flex;
            align-items: center;
            justify-content: space-between;
            gap: 12px;
        }

        .calendar-month-label {
            font-family: 'Outfit', 'Noto Sans KR', sans-serif;
            font-size: 1.55rem;
            font-weight: 700;
            letter-spacing: -0.03em;
        }

        .calendar-nav {
            display: flex;
            gap: 8px;
        }

        .calendar-nav button {
            width: 36px;
            height: 36px;
            border-radius: 50%;
            border: 1px solid var(--border-soft);
            background: #ffffff;
            cursor: pointer;
            color: var(--text-main);
            transition: transform 0.2s ease, border-color 0.2s ease, background 0.2s ease;
        }

        .calendar-nav button:hover {
            transform: translateY(-1px);
            border-color: var(--border-strong);
            background: #f8fbfd;
        }

        .calendar-weekdays,
        .calendar-grid {
            display: grid;
            grid-template-columns: repeat(7, minmax(0, 1fr));
            gap: 8px;
        }

        .calendar-weekdays span {
            text-align: center;
            font-size: 0.83rem;
            font-weight: 700;
            color: var(--text-muted);
        }

        .calendar-day {
            height: 42px;
            border: 1px solid transparent;
            border-radius: 50%;
            background: transparent;
            color: var(--text-main);
            display: inline-flex;
            align-items: center;
            justify-content: center;
            cursor: pointer;
            transition: all 0.2s ease;
        }

        .calendar-day.is-muted {
            color: #b0b7c3;
        }

        .calendar-day.is-available:hover {
            border-color: rgba(14, 165, 183, 0.32);
            background: rgba(14, 165, 183, 0.08);
        }

        .calendar-day.is-selected {
            background: #0f6bd8;
            color: #ffffff;
            box-shadow: 0 12px 18px rgba(15, 107, 216, 0.22);
        }

        .calendar-day:disabled {
            cursor: default;
        }

        .calendar-caption {
            font-size: 0.82rem;
            color: var(--text-muted);
            line-height: 1.45;
        }

        .workspace {
            display: grid;
            grid-template-columns: minmax(220px, 300px) minmax(0, 1fr);
            gap: 22px;
            align-items: start;
        }

        .sidebar {
            position: sticky;
            top: 20px;
            display: flex;
            flex-direction: column;
            gap: 14px;
        }

        .sidebar-tab {
            border: 1px solid var(--border-strong);
            border-radius: 18px;
            background: rgba(255, 255, 255, 0.78);
            min-height: 64px;
            padding: 14px 20px;
            text-align: center;
            font-family: 'Outfit', 'Noto Sans KR', sans-serif;
            font-size: 1.25rem;
            font-weight: 700;
            letter-spacing: -0.03em;
            cursor: pointer;
            transition: transform 0.22s ease, background 0.22s ease, color 0.22s ease, box-shadow 0.22s ease;
        }

        .sidebar-tab:hover {
            transform: translateY(-2px);
            box-shadow: 0 10px 22px rgba(22, 30, 37, 0.08);
        }

        .sidebar-tab.active {
            background: linear-gradient(135deg, #2da9b8 0%, #6e8ef7 100%);
            color: #ffffff;
            border-color: transparent;
            box-shadow: 0 14px 26px rgba(75, 122, 161, 0.22);
        }

        .main-stage {
            min-width: 0;
        }

        .content-section {
            display: none;
            animation: fadeIn 0.35s ease;
        }

        .content-section.active {
            display: block;
        }

        @keyframes fadeIn {
            from {
                opacity: 0;
                transform: translateY(8px);
            }
            to {
                opacity: 1;
                transform: translateY(0);
            }
        }

        .panel-shell {
            background: var(--bg-panel);
            border: 1px solid var(--border-strong);
            border-radius: 30px;
            padding: 24px;
            box-shadow: var(--shadow-soft);
            backdrop-filter: blur(14px);
        }

        .panel-header {
            display: flex;
            align-items: flex-end;
            justify-content: space-between;
            gap: 16px;
            padding-bottom: 18px;
            margin-bottom: 18px;
            border-bottom: 1px solid var(--border-soft);
        }

        .panel-kicker {
            font-size: 0.78rem;
            color: var(--text-muted);
            text-transform: uppercase;
            letter-spacing: 0.16em;
            margin-bottom: 6px;
        }

        .panel-header h2 {
            font-family: 'Outfit', 'Noto Sans KR', sans-serif;
            font-size: clamp(1.8rem, 3vw, 2.55rem);
            letter-spacing: -0.04em;
            line-height: 1.04;
        }

        .panel-count {
            border: 1px solid var(--border-soft);
            background: rgba(255, 255, 255, 0.76);
            border-radius: 999px;
            padding: 8px 14px;
            font-size: 0.85rem;
            color: var(--text-muted);
            font-weight: 700;
            white-space: nowrap;
        }

        .indicators-board {
            display: grid;
            grid-template-columns: repeat(2, minmax(0, 1fr));
            gap: 18px;
        }

        .indicator-card {
            background: var(--bg-panel-strong);
            border: 1px solid var(--border-soft);
            border-radius: 24px;
            padding: 20px;
            min-height: 360px;
            display: flex;
            flex-direction: column;
            gap: 14px;
            box-shadow: var(--shadow-card);
        }

        .indicator-summary {
            display: flex;
            flex-direction: column;
            justify-content: flex-start;
            align-items: flex-start;
            gap: 6px;
        }

        .indicator-card.tone-blue {
            box-shadow: inset 4px 0 0 var(--accent-blue), var(--shadow-card);
        }

        .indicator-card.tone-green {
            box-shadow: inset 4px 0 0 var(--accent-green), var(--shadow-card);
        }

        .indicator-card.tone-amber {
            box-shadow: inset 4px 0 0 #d97706, var(--shadow-card);
        }

        .indicator-card.tone-slate {
            box-shadow: inset 4px 0 0 var(--accent-slate), var(--shadow-card);
        }

        .metric-label {
            font-size: 0.98rem;
            color: var(--text-muted);
            font-weight: 700;
        }

        .metric-value {
            font-family: 'Outfit', 'Noto Sans KR', sans-serif;
            font-size: clamp(1.1rem, 1.7vw, 1.45rem);
            font-weight: 700;
            letter-spacing: -0.04em;
        }

        .metric-detail {
            font-size: 0.82rem;
            color: #425264;
            line-height: 1.55;
        }

        .flow-value {
            font-weight: 800;
        }

        .flow-value.up {
            color: #d84c34;
        }

        .flow-value.down {
            color: #1763d6;
        }

        .flow-value.neutral {
            color: var(--text-main);
        }

        .chart-panel {
            min-height: 292px;
            background: transparent;
            border: none;
            border-radius: 0;
            padding: 0;
            flex: 1;
        }

        .chart-canvas {
            min-height: 292px;
            height: 100%;
        }

        .impact-source-strip {
            display: grid;
            grid-template-columns: repeat(auto-fit, minmax(140px, 1fr));
            gap: 14px;
            margin-bottom: 18px;
        }

        .impact-source-card {
            min-height: 128px;
            padding: 18px 16px;
            border-radius: 22px;
            border: 1px solid var(--border-soft);
            background: var(--bg-panel-strong);
            display: flex;
            flex-direction: column;
            justify-content: space-between;
            align-items: flex-start;
            cursor: pointer;
            text-align: left;
            transition: transform 0.22s ease, box-shadow 0.22s ease, border-color 0.22s ease;
        }

        .impact-source-card:hover {
            transform: translateY(-2px);
            box-shadow: 0 12px 20px rgba(68, 92, 112, 0.08);
            border-color: rgba(23, 48, 66, 0.24);
        }

        .impact-source-card.active {
            border-color: rgba(45, 169, 184, 0.35);
            box-shadow: 0 16px 24px rgba(68, 92, 112, 0.12);
        }

        .impact-source-logo {
            width: 100%;
            min-height: 34px;
            display: flex;
            align-items: center;
            margin-bottom: 12px;
        }

        .impact-source-logo img {
            max-width: 148px;
            max-height: 34px;
            object-fit: contain;
            object-position: left center;
        }

        .impact-source-logo.panel-logo {
            min-height: 30px;
            margin-bottom: 0;
            width: auto;
        }

        .impact-source-logo.panel-logo img {
            max-width: 170px;
            max-height: 34px;
        }

        .fallback-logo {
            font-size: 0.86rem;
            color: var(--text-muted);
            font-weight: 800;
        }

        .impact-source-card strong {
            font-family: 'Outfit', 'Noto Sans KR', sans-serif;
            font-size: 1.04rem;
            line-height: 1.2;
            letter-spacing: -0.03em;
        }

        .impact-source-count {
            font-size: 0.82rem;
            color: var(--text-muted);
            font-weight: 700;
        }

        .impact-brand-impacton { box-shadow: inset 0 4px 0 #111827; }
        .impact-brand-social { box-shadow: inset 0 4px 0 #2563eb; }
        .impact-brand-eroun { box-shadow: inset 0 4px 0 #0f766e; }
        .impact-brand-trellis { box-shadow: inset 0 4px 0 #15803d; }
        .impact-brand-bloomberg { box-shadow: inset 0 4px 0 #65a30d; }
        .impact-brand-ctvc { box-shadow: inset 0 4px 0 #7c3aed; }
        .impact-brand-impactalpha { box-shadow: inset 0 4px 0 #ec4899; }
        .impact-brand-causeartist { box-shadow: inset 0 4px 0 #0f766e; }
        .impact-brand-unicorn { box-shadow: inset 0 4px 0 #111827; }
        .impact-brand-dealsite { box-shadow: inset 0 4px 0 #0f172a; }
        .impact-brand-recipe { box-shadow: inset 0 4px 0 #f59e0b; }
        .impact-brand-platum { box-shadow: inset 0 4px 0 #2563eb; }
        .impact-brand-venturesquare { box-shadow: inset 0 4px 0 #10b981; }
        .impact-brand-ai-news { box-shadow: inset 0 4px 0 #2563eb; }
        .impact-brand-aitimes { box-shadow: inset 0 4px 0 #111827; }
        .impact-brand-marketingtech { box-shadow: inset 0 4px 0 #ec4899; }
        .impact-brand-batch { box-shadow: inset 0 4px 0 #0f766e; }
        .impact-brand-batch-weekly { box-shadow: inset 0 4px 0 #f59e0b; }
        .impact-brand-mckinsey { box-shadow: inset 0 4px 0 #003b5c; }
        .impact-brand-bain { box-shadow: inset 0 4px 0 #c41230; }
        .impact-brand-bcg { box-shadow: inset 0 4px 0 #177663; }

        .mbb-source-strip {
            grid-template-columns: repeat(3, minmax(0, 1fr));
        }

        .mbb-wordmark {
            min-height: 42px;
            font-family: 'Outfit', 'Noto Sans KR', sans-serif;
            font-size: 1.28rem;
            line-height: 1.05;
            letter-spacing: -0.045em;
            color: #173042;
        }

        .mbb-wordmark small {
            display: block;
            margin-left: 5px;
            font-size: 0.68rem;
            letter-spacing: -0.02em;
        }

        .mbb-wordmark-bcg {
            max-width: 185px;
            color: #177663;
            font-weight: 800;
        }

        .mbb-logo-image img {
            max-width: 190px;
            max-height: 42px;
        }

        #section-industry .impact-news-stage {
            min-height: 0;
        }

        .impact-news-stage {
            background: var(--bg-panel-strong);
            border: 1px solid var(--border-soft);
            border-radius: 26px;
            padding: 22px;
            min-height: 560px;
        }

        .impact-news-panel {
            display: none;
        }

        .impact-news-panel.active {
            display: block;
        }

        .impact-panel-head {
            margin-bottom: 14px;
            padding-bottom: 14px;
            border-bottom: 1px solid var(--border-soft);
            display: flex;
            flex-direction: column;
            gap: 8px;
        }

        .impact-panel-head h3 {
            font-family: 'Outfit', 'Noto Sans KR', sans-serif;
            font-size: 1.35rem;
            letter-spacing: -0.03em;
        }

        .story-board {
            display: grid;
            gap: 18px;
        }

        .story-group {
            display: grid;
            gap: 14px;
        }

        .theme-news-group {
            margin-top: 28px;
        }

        .story-group-title {
            font-family: 'Outfit', 'Noto Sans KR', sans-serif;
            font-size: 1.08rem;
            font-weight: 700;
            letter-spacing: -0.03em;
            color: var(--text-main);
        }

        .story-card {
            background: var(--bg-panel-strong);
            border: 1px solid var(--border-soft);
            border-radius: 22px;
            padding: 20px;
        }

        .story-label {
            display: inline-flex;
            align-items: center;
            gap: 8px;
            font-size: 0.8rem;
            font-weight: 800;
            text-transform: uppercase;
            letter-spacing: 0.12em;
            color: var(--text-muted);
            margin-bottom: 14px;
        }

        .news-card {
            background: #ffffff;
            border: 1px solid rgba(15, 23, 42, 0.08);
            border-radius: 18px;
            padding: 18px;
            box-shadow: 0 10px 16px rgba(68, 92, 112, 0.05);
        }

        .news-card + .news-card {
            margin-top: 12px;
        }

        .news-title {
            font-size: 1.02rem;
            line-height: 1.5;
            font-weight: 700;
            letter-spacing: -0.02em;
            margin-bottom: 6px;
        }

        .news-title a {
            color: var(--text-main);
            text-decoration: none;
        }

        .news-title a:hover {
            color: var(--accent-teal);
        }

        .news-date {
            font-size: 0.8rem;
            color: var(--text-muted);
            margin-bottom: 10px;
        }

        .news-summary {
            padding-left: 18px;
            font-size: 0.9rem;
            color: #3d4a5b;
            line-height: 1.68;
        }

        .news-summary li + li {
            margin-top: 4px;
        }

        .news-actions {
            margin-top: 14px;
        }

        .news-action-link {
            display: inline-flex;
            align-items: center;
            justify-content: center;
            border-radius: 999px;
            padding: 9px 14px;
            background: rgba(15, 107, 216, 0.09);
            color: #0f5fc2;
            font-size: 0.84rem;
            font-weight: 800;
            text-decoration: none;
            transition: transform 0.2s ease, background 0.2s ease;
        }

        .news-action-link:hover {
            transform: translateY(-1px);
            background: rgba(15, 107, 216, 0.16);
        }

        .industry-card {
            background: #ffffff;
            border: 1px solid var(--border-soft);
            border-radius: 26px;
            padding: 24px;
            box-shadow: var(--shadow-card);
            display: grid;
            gap: 18px;
        }

        .industry-list {
            display: block;
        }

        .industry-summary {
            margin: 0;
        }

        .mbb-news-card .industry-chart-image {
            margin-top: 16px;
        }

        .industry-meta {
            display: flex;
            align-items: center;
            justify-content: space-between;
            gap: 12px;
            color: var(--text-muted);
            font-size: 0.82rem;
            font-weight: 800;
            letter-spacing: 0.08em;
            text-transform: uppercase;
        }

        .industry-card h3 {
            font-family: 'Outfit', 'Noto Sans KR', sans-serif;
            font-size: clamp(1.55rem, 2.5vw, 2.25rem);
            line-height: 1.14;
            letter-spacing: -0.04em;
        }

        .industry-description {
            font-size: 0.98rem;
            line-height: 1.78;
            color: #334155;
            max-width: 980px;
        }

        .industry-chart-image {
            background: linear-gradient(135deg, #f8fbfd 0%, #eef7f8 100%);
            border: 1px solid rgba(15, 23, 42, 0.08);
            border-radius: 24px;
            padding: 18px;
            display: flex;
            justify-content: center;
            overflow: hidden;
        }

        .industry-chart-image img {
            width: min(100%, 980px);
            height: auto;
            display: block;
        }

        .industry-source-note {
            color: var(--text-muted);
            font-size: 0.82rem;
            font-weight: 700;
        }

        .industry-actions {
            display: flex;
            flex-wrap: wrap;
            gap: 10px;
        }

        .industry-report-link {
            display: inline-flex;
            align-items: center;
            justify-content: center;
            width: fit-content;
            border-radius: 999px;
            padding: 10px 14px;
            background: #0f6bd8;
            color: #ffffff;
            text-decoration: none;
            font-size: 0.86rem;
            font-weight: 800;
        }

        .industry-report-link.secondary {
            background: #e9f3f6;
            color: #0f5261;
        }

        .empty-state {
            border: 1px dashed rgba(23, 48, 66, 0.22);
            border-radius: 18px;
            padding: 18px;
            background: #f8fafc;
            color: var(--text-muted);
            font-size: 0.92rem;
        }

        .theme-hero {
            display: flex;
            align-items: center;
            justify-content: space-between;
            gap: 18px;
            background: linear-gradient(135deg, rgba(14, 165, 183, 0.08), rgba(37, 99, 235, 0.07));
            border: 1px solid rgba(14, 165, 183, 0.18);
            border-radius: 24px;
            padding: 22px;
            margin-bottom: 18px;
        }

        .theme-title-wrap {
            display: grid;
            gap: 10px;
        }

        .theme-badge {
            display: inline-flex;
            width: fit-content;
            font-size: 0.76rem;
            font-weight: 800;
            text-transform: uppercase;
            letter-spacing: 0.16em;
            color: #0f6b7a;
            background: rgba(255, 255, 255, 0.78);
            border: 1px solid rgba(14, 165, 183, 0.22);
            border-radius: 999px;
            padding: 6px 10px;
        }

        .theme-title-wrap h3 {
            font-family: 'Outfit', 'Noto Sans KR', sans-serif;
            font-size: clamp(1.45rem, 2.7vw, 2.2rem);
            letter-spacing: -0.04em;
            line-height: 1.12;
        }

        .theme-rate {
            padding: 10px 16px;
            border-radius: 999px;
            font-family: 'Outfit', 'Noto Sans KR', sans-serif;
            font-size: 1.15rem;
            font-weight: 700;
            white-space: nowrap;
        }

        .theme-rate-up {
            background: #fee2e2;
            color: #dc2626;
        }

        .theme-rate-down {
            background: #dbeafe;
            color: #2563eb;
        }

        .theme-rate-neutral {
            background: #e2e8f0;
            color: var(--text-main);
        }

        .theme-summary-box {
            background: #ffffff;
            border: 1px solid var(--border-soft);
            border-left: 4px solid var(--accent-teal);
            border-radius: 18px;
            padding: 18px 20px;
            margin-bottom: 18px;
            line-height: 1.72;
            color: #334155;
        }

        .stocks-table-wrapper {
            overflow-x: auto;
            border: 1px solid var(--border-soft);
            border-radius: 18px;
            background: #ffffff;
        }

        .stocks-table {
            width: 100%;
            border-collapse: collapse;
            min-width: 620px;
        }

        .stocks-table th,
        .stocks-table td {
            padding: 14px 16px;
            text-align: left;
            border-bottom: 1px solid rgba(15, 23, 42, 0.06);
        }

        .stocks-table th {
            background: #f8fafc;
            font-size: 0.78rem;
            text-transform: uppercase;
            letter-spacing: 0.12em;
            color: var(--text-muted);
        }

        .stocks-table tr:last-child td {
            border-bottom: none;
        }

        .stock-name-cell a {
            color: var(--text-main);
            text-decoration: none;
            font-weight: 700;
        }

        .stock-name-cell a:hover {
            color: var(--accent-teal);
        }

        .stock-price-cell {
            font-family: 'Outfit', 'Noto Sans KR', sans-serif;
            font-weight: 700;
        }

        .stock-rate-cell span {
            display: inline-flex;
            border-radius: 999px;
            padding: 5px 10px;
            font-size: 0.82rem;
            font-weight: 800;
        }

        .stock-rate-cell span.up {
            background: #fee2e2;
            color: #dc2626;
        }

        .stock-rate-cell span.down {
            background: #dbeafe;
            color: #2563eb;
        }

        .stock-rate-cell span.neutral {
            background: #e2e8f0;
            color: var(--text-main);
        }

        .stock-reason-cell {
            color: #475569;
            line-height: 1.65;
            font-size: 0.9rem;
        }

        .empty-table {
            text-align: center;
            color: var(--text-muted);
            padding: 22px;
        }

        @media (max-width: 1200px) {
            .workspace {
                grid-template-columns: 1fr;
            }

            .sidebar {
                position: static;
                display: grid;
                grid-template-columns: repeat(3, minmax(0, 1fr));
            }

            .sidebar-calendar {
                grid-column: 1 / -1;
            }

            .top-banner {
                grid-template-columns: 1fr;
                gap: 6px;
            }

            .title-box {
                grid-template-columns: 1fr;
                align-items: flex-start;
                gap: 6px;
            }

            .title-meta {
                text-align: left;
                white-space: normal;
            }

            .indicators-board {
                grid-template-columns: 1fr;
            }
        }

        @media (max-width: 780px) {
            body {
                padding: 18px;
            }

            .sidebar {
                grid-template-columns: repeat(2, minmax(0, 1fr));
            }

            .impact-source-strip {
                grid-template-columns: repeat(2, minmax(0, 1fr));
            }

            .panel-shell,
            .sidebar-calendar {
                border-radius: 24px;
            }

            .indicator-card {
                min-height: 320px;
            }
        }

        @media (max-width: 560px) {
            .sidebar {
                grid-template-columns: 1fr;
            }

            .impact-source-strip {
                grid-template-columns: 1fr;
            }

            .theme-hero,
            .panel-header {
                flex-direction: column;
                align-items: flex-start;
            }

            .calendar-day {
                height: 36px;
            }
        }
    </style>
</head>
<body>
    <div class="page-shell">
        <header class="top-banner">
            <section class="brand-box">
                <div class="brand-logo">
                    <span>IMPACT</span>
                    <span>SQUARE<div class="brand-dot"></div></span>
                </div>
            </section>
            <section class="title-box">
                <h1>오늘의 마켓 & 뉴스 브리핑</h1>
                <div class="title-meta">기사 기준일: {target_dot}<br>최종 갱신: {updated_at} KST</div>
            </section>
        </header>

        <div class="workspace">
            <aside class="sidebar">
                {calendar_sidebar_html}
                {sidebar_html}
            </aside>
            <main class="main-stage">
                {indicator_section_html}
                {impact_section_html}
                {vcac_section_html}
                {ai_section_html}
                {generic_sections_html}
                {industry_source_section_html}
                {industry_section_html}
                {theme_section_html}
            </main>
        </div>
    </div>

    <script>
        const chartData = {chart_json};
        const selectedDate = "{target_dash}";
        const hasArchiveManifest = typeof archiveDates !== "undefined" && Array.isArray(archiveDates) && archiveDates.length > 0;
        const availableDates = hasArchiveManifest
            ? Array.from(new Set([...archiveDates, selectedDate])).sort()
            : [selectedDate];

        const sidebarTabs = document.querySelectorAll(".sidebar-tab");
        const sections = document.querySelectorAll(".content-section");

        function activateSection(sectionId) {
            sidebarTabs.forEach((tab) => tab.classList.toggle("active", tab.dataset.target === sectionId));
            sections.forEach((section) => section.classList.toggle("active", section.id === sectionId));
        }

        sidebarTabs.forEach((tab) => {
            tab.addEventListener("click", () => activateSection(tab.dataset.target));
        });

        document.querySelectorAll(".source-tab-section").forEach((sourceSection) => {
            const sourceCards = sourceSection.querySelectorAll("[data-source-target]");
            const sourcePanels = sourceSection.querySelectorAll("[data-source-panel]");

            function activateSource(targetKey) {
                sourceCards.forEach((card) => card.classList.toggle("active", card.dataset.sourceTarget === targetKey));
                sourcePanels.forEach((panel) => panel.classList.toggle("active", panel.dataset.sourcePanel === targetKey));
            }

            sourceCards.forEach((card) => {
                card.addEventListener("click", () => activateSource(card.dataset.sourceTarget));
            });
        });

        function archiveHref(dateStr) {
            if (!hasArchiveManifest) {
                return "#";
            }
            return dateStr === archiveDates[0] ? "index.html" : `archive_${dateStr}.html`;
        }

        const calendarGrid = document.getElementById("calendar-grid");
        const calendarMonthLabel = document.getElementById("calendar-month-label");
        const calendarPrev = document.getElementById("calendar-prev");
        const calendarNext = document.getElementById("calendar-next");
        let calendarView = new Date(`${selectedDate}T00:00:00`);

        function renderCalendar() {
            const year = calendarView.getFullYear();
            const month = calendarView.getMonth();
            calendarMonthLabel.textContent = `${year}년 ${month + 1}월`;
            calendarGrid.innerHTML = "";

            const firstDay = new Date(year, month, 1);
            const firstDate = new Date(firstDay);
            firstDate.setDate(firstDate.getDate() - firstDay.getDay());

            for (let i = 0; i < 42; i += 1) {
                const current = new Date(firstDate);
                current.setDate(firstDate.getDate() + i);
                const dateStr = `${current.getFullYear()}-${String(current.getMonth() + 1).padStart(2, "0")}-${String(current.getDate()).padStart(2, "0")}`;
                const isCurrentMonth = current.getMonth() === month;
                const isSelected = dateStr === selectedDate;
                const isAvailable = availableDates.includes(dateStr);
                const button = document.createElement("button");
                button.type = "button";
                button.className = "calendar-day";
                if (!isCurrentMonth) button.classList.add("is-muted");
                if (isAvailable) button.classList.add("is-available");
                if (isSelected) button.classList.add("is-selected");
                button.textContent = String(current.getDate());

                if (!isAvailable) {
                    button.disabled = true;
                } else {
                    button.addEventListener("click", () => {
                        const href = archiveHref(dateStr);
                        if (href && href !== "#") {
                            window.location.href = href;
                        }
                    });
                }
                calendarGrid.appendChild(button);
            }
        }

        calendarPrev.addEventListener("click", () => {
            calendarView = new Date(calendarView.getFullYear(), calendarView.getMonth() - 1, 1);
            renderCalendar();
        });

        calendarNext.addEventListener("click", () => {
            calendarView = new Date(calendarView.getFullYear(), calendarView.getMonth() + 1, 1);
            renderCalendar();
        });

        renderCalendar();

        document.addEventListener("DOMContentLoaded", () => {
            if (!window.ApexCharts) {
                return;
            }

            const colors = {
                us10y: "#0f6bd8",
                fx: "#14875e",
                kospi: "#d97706",
                kosdaq: "#334155",
            };

            const formatValue = (value, suffix = "") => {
                const numericValue = Number(value);
                if (!Number.isFinite(numericValue)) {
                    return value;
                }
                const maximumFractionDigits = Math.abs(numericValue) >= 100 ? 1 : 3;
                return numericValue.toLocaleString("ko-KR", { maximumFractionDigits }) + suffix;
            };

            const chartOptions = (title, dates, values, color, suffix = "") => ({
                series: [{ name: title, data: values }],
                chart: {
                    type: "area",
                    height: 292,
                    toolbar: { show: false },
                    zoom: { enabled: false },
                    fontFamily: "Inter, Noto Sans KR, sans-serif",
                },
                colors: [color],
                dataLabels: { enabled: false },
                stroke: { curve: "smooth", width: 3 },
                fill: {
                    type: "gradient",
                    gradient: {
                        shadeIntensity: 1,
                        opacityFrom: 0.34,
                        opacityTo: 0.04,
                        stops: [0, 95, 100],
                    },
                },
                grid: {
                    borderColor: "rgba(148, 163, 184, 0.22)",
                    strokeDashArray: 4,
                },
                xaxis: {
                    categories: dates,
                    labels: {
                        style: {
                            colors: "#94a3b8",
                            fontSize: "10px",
                        },
                    },
                    axisBorder: { show: false },
                    axisTicks: { show: false },
                },
                yaxis: {
                    labels: {
                        style: {
                            colors: "#94a3b8",
                            fontSize: "10px",
                        },
                        formatter: (value) => formatValue(value, suffix),
                    },
                },
                tooltip: {
                    x: { show: true },
                    y: {
                        formatter: (value) => formatValue(value, suffix),
                    },
                },
            });

            if (chartData.us_10y && chartData.us_10y.values.length) {
                new ApexCharts(
                    document.querySelector("#chart-us-10y"),
                    chartOptions("금리", chartData.us_10y.dates, chartData.us_10y.values, colors.us10y, "%")
                ).render();
            }

            if (chartData.fx && chartData.fx.values.length) {
                new ApexCharts(
                    document.querySelector("#chart-fx"),
                    chartOptions("환율", chartData.fx.dates, chartData.fx.values, colors.fx, "원")
                ).render();
            }

            if (chartData.kospi && chartData.kospi.values.length) {
                new ApexCharts(
                    document.querySelector("#chart-kospi"),
                    chartOptions("코스피", chartData.kospi.dates, chartData.kospi.values, colors.kospi)
                ).render();
            }

            if (chartData.kosdaq && chartData.kosdaq.values.length) {
                new ApexCharts(
                    document.querySelector("#chart-kosdaq"),
                    chartOptions("코스닥", chartData.kosdaq.dates, chartData.kosdaq.values, colors.kosdaq)
                ).render();
            }
        });
    </script>
</body>
</html>
"""
    html_content = (
        html_template
        .replace("{target_dot}", target_dot)
        .replace("{updated_at}", updated_at)
        .replace("{calendar_sidebar_html}", calendar_sidebar_html)
        .replace("{sidebar_html}", sidebar_html)
        .replace("{indicator_section_html}", indicator_section_html)
        .replace("{impact_section_html}", impact_section_html)
        .replace("{vcac_section_html}", vcac_section_html)
        .replace("{industry_section_html}", industry_section_html)
        .replace("{industry_source_section_html}", industry_source_section_html)
        .replace("{ai_section_html}", ai_section_html)
        .replace("{generic_sections_html}", generic_sections_html)
        .replace("{theme_section_html}", theme_section_html)
        .replace("{chart_json}", chart_json)
        .replace("{target_dash}", target_dash)
    )
    return html_content

def main():
    args = parse_args()
    target_date = get_news_date(args)
    target_dot = target_date.strftime("%Y.%m.%d")
    target_dash = target_date.strftime("%Y-%m-%d")
    seen_links, seen_titles = set(), [] 
    env = load_env()
    configure_summary_generator(env)
    
    # 1. 대시보드 및 강세테마 데이터 수집
    dashboard_data = fetch_dashboard_data()
    strong_theme = fetch_strong_theme()
    industry_source_trend = fetch_industry_source_trend()
    industry_trend = fetch_industry_trend(target_date)
    dashboard_data["theme_name"] = strong_theme["name"]

    # 2. 30일 시계열 차트 데이터 수집
    chart_data = {
        "us_10y": fetch_historical_chart_data("^TNX"),
        "fx": fetch_historical_chart_data("KRW=X"),
        "kospi": fetch_historical_chart_data("^KS11"),
        "kosdaq": fetch_historical_chart_data("^KQ11")
    }

    # 3. 뉴스 수집
    global_impact = fetch_global_impact(target_date, seen_links, seen_titles)
    trellis_news = fetch_trellis_news(target_date, seen_links, seen_titles)
    causeartist_news = fetch_causeartist_news(target_date, seen_links, seen_titles)
    socialimpact_news = fetch_sitemap_news_source(
        "소셜임팩트뉴스",
        "https://www.socialimpactnews.net/sitemap.xml",
        target_date,
        seen_links,
        seen_titles,
        "Korean social impact and mission-driven business news.",
        limit=10,
        delay_seconds=2.0,
    )
    eroun_news = fetch_sitemap_news_source(
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
    gmail_user = env.get("GMAIL_USER", "")
    gmail_password = env.get("GMAIL_APP_PASSWORD", "")
    if gmail_user and gmail_password:
        print("\n[Newsletter] 메일 뉴스레터 수집 중...")
        newsletter_news = fetch_newsletter_emails(gmail_user, gmail_password, target_date, seen_links, seen_titles)
    
    impact_news = []
    try:
        parser = ArticleLinkParser()
        parser.feed(fetch_text("https://www.impacton.net/news/articleList.html"))
        for title, href in parser.links:
            if len(impact_news) >= MAX_IMPACT_NEWS: break
            if not title or len(title) < 4: continue
            link = href if href.startswith("http") else f"https://www.impacton.net{href}"
            if link in seen_links or "pro" in title.lower() or "유료" in title: continue
            if any(is_similar_title(title, st) for st in seen_titles): continue
            
            try:
                article_html = fetch_text(link)
                if extract_impact_date(article_html) == target_dot:
                    soup = BeautifulSoup(article_html, "html.parser")
                    if not is_allowed_impacton_section(soup):
                        continue
                    body = extract_best_article_text(soup)
                    summary = make_three_line_summary(title, body, "임팩트온", "국내 ESG 및 임팩트 비즈니스 이슈입니다.")
                    seen_links.add(link); seen_titles.append(title)
                    impact_news.append({
                        "title": title,
                        "link": link,
                        "date": target_dot,
                        "source": "임팩트온",
                        "summary": summary,
                        "_summary_source": body,
                        "_summary_context": "국내 ESG 및 임팩트 비즈니스 이슈입니다.",
                    })
            except: continue
    except: pass

    all_impact = impact_news + global_impact + trellis_news + causeartist_news + socialimpact_news + eroun_news + newsletter_news
    all_impact = rank_news_by_source(all_impact, load_recent_briefing_titles(target_date))
    search_sections = fetch_search_sections(target_date, seen_links, seen_titles)
    selected_count = count_selected_news(strong_theme, all_impact, search_sections)
    print(f"[Selection] 최종 기사 {selected_count}건")

    domestic_impact, global_impact = [], []
    for news in all_impact:
        if is_domestic_news(news["title"], news["summary"], news["source"]): domestic_impact.append(news)
        else: global_impact.append(news)
    agent_c_report = None
    if agent_c is not None:
        try:
            agent_c_report = agent_c.apply_agent_b_summaries(
                target_date,
                strong_theme,
                domestic_impact,
                global_impact,
                search_sections,
            )
            if agent_c_report.get("applied", 0):
                print(
                    f"[Agent C] Agent B 요약 적용: "
                    f"{agent_c_report.get('applied', 0)}/{agent_c_report.get('news_items', 0)}건 "
                    f"({agent_c_report.get('summary_path', '')})"
                )
                if agent_c_report.get("report_path"):
                    print(f"[Agent C] 리포트: {agent_c_report['report_path']}")
            else:
                print(f"[Agent C] 적용할 Agent B 요약이 없습니다: {agent_c_report.get('summary_path', '')}")
        except Exception as exc:
            print(f"[Agent C] Agent B 요약 적용 실패: {exc}")
            agent_c_report = None

    if not agent_c_report or not agent_c_report.get("applied", 0):
        apply_ai_summaries_to_news(strong_theme, domestic_impact, global_impact, search_sections, industry_trend, industry_source_trend)
    else:
        # Agent C는 일반 뉴스만 다루므로 새 MBB 기사 요약은 별도로 같은 AI 요약기에 전달한다.
        apply_ai_summaries_to_news({}, [], [], [], industry_trend, industry_source_trend)

    # 4. 아카이브 및 HTML 생성
    archive_files = list(BASE_DIR.glob("archive_*.html"))
    dates = [f.stem.replace("archive_", "") for f in archive_files]
    if target_dash not in dates: dates.append(target_dash)
    dates.sort(reverse=True)
    ARCHIVE_JS_FILE.write_text(f"const archiveDates = {json.dumps(dates)};", encoding="utf-8")

    html_content = render_html(
        target_date,
        domestic_impact,
        global_impact,
        search_sections,
        target_dash,
        dashboard_data,
        strong_theme,
        chart_data,
        industry_trend,
        industry_source_trend,
    )
    share_html_content = build_shareable_html(html_content)
    OUTPUT_FILE.write_text(html_content, encoding="utf-8")
    SHARE_OUTPUT_FILE.write_text(share_html_content, encoding="utf-8")
    (BASE_DIR / f"archive_{target_dash}.html").write_text(html_content, encoding="utf-8")
    save_summary_cache()
    print(f"\n[Success] 완료! 대시보드가 추가된 파일이 생성되었습니다: {OUTPUT_FILE}")

if __name__ == "__main__":
    main()


