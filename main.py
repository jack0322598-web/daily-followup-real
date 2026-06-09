import argparse
import html
import imaplib
import json
import re
import time
import urllib.parse
import urllib.request
from collections import Counter
from datetime import datetime, timedelta, timezone
from email import message_from_bytes
from email.header import decode_header
from email.utils import parsedate_to_datetime
from html.parser import HTMLParser
from pathlib import Path
from urllib.parse import urlparse
from xml.etree import ElementTree
from bs4 import BeautifulSoup

try:
    import requests
except ImportError:  # pragma: no cover - fallback for bundled runtime
    requests = None

try:
    from googlenewsdecoder import gnewsdecoder
except ImportError:  # pragma: no cover - optional improvement for Google News RSS links
    gnewsdecoder = None

KST = timezone(timedelta(hours=9))
BASE_DIR = Path(__file__).resolve().parent
OUTPUT_FILE = BASE_DIR / "index.html"
SHARE_OUTPUT_FILE = BASE_DIR / "share_index.html"
ARCHIVE_JS_FILE = BASE_DIR / "archive_list.js"
TREND_KEYWORDS_FILE = BASE_DIR / "weekly_keywords.json"

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

MAX_IMPACT_NEWS = 5
MAX_GLOBAL_IMPACT_NEWS_PER_SOURCE = 2
MAX_NEWS_PER_CATEGORY = 3
MAX_VCAC_NEWS_PER_SOURCE = 8
SUMMARY_LINE_COUNT = 3
SUMMARY_MAX_CHARS = 145
TREND_LOOKBACK_DAYS = 7
TREND_TITLES_PER_CATEGORY = 12
TREND_KEYWORDS_PER_CATEGORY = 7
TREND_REFRESH_WEEKDAY = 1  # Tuesday, because Monday's news starts the weekly cycle.
GEMINI_KEYWORD_MODEL = "gemini-2.5-flash"

GLOBAL_IMPACT_FEEDS = [
    ("Powerstack", "https://powerstack.sightlineclimate.com/feed/"),
    ("ImpactAlpha", "https://impactalpha.com/feed/")
]

IMPACTON_ALLOWED_SECTIONS = {"산업", "정책", "투자·평가", "투자.평가"}

VCAC_SOURCE_PRIORITY = ("유니콘팩토리", "스타트업레시피", "플래텀", "벤처스퀘어")

VCAC_BRANDING = {
    "유니콘팩토리": ("unicorn", "https://menu.mt.co.kr/ucfactory/images/meta_unicornfactory.png"),
    "스타트업레시피": ("recipe", "https://startuprecipe.co.kr/wp-content/uploads/2025/05/StartupRecipe_logo-removebg-preview.png"),
    "플래텀": ("platum", "https://cdn.platum.kr/wp-content/uploads/2024/11/Platum-logo.svg"),
    "벤처스퀘어": ("venturesquare", "https://www.venturesquare.net/wp-content/uploads/2026/04/cropped-vs-symbol-color-192x192.png"),
}

VCAC_RSS_SOURCE_CONFIGS = [
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
        "context": "유니콘팩토리 투자·회수 섹션의 스타트업 투자 및 회수 소식입니다.",
    },
    {
        "source": "플래텀",
        "pages": ["https://platum.kr/news"],
        "link_pattern": r"/archives/\d+",
        "context": "플래텀 뉴스 섹션의 스타트업 생태계 소식입니다.",
    },
]

VCAC_TREND_CATEGORIES = [
    {
        "name": "스타트업 투자/VC/AC",
        "query": "(스타트업 투자유치 OR 벤처캐피탈 OR 액셀러레이터 OR 모태펀드 OR 시리즈A OR 시리즈B OR 프리IPO OR 스타트업 IPO OR 스타트업 M&A)",
        "trend_anchor": "(스타트업 투자유치 OR 벤처캐피탈 OR 액셀러레이터 OR 모태펀드 OR 스타트업 IPO OR 스타트업 M&A)",
        "context": "스타트업 투자, 회수, VC/AC 생태계 흐름입니다.",
    }
]

IMPACT_TREND_CATEGORIES = [
    {
        "name": "ESG/지속가능경영",
        "query": "(ESG OR 지속가능경영 OR 기후공시 OR 공급망 실사 OR RE100 OR 탄소중립)",
        "context": "ESG와 지속가능경영 흐름입니다.",
        "trend_anchor": "(ESG OR 지속가능경영 OR 기후공시 OR 공급망 실사)",
    },
    {
        "name": "임팩트투자/소셜벤처",
        "query": "(임팩트투자 OR 소셜벤처 OR 사회적기업 OR 사회혁신 OR 로컬임팩트)",
        "context": "임팩트투자와 소셜벤처 생태계 흐름입니다.",
        "trend_anchor": "(임팩트투자 OR 소셜벤처 OR 사회적기업)",
    },
    {
        "name": "기후/에너지 전환",
        "query": "(기후테크 OR 에너지 전환 OR 재생에너지 OR 배터리 재활용 OR 탄소감축)",
        "context": "기후와 에너지 전환 관련 흐름입니다.",
        "trend_anchor": "(기후테크 OR 에너지 전환 OR 탄소감축)",
    },
    {
        "name": "글로벌 임팩트",
        "query": "(impact investing OR climate tech OR sustainability disclosure OR blended finance OR social impact)",
        "context": "글로벌 임팩트와 지속가능성 흐름입니다.",
        "trend_anchor": "(impact investing OR climate tech OR sustainability)",
    },
]

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
BLOCKED_SOURCE_DOMAINS = ("blog.naver.com", "tistory.com", "youtube.com")
GOOGLE_NEWS_DECODE_CACHE = {}
ARTICLE_BODY_CACHE = {}
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
    {
        "id": "ai", "label": "AI",
        "groups": [
            {
                "title": "AI",
                "categories": [
                    {"name": "글로벌/빅테크", "query": "(오픈AI OR OpenAI OR 구글 Gemini OR 메타 Llama OR MS 코파일럿 OR 빅테크 AI)", "context": "글로벌 빅테크 AI 동향입니다."},
                    {"name": "AI 인프라/비용", "query": "(AI 데이터센터 OR GPU OR 엔비디아 OR HBM OR AI 반도체 OR 전력 인프라)", "context": "AI 하드웨어 동향입니다."},
                    {"name": "AI 융합 산업", "query": "(AI 헬스케어 OR AI 의료 OR 자율주행 AI OR AI 로봇 OR 온디바이스 AI OR AI 핀테크)", "context": "AI 산업 결합 기사입니다."},
                    {"name": "규제 이슈", "query": "(AI 규제 OR AI 가이드라인 OR AI 저작권 OR AI 윤리 OR EU AI법)", "context": "AI 규제 동향입니다."},
                ],
            },
        ],
    },
]

NAV_SECTIONS = (
    ("indicators", "주요 지표"),
    ("impact", "임팩트"),
    ("vcac", "VC/AC"),
    ("ai", "AI"),
    ("macro", "거시경제"),
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
                            "summary": make_three_line_summary(title, summary_source, source_name, f"{theme_name} 관련 강세 테마 뉴스입니다.")
                        })
                except Exception as ne:
                    print(f"Theme News Error for {theme_name}: {ne}")
                
                theme["news"] = news_list
                break  # Only Top 1 theme!
    except Exception as e:
        print("Theme Crawl Error:", e)
        
    return theme

# ==========================================
# 기본 함수들 (필터링 및 텍스트 정리)
# ==========================================
def parse_args():
    parser = argparse.ArgumentParser()
    parser.add_argument("--date", help="뉴스 수집 및 달력 표시 날짜 (YYYY-MM-DD). 기본값은 KST 기준 어제입니다.")
    parser.add_argument("--news-date", help="--date와 동일한 별칭입니다. 둘 다 있으면 --news-date가 우선합니다.")
    parser.add_argument("--refresh-keywords", action="store_true", help="주간 트렌드 키워드를 강제로 다시 생성합니다.")
    parser.add_argument("--skip-keyword-refresh", action="store_true", help="화요일이어도 주간 트렌드 키워드 갱신을 건너뜁니다.")
    parser.add_argument("--refresh-keywords-only", action="store_true", help="주간 트렌드 키워드만 생성하고 브리핑 HTML은 만들지 않습니다.")
    return parser.parse_args()

def parse_date_arg(date_arg):
    return datetime.strptime(date_arg.strip().replace(".", "-"), "%Y-%m-%d").date()

def get_target_date(date_arg=None):
    if not date_arg: return (datetime.now(KST) - timedelta(days=1)).date()
    return parse_date_arg(date_arg)

def get_news_date(args):
    return get_target_date(args.news_date or args.date)

def load_env():
    env = {}
    env_path = BASE_DIR / ".env"
    if not env_path.exists():
        return env
    for raw_line in env_path.read_text(encoding="utf-8", errors="ignore").splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        env[key.strip()] = value.strip().strip('"').strip("'")
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

def should_skip_search_item(section_id, category_name, source_name):
    normalized = normalize_source_name(source_name)
    if section_id == "macro" and category_name == "외교" and normalized == "브런치":
        return True
    return False

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

def week_start_for(date_obj):
    return date_obj - timedelta(days=date_obj.weekday())

def trend_category_key(section_id, group_title, category_name):
    return f"{section_id}::{group_title or section_id}::{category_name}"

def iter_trend_categories():
    for category in IMPACT_TREND_CATEGORIES:
        yield {
            "key": trend_category_key("impact", "임팩트", category["name"]),
            "section_id": "impact",
            "section_label": "임팩트",
            "group": "임팩트",
            "category": category["name"],
            "query": category.get("trend_query", category["query"]),
            "trend_anchor": category.get("trend_anchor", category["query"]),
            "context": category["context"],
        }
    for category in VCAC_TREND_CATEGORIES:
        yield {
            "key": trend_category_key("vcac", "VC/AC", category["name"]),
            "section_id": "vcac",
            "section_label": "VC/AC",
            "group": "VC/AC",
            "category": category["name"],
            "query": category.get("trend_query", category["query"]),
            "trend_anchor": category.get("trend_anchor", category["query"]),
            "context": category["context"],
        }
    for section in SEARCH_SECTIONS:
        for group in section["groups"]:
            for category in group["categories"]:
                yield {
                    "key": trend_category_key(section["id"], group["title"], category["name"]),
                    "section_id": section["id"],
                    "section_label": section["label"],
                    "group": group["title"],
                    "category": category["name"],
                    "query": category.get("trend_query", category["query"]),
                    "trend_anchor": category.get("trend_anchor", f'{section["label"]} {group["title"]} {category["name"]}'),
                    "context": category["context"],
                }

def load_trend_keywords():
    if not TREND_KEYWORDS_FILE.exists():
        return {}
    try:
        return json.loads(TREND_KEYWORDS_FILE.read_text(encoding="utf-8"))
    except Exception:
        return {}

def save_trend_keywords(payload):
    TREND_KEYWORDS_FILE.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")

def normalize_keyword_list(keywords):
    normalized = []
    seen = set()
    for keyword in keywords or []:
        keyword = normalize_space(str(keyword)).strip(" ,.;:|/\\\"'")
        if not keyword or len(keyword) < 2 or len(keyword) > 32:
            continue
        lowered = keyword.casefold()
        if lowered in seen:
            continue
        seen.add(lowered)
        normalized.append(keyword)
        if len(normalized) >= TREND_KEYWORDS_PER_CATEGORY:
            break
    return normalized

def format_google_query_term(keyword):
    keyword = normalize_space(keyword).replace('"', "")
    if not keyword:
        return ""
    return f'"{keyword}"' if " " in keyword else keyword

def keyword_clause(keywords):
    terms = [format_google_query_term(keyword) for keyword in keywords]
    terms = [term for term in terms if term]
    return " OR ".join(terms)

def get_trend_entry(trend_keywords, section_id, group_title, category_name):
    key = trend_category_key(section_id, group_title, category_name)
    return (trend_keywords or {}).get("categories", {}).get(key, {})

def get_trend_keywords_for_category(trend_keywords, section_id, group_title, category_name):
    entry = get_trend_entry(trend_keywords, section_id, group_title, category_name)
    return normalize_keyword_list(entry.get("keywords", []))

def enhance_query_with_trends(base_query, trend_anchor, keywords):
    keywords = normalize_keyword_list(keywords)
    if not keywords:
        return base_query
    clause = keyword_clause(keywords)
    if not clause:
        return base_query
    return f"({base_query}) OR ({trend_anchor} ({clause}))"

def fetch_trend_titles_for_query(query, reference_date):
    start_date = (reference_date - timedelta(days=TREND_LOOKBACK_DAYS)).strftime("%Y-%m-%d")
    end_date = (reference_date + timedelta(days=1)).strftime("%Y-%m-%d")
    encoded_query = urllib.parse.quote(f"({query}) after:{start_date} before:{end_date} -블로그 -카페 -blog -cafe")
    titles = []
    try:
        rss_text = fetch_text(f"https://news.google.com/rss/search?q={encoded_query}&hl=ko&gl=KR", timeout=12)
        for item in ElementTree.fromstring(rss_text).findall(".//item"):
            title, source_name = parse_google_news_item(item)
            title = normalize_space(title)
            source_name = normalize_source_name(source_name)
            if title and not any(is_similar_title(title, existing, threshold=0.28) for existing in titles):
                titles.append(f"{title} ({source_name})")
            if len(titles) >= TREND_TITLES_PER_CATEGORY:
                break
    except Exception as e:
        print(f"  - trend title fetch failed: {e}")
    return titles

def collect_trend_signals(reference_date):
    signals = []
    for category in iter_trend_categories():
        titles = fetch_trend_titles_for_query(category["query"], reference_date)
        signals.append({**category, "recent_titles": titles})
    return signals

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

def generate_trend_keywords_with_gemini(env, signals, reference_date):
    api_key = env.get("GEMINI_API_KEY", "")
    if not api_key:
        return None
    primary_model = env.get("GEMINI_MODEL", GEMINI_KEYWORD_MODEL)
    model_candidates = list(dict.fromkeys([primary_model, "gemini-2.5-flash-lite"]))
    merged_categories = {}

    def call_model(signal_chunk, model):
        prompt_payload = [
            {
                "key": signal["key"],
                "section": signal["section_label"],
                "group": signal["group"],
                "category": signal["category"],
                "base_query": signal["query"],
                "recent_titles": signal["recent_titles"],
            }
            for signal in signal_chunk
        ]
        prompt = (
            "You are improving a Korean morning market/news briefing search system. "
            "For each category, infer this week's concrete search keywords from the recent Google News titles. "
            "Return only valid JSON. For each category key, provide 4-7 keywords. "
            "Prefer specific entities, countries, companies, technologies, policies, tickers, conflicts, laws, and event names. "
            "Avoid generic category words such as 뉴스, 이슈, 전망, 시장, 투자, 경제, AI, ESG unless attached to a specific phrase. "
            "Include Korean keywords, and include English proper nouns when they are common search terms.\n\n"
            f"Reference news date: {reference_date.strftime('%Y-%m-%d')}\n"
            "JSON schema: {\"categories\":{\"<key>\":{\"keywords\":[\"...\"],\"rationale\":\"short Korean reason\"}}}\n"
            f"Categories and recent titles:\n{json.dumps(prompt_payload, ensure_ascii=False)}"
        )
        body = {
            "contents": [{"role": "user", "parts": [{"text": prompt}]}],
            "generationConfig": {"temperature": 0.2, "responseMimeType": "application/json"},
        }
        url = f"https://generativelanguage.googleapis.com/v1beta/models/{urllib.parse.quote(model)}:generateContent?key={urllib.parse.quote(api_key)}"
        request = urllib.request.Request(
            url,
            data=json.dumps(body, ensure_ascii=False).encode("utf-8"),
            headers={**HEADERS, "Content-Type": "application/json"},
            method="POST",
        )
        with urllib.request.urlopen(request, timeout=45) as response:
            data = json.loads(response.read().decode("utf-8", errors="replace"))
        text = " ".join(
            part.get("text", "")
            for candidate in data.get("candidates", [])
            for part in candidate.get("content", {}).get("parts", [])
        )
        parsed = extract_json_payload(text)
        return parsed.get("categories", {}) if isinstance(parsed, dict) else {}

    for i in range(0, len(signals), 5):
        chunk = signals[i:i + 5]
        chunk_categories = {}
        last_error = None
        for model in model_candidates:
            for attempt in range(3):
                try:
                    chunk_categories = call_model(chunk, model)
                    if chunk_categories:
                        break
                except Exception as e:
                    last_error = e
                    time.sleep(1.5 * (attempt + 1))
            if chunk_categories:
                break
        if chunk_categories:
            merged_categories.update(chunk_categories)
        else:
            print(f"  - Gemini keyword chunk failed: {last_error}")
    return {"categories": merged_categories} if merged_categories else None

TREND_TOKEN_STOPWORDS = STORY_TOKEN_STOPWORDS.union({
    "경제", "시장", "관련", "브리핑", "전망", "투자", "기업", "산업", "글로벌", "국내",
    "미국", "한국", "중국", "유럽", "주요", "이번주", "지난주", "상승", "하락",
    "머니투데이", "경향신문", "연합뉴스", "조선비즈", "한국경제", "매일경제", "파이낸셜뉴스",
    "news", "google", "ai", "esg", "vc", "ipo", "m&a", "or",
})

def fallback_keywords_from_texts(texts):
    counter = Counter()
    for title in texts:
        title = re.sub(r"\([^)]{1,40}\)\s*$", "", title)
        for token in extract_story_tokens(title):
            token = token.strip()
            if token.casefold() in TREND_TOKEN_STOPWORDS:
                continue
            if token.isdigit() or len(token) < 2:
                continue
            counter[token] += 1
    return [token for token, _ in counter.most_common(TREND_KEYWORDS_PER_CATEGORY)]

def fallback_keywords_from_titles(titles, base_query=""):
    keywords = fallback_keywords_from_texts(titles)
    if len(keywords) >= 4:
        return keywords
    query_terms = re.sub(r"\bOR\b|[()\"']", " ", base_query, flags=re.IGNORECASE)
    for token in fallback_keywords_from_texts([query_terms]):
        if token not in keywords:
            keywords.append(token)
        if len(keywords) >= TREND_KEYWORDS_PER_CATEGORY:
            break
    return keywords

def build_trend_keyword_payload(reference_date, signals, model_payload=None):
    model_categories = (model_payload or {}).get("categories", {}) if isinstance(model_payload, dict) else {}
    categories = {}
    for signal in signals:
        model_entry = model_categories.get(signal["key"], {}) if isinstance(model_categories, dict) else {}
        keywords = normalize_keyword_list(model_entry.get("keywords", []))
        source = "gemini" if keywords else "fallback"
        if not keywords:
            keywords = fallback_keywords_from_titles(signal["recent_titles"], signal["query"])
        categories[signal["key"]] = {
            "section": signal["section_label"],
            "group": signal["group"],
            "category": signal["category"],
            "query": signal["query"],
            "trend_anchor": signal["trend_anchor"],
            "keywords": normalize_keyword_list(keywords),
            "rationale": normalize_space(model_entry.get("rationale", ""))[:160],
            "source": source,
            "sample_titles": signal["recent_titles"][:5],
        }
    now = datetime.now(KST)
    return {
        "generated_at": now.strftime("%Y-%m-%dT%H:%M:%S%z"),
        "reference_date": reference_date.strftime("%Y-%m-%d"),
        "week_start": week_start_for(reference_date).strftime("%Y-%m-%d"),
        "lookback_days": TREND_LOOKBACK_DAYS,
        "model": "gemini" if model_payload else "fallback",
        "categories": categories,
    }

def should_refresh_trend_keywords(args, target_date, existing_payload):
    if args.skip_keyword_refresh:
        return False
    if args.refresh_keywords or args.refresh_keywords_only:
        return True
    if not existing_payload:
        return True
    expected_week = week_start_for(target_date).strftime("%Y-%m-%d")
    if existing_payload.get("week_start") != expected_week:
        return True
    now = datetime.now(KST)
    return now.weekday() == TREND_REFRESH_WEEKDAY and target_date == now.date() - timedelta(days=1) and existing_payload.get("reference_date") != target_date.strftime("%Y-%m-%d")

def get_or_refresh_trend_keywords(args, target_date, env):
    existing = load_trend_keywords()
    if not should_refresh_trend_keywords(args, target_date, existing):
        return existing
    print("\n[Trend] 주간 카테고리 키워드 갱신 중...")
    signals = collect_trend_signals(target_date)
    model_payload = generate_trend_keywords_with_gemini(env, signals, target_date)
    payload = build_trend_keyword_payload(target_date, signals, model_payload)
    save_trend_keywords(payload)
    keyword_count = sum(len(entry.get("keywords", [])) for entry in payload.get("categories", {}).values())
    print(f"[Trend] 완료: {len(payload.get('categories', {}))}개 카테고리, {keyword_count}개 키워드")
    return payload

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

def truncate_text(text, limit=90):
    text = normalize_space(text)
    if len(text) <= limit:
        return text
    return text[: max(0, limit - 1)].rstrip() + "…"

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

def make_three_line_summary(title, raw_text="", source="", context=""):
    title = normalize_space(title)
    lines, seen = [], set()
    text = clean_article_text(raw_text)
    text = re.sub(r"\bv\.daum\.net\b", "파이낸셜뉴스", text, flags=re.IGNORECASE)
    text = re.sub(r"\s*[|/]\s*", ". ", text)
    text = re.sub(r"…", ". ", text)
    text = re.sub(r"([.!?])\s+", r"\1|", text)
    text = re.sub(r"([.!?])(?=[가-힣\"'‘“])", r"\1|", text)
    title_key = compact_text(title)
    source_key = compact_text(source)
    for sentence in [normalize_space(p) for p in text.split("|") if normalize_space(p)]:
        sentence = re.sub(r"\bv\.daum\.net\b", "파이낸셜뉴스", sentence, flags=re.IGNORECASE)
        sentence = re.sub(r"^[가-힣]{2,5}\s기자\s+", "", sentence)
        sentence = re.sub(r"^\([^)]{2,30}\)\s*", "", sentence)
        sentence = re.sub(r"^\[[^\]]{2,40}\]\s*", "", sentence)
        sentence = re.sub(r"^.{0,90}?\d{4}[-./]\d{1,2}[-./]\d{1,2}\s+[가-힣]{2,5}\s+기자\s+", "", sentence)
        sentence = re.sub(r"^[가-힣A-Za-z·.\s]{2,20}\s기자\s*=\s*", "", sentence)
        sentence = re.sub(r"^[가-힣A-Za-z·.\s]{2,40}\s제공\s+", "", sentence)
        sentence = re.sub(r"^(송고|입력|수정)\s+\d{4}[-./년\s]\d{1,2}.*", "", sentence)
        if source:
            sentence = normalize_space(re.sub(rf"\s*[-|]?\s*{re.escape(source)}\s*$", "", sentence, flags=re.IGNORECASE))
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
        key = sentence.casefold()
        if key not in seen:
            lines.append(sentence[:SUMMARY_MAX_CHARS - 1] + "…" if len(sentence) > SUMMARY_MAX_CHARS else sentence)
            seen.add(key)
        if len(lines) >= SUMMARY_LINE_COUNT:
            break

    if lines:
        fallbacks = [
            truncate_text(context or "관련 흐름을 함께 볼 수 있는 기사입니다.", SUMMARY_MAX_CHARS),
            truncate_text(f"{source or '원문'} 보도를 바탕으로 정리했습니다.", SUMMARY_MAX_CHARS),
            truncate_text(title, SUMMARY_MAX_CHARS),
        ]
    else:
        fallbacks = [
            truncate_text(title, SUMMARY_MAX_CHARS),
            truncate_text(context or "관련 흐름을 함께 볼 수 있는 기사입니다.", SUMMARY_MAX_CHARS),
            truncate_text(f"{source or '원문'} 보도를 바탕으로 정리했습니다.", SUMMARY_MAX_CHARS),
        ]
    for fb in fallbacks:
        if len(lines) >= SUMMARY_LINE_COUNT:
            break
        if fb.casefold() not in seen:
            lines.append(fb)
            seen.add(fb.casefold())
    return lines[:SUMMARY_LINE_COUNT]

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
    for anchor in soup.find_all("a", href=True):
        link = anchor.get("href", "").strip()
        if not link.startswith("http"):
            continue
        low = link.lower()
        if any(token in low for token in blocked):
            continue
        title = normalize_space(anchor.get_text(" ", strip=True))
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
    try:
        mail = imaplib.IMAP4_SSL("imap.gmail.com")
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
                    "summary": make_three_line_summary(subject_title, body_text, source_name, f"{source_name} newsletter lead story.")
                }
                if source_name == "Bloomberg Green":
                    items = [subject_item]
                else:
                    items = [subject_item] + [item for item in items if item["title"] != subject_title]
            for item in items:
                if item["link"] in seen_links or any(is_similar_title(item["title"], title) for title in seen_titles):
                    continue
                seen_links.add(item["link"])
                seen_titles.append(item["title"])
                collected.append(item)
        mail.logout()
    except Exception as e:
        print(f"  - Newsletter fetch failed: {e}")
    return collected

# --- News Fetching Logic ---
def fetch_global_impact(target_date, seen_links, seen_titles):
    target_dot = target_date.strftime("%Y.%m.%d")
    global_news = []
    for source_name, feed_url in GLOBAL_IMPACT_FEEDS:
        try:
            feed_text = fetch_text(feed_url)
            soup = BeautifulSoup(feed_text, 'html.parser')
            items = soup.find_all(["item", "entry"])
            count = 0
            for item in items:
                if count >= MAX_GLOBAL_IMPACT_NEWS_PER_SOURCE: break
                title = extract_feed_item_title(item)
                link = extract_feed_item_link(item)
                date_tag = extract_feed_item_date(item)
                
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
                
                desc_tag = item.find("description") or item.find("summary") or item.find("content")
                summary = make_three_line_summary(title, strip_tags(desc_tag.text if desc_tag else ""), source_name, "글로벌 기후/임팩트 최신 동향입니다.")
                
                seen_links.add(link); seen_titles.append(title)
                global_news.append({"title": title, "link": link, "date": target_dot, "source": source_name, "summary": summary})
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
        " - 뉴스레터로 만나는 스타트업 투자 리포트 ‘스타트업레시피’",
        " - 뉴스레터로 만나는 스타트업 투자 리포트 '스타트업레시피'",
    ]
    for suffix in suffixes:
        if title.endswith(suffix):
            title = title[: -len(suffix)]
    return normalize_space(title)

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

def collect_listing_article_links(page_url, link_pattern):
    items = []
    seen = set()
    try:
        page_html = fetch_text(page_url, timeout=20)
        soup = BeautifulSoup(page_html, "html.parser")
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
    except Exception as e:
        print(f"  - VC/AC listing failed ({page_url}): {e}")
    return items

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
            feed_text = fetch_text(feed_url, timeout=20)
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
        for item in collect_listing_article_links(page_url, config["link_pattern"]):
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
    return dedupe_news_items(news_items)

def fetch_vcac_sources(target_date, seen_links, seen_titles):
    source_news = {source_name: [] for source_name in VCAC_SOURCE_PRIORITY}
    for config in VCAC_LISTING_SOURCE_CONFIGS:
        source_news[config["source"]] = fetch_vcac_listing_source(config, target_date, seen_links, seen_titles)
    for config in VCAC_RSS_SOURCE_CONFIGS:
        source_news[config["source"]] = fetch_vcac_rss_source(config, target_date, seen_links, seen_titles)
    return {
        "id": "vcac",
        "label": "VC/AC",
        "groups": [
            {
                "title": "VC/AC",
                "categories": [
                    {"name": source_name, "news": source_news.get(source_name, [])}
                    for source_name in VCAC_SOURCE_PRIORITY
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
            })
            seen_links.add(link)
            seen_titles.append(title)
    except Exception as e:
        print(f"  - Trellis failed: {e}")
    return news_items

def fetch_google_news_for_category(target_date, section_id, group_title, category, trend_keywords, seen_links, seen_titles, limit=MAX_NEWS_PER_CATEGORY, forced_source=None):
    news_list = []
    category_story_cache = []
    target_dot = target_date.strftime("%Y.%m.%d")
    start_date = target_date.strftime("%Y-%m-%d")
    end_date = (target_date + timedelta(days=1)).strftime("%Y-%m-%d")
    dynamic_keywords = get_trend_keywords_for_category(trend_keywords, section_id, group_title, category["name"])
    trend_anchor = category.get("trend_anchor") or category.get("trend_query") or f"{group_title} {category['name']}"
    enhanced_query = enhance_query_with_trends(category["query"], trend_anchor, dynamic_keywords)
    try:
        query = urllib.parse.quote(
            f"({enhanced_query}) after:{start_date} before:{end_date} -블로그 -카페 -blog -cafe"
        )
        rss_text = fetch_text(f"https://news.google.com/rss/search?q={query}&hl=ko&gl=KR")
        for item in ElementTree.fromstring(rss_text).findall(".//item"):
            if len(news_list) >= limit:
                break
            title, source_name = parse_google_news_item(item)
            source_name = normalize_source_name(source_name)
            google_link = item.findtext("link", "")
            article_link = resolve_google_news_url(google_link)
            link = article_link or google_link
            
            if section_id == "vcac" and not is_valid_vcac_title(title):
                continue
            if should_skip_search_item(section_id, category["name"], source_name):
                continue
                
            try:
                if parsedate_to_datetime(item.findtext("pubDate", "")).astimezone(KST).strftime("%Y.%m.%d") != target_dot:
                    continue
            except:
                continue

            desc_text = strip_tags(item.findtext("description", ""))
            if link in seen_links or google_link in seen_links or any(is_similar_title(title, st) for st in seen_titles):
                continue
            article_body = fetch_article_body_text(article_link)
            summary_source = article_body if len(article_body) >= 180 else desc_text
            if any(
                is_duplicate_story(title, summary_source, cached["title"], cached["text"])
                for cached in category_story_cache
            ):
                continue

            seen_links.add(link)
            seen_links.add(google_link)
            seen_titles.append(title)
            category_story_cache.append({"title": title, "text": summary_source})
            news_list.append({
                "title": title,
                "link": link,
                "source": forced_source or source_name,
                "date": target_dot,
                "summary": make_three_line_summary(title, summary_source, source_name, category["context"])
            })
    except Exception as e:
        print("수집 오류:", e)
    return dedupe_news_items(news_list)

def fetch_search_sections(target_date, seen_links, seen_titles, trend_keywords=None):
    results = [fetch_vcac_sources(target_date, seen_links, seen_titles)]
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
                    trend_keywords or {},
                    seen_links,
                    seen_titles,
                )
                group_result["categories"].append({"name": category["name"], "news": news_list})
            section_result["groups"].append(group_result)
        results.append(section_result)
    return results

# ==========================================
# 🌟 HTML 렌더링
# ==========================================
def render_html(target_date, domestic_impact, global_impact, search_sections, target_dash, dashboard, strong_theme, chart_data):
    target_dot = target_date.strftime("%Y.%m.%d")
    current_kst = datetime.now(KST)
    updated_at = current_kst.strftime("%Y.%m.%d %H:%M")
    updated_dot = current_kst.strftime("%Y.%m.%d")
    section_map = {section["id"]: section for section in search_sections}

    counts = {s["id"]: sum(len(c["news"]) for g in s["groups"] for c in g["categories"]) for s in search_sections}
    counts["indicators"] = 4
    counts["impact"] = len(domestic_impact) + len(global_impact)
    counts["theme"] = 1 if strong_theme and strong_theme["name"] != "강세테마 대기중" else 0

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
        return (
            f'<article class="news-card">'
            f'<div class="news-title"><a href="{esc(news.get("link", ""))}" target="_blank" rel="noopener noreferrer">{esc(news.get("title", ""))}</a></div>'
            f'<div class="news-date">출처: {esc(news.get("source", ""))} | 발행일: {esc(news.get("date", ""))}</div>'
            f'<ul class="news-summary">{summary_html}</ul>'
            f'</article>'
        )

    def render_news_list(news_items, empty_message):
        if not news_items:
            return f'<div class="empty-state">{esc(empty_message)}</div>'
        return "".join(render_news_card(news) for news in news_items)

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
        "Powerstack": ("powerstack", "https://media.beehiiv.com/cdn-cgi/image/format=auto,onerror=redirect/uploads/asset/file/19ec0b41-333b-4831-92b4-af055d65d058/Full.png"),
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
        "VC/AC",
        "Startup & Capital",
        "VC/AC",
        vcac_groups,
        VCAC_SOURCE_PRIORITY,
        VCAC_BRANDING,
        "수집된 VC/AC 소스가 없습니다.",
        "수집된 VC/AC 뉴스가 없습니다.",
        show_empty_sources=True,
    )

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
        for sid in ("ai", "macro")
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
        .impact-brand-powerstack { box-shadow: inset 0 4px 0 #ea580c; }
        .impact-brand-causeartist { box-shadow: inset 0 4px 0 #0f766e; }
        .impact-brand-unicorn { box-shadow: inset 0 4px 0 #111827; }
        .impact-brand-recipe { box-shadow: inset 0 4px 0 #f59e0b; }
        .impact-brand-platum { box-shadow: inset 0 4px 0 #2563eb; }
        .impact-brand-venturesquare { box-shadow: inset 0 4px 0 #10b981; }

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
                {generic_sections_html}
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
    trend_keywords = get_or_refresh_trend_keywords(args, target_date, env)
    if args.refresh_keywords_only:
        print(f"[Trend] 키워드 파일만 갱신했습니다: {TREND_KEYWORDS_FILE}")
        return
    
    # 1. 대시보드 및 강세테마 데이터 수집
    dashboard_data = fetch_dashboard_data()
    strong_theme = fetch_strong_theme()
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
                    impact_news.append({"title": title, "link": link, "date": target_dot, "source": "임팩트온", "summary": summary})
            except: continue
    except: pass

    all_impact = impact_news + global_impact + trellis_news + causeartist_news + socialimpact_news + eroun_news + newsletter_news
    domestic_impact, global_impact = [], []
    for news in all_impact:
        if is_domestic_news(news["title"], news["summary"], news["source"]): domestic_impact.append(news)
        else: global_impact.append(news)

    search_sections = fetch_search_sections(target_date, seen_links, seen_titles, trend_keywords)

    # 4. 아카이브 및 HTML 생성
    archive_files = list(BASE_DIR.glob("archive_*.html"))
    dates = [f.stem.replace("archive_", "") for f in archive_files]
    if target_dash not in dates: dates.append(target_dash)
    dates.sort(reverse=True)
    ARCHIVE_JS_FILE.write_text(f"const archiveDates = {json.dumps(dates)};", encoding="utf-8")

    html_content = render_html(target_date, domestic_impact, global_impact, search_sections, target_dash, dashboard_data, strong_theme, chart_data)
    share_html_content = build_shareable_html(html_content)
    OUTPUT_FILE.write_text(html_content, encoding="utf-8")
    SHARE_OUTPUT_FILE.write_text(share_html_content, encoding="utf-8")
    (BASE_DIR / f"archive_{target_dash}.html").write_text(html_content, encoding="utf-8")
    print(f"\n[Success] 완료! 대시보드가 추가된 파일이 생성되었습니다: {OUTPUT_FILE}")

if __name__ == "__main__":
    main()


