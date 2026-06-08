import argparse
import html
import imaplib
import json
import re
import time
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
from bs4 import BeautifulSoup

try:
    import requests
except ImportError:  # pragma: no cover - fallback for bundled runtime
    requests = None

KST = timezone(timedelta(hours=9))
BASE_DIR = Path(__file__).resolve().parent
OUTPUT_FILE = BASE_DIR / "index.html"
SHARE_OUTPUT_FILE = BASE_DIR / "share_index.html"
ARCHIVE_JS_FILE = BASE_DIR / "archive_list.js"

HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,image/webp,*/*;q=0.8",
}

def http_get_text(url, timeout=10, encoding=None):
    if requests is not None:
        res = requests.get(url, headers=HEADERS, timeout=timeout)
        if encoding:
            res.encoding = encoding
            return res.text
        return res.text

    req = urllib.request.Request(url, headers=HEADERS)
    with urllib.request.urlopen(req, timeout=timeout) as response:
        body = response.read()
        charset = encoding or response.headers.get_content_charset() or "utf-8"
        return body.decode(charset, errors="replace")

def http_get_json(url, timeout=10):
    if requests is not None:
        res = requests.get(url, headers=HEADERS, timeout=timeout)
        return res.json()
    return json.loads(http_get_text(url, timeout=timeout))

MAX_IMPACT_NEWS = 5
MAX_GLOBAL_IMPACT_NEWS_PER_SOURCE = 2
MAX_NEWS_PER_CATEGORY = 3
SUMMARY_LINE_COUNT = 3
SUMMARY_MAX_CHARS = 110

GLOBAL_IMPACT_FEEDS = [
    ("Powerstack", "https://powerstack.sightlineclimate.com/feed/"),
    ("ImpactAlpha", "https://impactalpha.com/feed/")
]

SUMMARY_SKIP_KEYWORDS = ("무단전재", "재배포", "저작권", "copyright", "구독", "광고", "로그인")
BLOCKED_SOURCE_DOMAINS = ("blog.naver.com", "tistory.com", "youtube.com")

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
    {
        "id": "vcac", "label": "VC/AC",
        "groups": [
            {
                "title": "VC/AC",
                "categories": [
                    {"name": "빅딜/메가라운드", "query": "(스타트업 (시리즈C OR 시리즈D OR 메가라운드 OR 대규모 투자 유치) OR 벤처투자 대형딜)", "context": "대규모 투자 유치 소식입니다."},
                    {"name": "신규 펀드 결성", "query": "(벤처펀드 결성 OR 벤처캐피탈 출자 OR 모태펀드 선정 OR VC 블라인드 펀드)", "context": "신규 펀드 결성 소식입니다."},
                    {"name": "M&A 및 IPO", "query": "(스타트업 (인수합병 OR M&A OR 상장 OR IPO OR 예비심사 청구))", "context": "스타트업 회수 이벤트입니다."},
                ],
            },
        ],
    },
]

NAV_SECTIONS = (("impact", "임팩트"), ("vcac", "VC/AC"), ("ai", "AI"), ("macro", "거시경제"), ("theme", "🔥 강세테마"))

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
                        link = item.findtext("link", "")
                        try:
                            pub_date = parsedate_to_datetime(item.findtext("pubDate", "")).astimezone(KST).strftime("%Y.%m.%d")
                        except:
                            pub_date = datetime.now(KST).strftime("%Y.%m.%d")
                        
                        desc_text = item.findtext("description", "")
                        news_list.append({
                            "title": title,
                            "link": link,
                            "source": source_name,
                            "date": pub_date,
                            "summary": make_three_line_summary(title, strip_tags(desc_text), source_name, f"{theme_name} theme coverage.")
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
    parser.add_argument("--date", help="수집 기준일 (YYYY-MM-DD)")
    return parser.parse_args()

def get_target_date(date_arg=None):
    if not date_arg: return (datetime.now(KST) - timedelta(days=1)).date()
    return datetime.strptime(date_arg.strip().replace(".", "-"), "%Y-%m-%d").date()

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
    html_text = html_text.replace('<script src="archive_list.js"></script>', "")
    html_text = re.sub(
        r'\s*<div class="archive-picker">.*?</div>\s*</div>',
        '\n    </div>',
        html_text,
        count=1,
        flags=re.DOTALL,
    )
    return html_text

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
        return response.read().decode(response.headers.get_content_charset() or "utf-8", errors="replace")

def strip_tags(raw_html):
    return normalize_space(html.unescape(re.sub(r"<[^>]+>", " ", re.sub(r"<(script|style)\b[^>]*>.*?</\1>", " ", raw_html, flags=re.IGNORECASE | re.DOTALL))))

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
    text = strip_tags(raw_text)
    text = re.sub(r"\bv\.daum\.net\b", "파이낸셜뉴스", text, flags=re.IGNORECASE)
    text = re.sub(r"([.!?])\s+", r"\1|", text)
    for sentence in [normalize_space(p) for p in text.split("|") if normalize_space(p)]:
        sentence = re.sub(r"\bv\.daum\.net\b", "파이낸셜뉴스", sentence, flags=re.IGNORECASE)
        if len(sentence) < 15 or any(k in sentence.lower() for k in SUMMARY_SKIP_KEYWORDS) or sentence in title: continue
        key = sentence.casefold()
        if key not in seen:
            lines.append(sentence[:SUMMARY_MAX_CHARS - 1] + "…" if len(sentence) > SUMMARY_MAX_CHARS else sentence)
            seen.add(key)
        if len(lines) >= SUMMARY_LINE_COUNT: break
    
    fallbacks = [f"핵심: {title}", f"맥락: {context or '주요 이슈입니다.'}", f"출처: {source or '원문'}에서 확인 가능합니다."]
    for fb in fallbacks:
        if len(lines) >= SUMMARY_LINE_COUNT: break
        if fb.casefold() not in seen: lines.append(fb); seen.add(fb.casefold())
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

def extract_best_article_text(soup):
    candidates = []
    selectors = [
        "#article-view-content-div",
        "article",
        "main",
        "[class*='prose']",
        "[class*='content']",
        "[class*='article']",
    ]
    for selector in selectors:
        candidates.extend(soup.select(selector))
    if not candidates:
        candidates = [soup.body or soup]
    best = max(candidates, key=lambda node: len(normalize_space(node.get_text(" ", strip=True))))
    for tag in best.find_all(["script", "style", "noscript", "svg", "iframe", "button"]):
        tag.decompose()
    return normalize_space(best.get_text(" ", strip=True)) or normalize_space(soup.get_text(" ", strip=True))

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

def fetch_search_sections(target_date, seen_links, seen_titles):
    results = []
    target_dot = target_date.strftime("%Y.%m.%d")
    for section in SEARCH_SECTIONS:
        section_result = {"id": section["id"], "label": section["label"], "groups": []}
        for group in section["groups"]:
            group_result = {"title": group["title"], "categories": []}
            for category in group["categories"]:
                news_list = []
                try:
                    query = urllib.parse.quote(f"({category['query']}) -블로그 -카페 -blog -cafe when:2d")
                    rss_text = fetch_text(f"https://news.google.com/rss/search?q={query}&hl=ko&gl=KR")
                    for item in ElementTree.fromstring(rss_text).findall(".//item"):
                        if len(news_list) >= MAX_NEWS_PER_CATEGORY: break
                        title, source_name = parse_google_news_item(item)
                        source_name = normalize_source_name(source_name)
                        link = item.findtext("link", "")
                        
                        if section["id"] == "vcac" and not is_valid_vcac_title(title): continue
                        if should_skip_search_item(section["id"], category["name"], source_name): continue
                            
                        try:
                            if parsedate_to_datetime(item.findtext("pubDate", "")).astimezone(KST).strftime("%Y.%m.%d") != target_dot: continue
                        except: continue

                        if link in seen_links or any(is_similar_title(title, st) for st in seen_titles): continue
                        
                        seen_links.add(link); seen_titles.append(title)
                        news_list.append({
                            "title": title, "link": link, "source": source_name, "date": target_dot,
                            "summary": make_three_line_summary(title, strip_tags(item.findtext("description", "")), source_name, category["context"])
                        })
                except Exception as e: print("수집 오류:", e)
                group_result["categories"].append({"name": category["name"], "news": news_list})
            section_result["groups"].append(group_result)
        results.append(section_result)
    return results

# ==========================================
# 🌟 HTML 렌더링
# ==========================================
def render_html(target_date, domestic_impact, global_impact, search_sections, target_dash, dashboard, strong_theme, chart_data):
    target_dot = target_date.strftime("%Y.%m.%d")
    updated_at = datetime.now(KST).strftime("%Y.%m.%d %H:%M")

    counts = {s["id"]: sum(len(c["news"]) for g in s["groups"] for c in g["categories"]) for s in search_sections}
    counts["impact"] = len(domestic_impact) + len(global_impact)
    counts["theme"] = 1 if strong_theme and strong_theme["name"] != "강세테마 대기중" else 0

    chart_json = json.dumps(chart_data)

    def format_flow_html(flow_text):
        parts = []
        for piece in normalize_space(flow_text).split("/"):
            segment = normalize_space(piece)
            if not segment:
                continue
            if " " not in segment:
                parts.append(segment)
                continue
            label, value = segment.split(" ", 1)
            css_class = "neutral"
            if value.startswith("+"):
                css_class = "up"
            elif value.startswith("-"):
                css_class = "down"
            parts.append(f'{label} <span class="flow-value {css_class}">{value}</span>')
        return " / ".join(parts)

    impact_groups = {}
    for news in domestic_impact + global_impact:
        impact_groups.setdefault(news["source"], []).append(news)

    html_content = f"""<!DOCTYPE html>
<html lang="ko">
<head>
    <meta charset="UTF-8">
    <meta name="viewport" content="width=device-width, initial-scale=1.0">
    <title>오늘의 마켓 & 뉴스 브리핑</title>
    <script src="archive_list.js"></script>
    <link rel="preconnect" href="https://fonts.googleapis.com">
    <link rel="preconnect" href="https://fonts.gstatic.com" crossorigin>
    <link href="https://fonts.googleapis.com/css2?family=Inter:wght@300;400;500;600;700&family=Outfit:wght@400;500;600;700;800;900&family=Noto+Sans+KR:wght@300;400;500;700;900&display=swap" rel="stylesheet">
    <script src="https://cdn.jsdelivr.net/npm/apexcharts"></script>
    <style>
        :root {{
            --bg-main: #f8fafc;
            --bg-card: #ffffff;
            --text-main: #0f172a;
            --text-muted: #64748b;
            --border-color: #e2e8f0;
            --primary: #8b5cf6;
            --primary-light: #f5f3ff;
            --accent-blue: #0ea5e9;
            --accent-green: #10b981;
            --accent-orange: #f59e0b;
            --accent-red: #ef4444;
        }}
        
        * {{ box-sizing: border-box; margin: 0; padding: 0; }}
        body {{
            font-family: 'Inter', 'Noto Sans KR', sans-serif;
            background-color: var(--bg-main);
            color: var(--text-main);
            max-width: 1220px;
            margin: 0 auto;
            padding: 40px 18px 60px;
            line-height: 1.5;
        }}
        
        /* Header controls */
        .header-controls {{
            display: flex;
            flex-direction: column;
            align-items: center;
            margin-bottom: 35px;
            text-align: center;
        }}
        h1 {{
            font-family: 'Outfit', 'Noto Sans KR', sans-serif;
            font-size: 2.3rem;
            font-weight: 800;
            color: var(--text-main);
            margin-bottom: 10px;
            letter-spacing: -0.02em;
            background: linear-gradient(135deg, #1e293b 0%, #475569 100%);
            -webkit-background-clip: text;
            -webkit-text-fill-color: transparent;
        }}
        .date-title {{
            color: var(--text-muted);
            font-size: 0.95rem;
            margin-bottom: 15px;
            font-weight: 500;
            line-height: 1.4;
        }}
        .archive-picker {{
            display: flex;
            align-items: center;
            gap: 8px;
            background: #ffffff;
            padding: 6px 16px;
            border-radius: 9999px;
            border: 1px solid var(--border-color);
            box-shadow: 0 2px 10px rgba(0,0,0,0.02);
        }}
        .archive-picker label {{
            font-size: 0.85rem;
            font-weight: 600;
            color: var(--text-muted);
        }}
        .archive-picker select {{
            border: none;
            font-weight: 700;
            cursor: pointer;
            color: var(--primary);
            outline: none;
            font-size: 0.85rem;
            background: transparent;
        }}
        
        /* Dashboard cards */
        .dashboard-container {{
            display: grid;
            grid-template-columns: repeat(4, 1fr);
            gap: 18px;
            margin-bottom: 18px;
        }}
        .dash-card {{
            background-color: var(--bg-card);
            padding: 18px;
            border-radius: 16px;
            border: 1px solid var(--border-color);
            box-shadow: 0 4px 15px rgba(0,0,0,0.02);
            transition: all 0.3s cubic-bezier(0.4, 0, 0.2, 1);
            position: relative;
            overflow: hidden;
            display: flex;
            flex-direction: column;
            justify-content: space-between;
            min-height: 148px;
        }}
        .dash-card:hover {{
            transform: translateY(-4px);
            box-shadow: 0 8px 20px rgba(0,0,0,0.05);
        }}
        .dash-card::before {{
            content: '';
            position: absolute;
            top: 0;
            left: 0;
            width: 100%;
            height: 4px;
        }}
        .dash-card.blue::before {{ background: var(--accent-blue); }}
        .dash-card.green::before {{ background: var(--accent-green); }}
        .dash-card.orange::before {{ background: var(--accent-orange); }}
        .dash-card.purple::before {{ background: var(--primary); }}
        
        .dash-title {{
            font-size: 0.8rem;
            font-weight: 700;
            color: var(--text-muted);
            text-transform: uppercase;
            letter-spacing: 0.05em;
            margin-bottom: 6px;
            display: flex;
            align-items: center;
            gap: 6px;
        }}
        .dash-value {{
            font-family: 'Outfit', 'Noto Sans KR', sans-serif;
            font-size: 1.35rem;
            font-weight: 800;
            color: var(--text-main);
            line-height: 1.2;
            margin-bottom: 8px;
        }}
        .dash-flow {{
            font-size: 0.8rem;
            color: var(--text-muted);
            line-height: 1.5;
            margin-top: 2px;
        }}
        .market-block + .market-block {{
            margin-top: 10px;
        }}
        .market-label {{
            font-size: 0.76rem;
            font-weight: 700;
            color: var(--text-muted);
            text-transform: uppercase;
            letter-spacing: 0.03em;
            margin-bottom: 4px;
        }}
        .market-index {{
            font-family: 'Outfit', 'Noto Sans KR', sans-serif;
            font-size: 1rem;
            font-weight: 800;
            color: var(--text-main);
            line-height: 1.25;
            margin-bottom: 4px;
        }}
        .flow-row {{
            white-space: nowrap;
        }}
        .flow-value {{
            font-weight: 700;
        }}
        .flow-value.up {{
            color: var(--accent-red);
        }}
        .flow-value.down {{
            color: var(--accent-blue);
        }}
        .flow-value.neutral {{
            color: var(--text-main);
        }}
        .sparkline-container {{
            width: 100%;
            height: 35px;
            margin-top: auto;
        }}
        
        /* Collapsible Charts Section */
        .market-charts-toggle {{
            background-color: var(--bg-card);
            border: 1px solid var(--border-color);
            border-radius: 12px;
            padding: 12px 20px;
            font-size: 0.88rem;
            font-weight: 700;
            color: var(--text-main);
            display: flex;
            justify-content: space-between;
            align-items: center;
            cursor: pointer;
            margin-bottom: 25px;
            box-shadow: 0 2px 10px rgba(0,0,0,0.01);
            transition: all 0.2s ease;
            user-select: none;
        }}
        .market-charts-toggle:hover {{
            background-color: #f8fafc;
            border-color: #cbd5e1;
        }}
        .toggle-icon {{
            font-size: 0.75rem;
            color: var(--text-muted);
            transition: transform 0.3s ease;
        }}
        .market-charts-wrapper {{
            max-height: 0;
            overflow: hidden;
            transition: max-height 0.5s cubic-bezier(0.4, 0, 0.2, 1);
            margin-bottom: 0;
        }}
        .market-charts-wrapper.expanded {{
            max-height: 800px;
            margin-bottom: 25px;
        }}
        .chart-grid {{
            display: grid;
            grid-template-columns: repeat(2, 1fr);
            gap: 15px;
            padding-bottom: 10px;
        }}
        .chart-card {{
            background-color: var(--bg-card);
            border: 1px solid var(--border-color);
            border-radius: 16px;
            padding: 16px;
            box-shadow: 0 4px 15px rgba(0,0,0,0.01);
        }}
        .chart-card h3 {{
            font-family: 'Outfit', 'Noto Sans KR', sans-serif;
            font-size: 0.95rem;
            font-weight: 700;
            color: var(--text-main);
            margin-bottom: 12px;
            display: flex;
            align-items: center;
            gap: 6px;
        }}
        
        /* Tabs navigation */
        .top-tabs {{
            display: grid;
            grid-template-columns: repeat(5, 1fr);
            gap: 10px;
            margin-bottom: 25px;
            position: sticky;
            top: 0;
            background: rgba(248, 250, 252, 0.9);
            backdrop-filter: blur(10px);
            padding: 10px 0;
            z-index: 10;
        }}
        .nav-tab {{
            font-family: 'Outfit', 'Noto Sans KR', sans-serif;
            font-size: 0.9rem;
            border: 1px solid var(--border-color);
            background: var(--bg-card);
            color: var(--text-main);
            padding: 12px;
            font-weight: 700;
            border-radius: 10px;
            cursor: pointer;
            display: flex;
            align-items: center;
            justify-content: center;
            gap: 5px;
            transition: all 0.2s ease;
            box-shadow: 0 2px 5px rgba(0,0,0,0.01);
        }}
        .nav-tab:hover {{
            border-color: #cbd5e1;
            background-color: #f8fafc;
        }}
        .nav-tab.active {{
            background: var(--text-main);
            color: #ffffff;
            border-color: var(--text-main);
            box-shadow: 0 4px 12px rgba(15, 23, 42, 0.15);
        }}
        .tab-count {{
            background: rgba(0,0,0,0.06);
            border-radius: 9999px;
            padding: 1px 6px;
            font-size: 0.7rem;
            font-weight: 700;
            color: var(--text-muted);
        }}
        .nav-tab.active .tab-count {{
            background: rgba(255,255,255,0.2);
            color: #ffffff;
        }}
        
        /* Sections layout */
        .content-section {{ display: none; animation: fadeIn 0.4s ease; }}
        .content-section.active {{ display: block; }}
        @keyframes fadeIn {{
            from {{ opacity: 0; transform: translateY(8px); }}
            to {{ opacity: 1; transform: translateY(0); }}
        }}
        
        .section-title {{
            font-family: 'Outfit', 'Noto Sans KR', sans-serif;
            font-size: 1.5rem;
            color: var(--text-main);
            border-left: 5px solid var(--primary);
            padding-left: 12px;
            margin: 25px 0 15px;
            font-weight: 800;
        }}
        #section-macro .section-title {{ border-left-color: var(--accent-blue); }}
        #section-ai .section-title {{ border-left-color: var(--accent-green); }}
        #section-vcac .section-title {{ border-left-color: var(--accent-orange); }}
        #section-impact .section-title {{ border-left-color: #ec4899; }}
        
        .group-title {{
            margin: 20px 0 10px;
            padding: 8px 14px;
            background: #e2e8f0;
            border-radius: 6px;
            font-weight: 700;
            font-size: 1rem;
            color: #334155;
        }}
        .sub-category {{
            font-size: 0.95rem;
            color: var(--text-main);
            margin: 12px 0 8px;
            font-weight: 700;
            display: flex;
            align-items: center;
            gap: 6px;
        }}
        
        /* News Cards */
        .news-card {{
            background: var(--bg-card);
            padding: 16px;
            margin-bottom: 12px;
            border-radius: 10px;
            border: 1px solid var(--border-color);
            box-shadow: 0 2px 10px rgba(0,0,0,0.01);
            transition: all 0.2s ease;
        }}
        .news-card:hover {{
            border-color: #cbd5e1;
            box-shadow: 0 4px 15px rgba(0,0,0,0.03);
        }}
        .news-title {{
            font-size: 1.05rem;
            font-weight: 700;
            margin-bottom: 6px;
            line-height: 1.45;
        }}
        .news-title a {{
            color: var(--text-main);
            text-decoration: none;
            transition: color 0.15s ease;
        }}
        .news-title a:hover {{
            color: var(--primary);
        }}
        .news-date {{
            font-size: 0.78rem;
            color: var(--text-muted);
            margin-bottom: 10px;
            font-weight: 500;
        }}
        .news-summary {{
            margin: 0;
            padding-left: 18px;
            color: #334155;
            font-size: 0.9rem;
            line-height: 1.6;
        }}
        .news-summary li {{
            margin-bottom: 4px;
        }}
        .no-news {{
            color: var(--text-muted);
            font-style: italic;
            padding: 12px;
            background: #f1f5f9;
            border-radius: 8px;
            font-size: 0.88rem;
            border: 1px dashed var(--border-color);
        }}
        
        /* Strong Theme Styling */
        .theme-container {{
            background: var(--bg-card);
            border-radius: 16px;
            border: 1px solid var(--border-color);
            padding: 24px;
            box-shadow: 0 4px 20px rgba(0,0,0,0.02);
            margin-bottom: 25px;
        }}
        .theme-header-box {{
            display: flex;
            justify-content: space-between;
            align-items: center;
            border-bottom: 1px solid var(--border-color);
            padding-bottom: 16px;
            margin-bottom: 20px;
        }}
        .theme-badge-title {{
            display: flex;
            align-items: center;
            gap: 10px;
        }}
        .theme-icon-badge {{
            background: var(--primary-light);
            color: var(--primary);
            padding: 6px 12px;
            border-radius: 9999px;
            font-weight: 800;
            font-size: 0.8rem;
            text-transform: uppercase;
            letter-spacing: 0.05em;
        }}
        .theme-title-text {{
            font-family: 'Outfit', 'Noto Sans KR', sans-serif;
            font-size: 1.45rem;
            font-weight: 800;
            color: var(--text-main);
        }}
        .theme-rate-badge {{
            background: #fee2e2;
            color: var(--accent-red);
            font-family: 'Outfit', sans-serif;
            padding: 6px 14px;
            border-radius: 9999px;
            font-weight: 800;
            font-size: 1.05rem;
            box-shadow: 0 2px 8px rgba(239, 68, 68, 0.08);
        }}
        .theme-desc-box {{
            background: #f8fafc;
            border-left: 4px solid var(--primary);
            padding: 14px 18px;
            border-radius: 0 10px 10px 0;
            margin-bottom: 24px;
            font-size: 0.92rem;
            line-height: 1.6;
            color: #334155;
        }}
        .theme-subtitle {{
            font-family: 'Outfit', 'Noto Sans KR', sans-serif;
            font-size: 1.05rem;
            font-weight: 700;
            color: var(--text-main);
            margin-bottom: 12px;
            display: flex;
            align-items: center;
            gap: 6px;
        }}
        
        /* Modern Table for stocks */
        .stocks-table-wrapper {{
            overflow-x: auto;
            border-radius: 10px;
            border: 1px solid var(--border-color);
            margin-bottom: 28px;
        }}
        .stocks-table {{
            width: 100%;
            border-collapse: collapse;
            text-align: left;
            font-size: 0.88rem;
        }}
        .stocks-table th {{
            background-color: #f8fafc;
            color: var(--text-muted);
            font-weight: 700;
            padding: 12px 15px;
            border-bottom: 1px solid var(--border-color);
            font-size: 0.78rem;
            text-transform: uppercase;
            letter-spacing: 0.05em;
        }}
        .stocks-table td {{
            padding: 14px 15px;
            border-bottom: 1px solid var(--border-color);
            color: #334155;
            vertical-align: middle;
        }}
        .stocks-table tr:last-child td {{
            border-bottom: none;
        }}
        .stocks-table tr:hover td {{
            background-color: #f8fafc;
        }}
        .stock-name-cell a {{
            font-weight: 700;
            color: var(--text-main);
            text-decoration: none;
        }}
        .stock-name-cell a:hover {{
            color: var(--primary);
            text-decoration: underline;
        }}
        .stock-price-cell {{
            font-family: 'Outfit', sans-serif;
            font-weight: 600;
        }}
        .stock-rate-cell span {{
            font-family: 'Outfit', sans-serif;
            font-weight: 700;
            padding: 3px 8px;
            border-radius: 6px;
            font-size: 0.78rem;
        }}
        .stock-rate-cell span.up {{
            background-color: #fee2e2;
            color: var(--accent-red);
        }}
        .stock-rate-cell span.down {{
            background-color: #e0f2fe;
            color: var(--accent-blue);
        }}
        .stock-reason-cell {{
            font-size: 0.82rem;
            line-height: 1.5;
            color: var(--text-muted);
        }}
        
        @media (max-width: 992px) {{
            .dashboard-container {{ grid-template-columns: repeat(2, 1fr); }}
            .chart-grid {{ grid-template-columns: 1fr; }}
            .top-tabs {{ grid-template-columns: repeat(3, 1fr); }}
        }}
        @media (max-width: 576px) {{
            .dashboard-container {{ grid-template-columns: 1fr; }}
            .top-tabs {{ grid-template-columns: repeat(2, 1fr); }}
            h1 {{ font-size: 1.85rem; }}
            .theme-header-box {{ flex-direction: column; align-items: flex-start; gap: 10px; }}
            .theme-rate-badge {{ align-self: flex-start; }}
        }}
    </style>
</head>
<body>
    <div class="header-controls">
        <h1>오늘의 마켓 & 뉴스 브리핑</h1>
        <div class="date-title">수집 기준일: {target_dot} 전일 기사 <br> 최종 갱신: {updated_at} KST</div>
        <div class="archive-picker">
            <label for="history-select">🗓️ 과거기사: </label>
            <select id="history-select"></select>
        </div>
    </div>

    <div class="dashboard-container">
        <div class="dash-card blue" style="cursor: pointer;" onclick="toggleMarketCharts(true)">
            <div class="dash-title">🇺🇸 미 10년물 금리</div>
            <div class="dash-value">{dashboard['us_10y']}</div>
            <div class="sparkline-container" id="sparkline-us-10y"></div>
        </div>
        <div class="dash-card green" style="cursor: pointer;" onclick="toggleMarketCharts(true)">
            <div class="dash-title">💱 원/달러 환율</div>
            <div class="dash-value">{dashboard['fx_info']}</div>
            <div class="sparkline-container" id="sparkline-fx"></div>
        </div>
        <div class="dash-card orange" style="cursor: pointer;" onclick="toggleMarketCharts(true)">
            <div class="dash-title">📈 코스피 / 코스닥</div>
            <div class="dash-flow">
                <div class="market-block">
                    <div class="market-label">코스피 지수</div>
                    <div class="market-index">{dashboard['kospi_info']}</div>
                    <div class="flow-row">{format_flow_html(dashboard['kospi_flow'])}</div>
                </div>
                <div class="market-block">
                    <div class="market-label">코스닥 지수</div>
                    <div class="market-index">{dashboard['kosdaq_info']}</div>
                    <div class="flow-row">{format_flow_html(dashboard['kosdaq_flow'])}</div>
                </div>
            </div>
            <div class="sparkline-container" id="sparkline-kospi"></div>
        </div>
        <div class="dash-card purple" onclick="activateThemeTab()" style="cursor:pointer;">
            <div class="dash-title">🔥 국내 강세 테마</div>
            <div class="dash-value" style="font-size:1.1rem; color:var(--primary);">Top 1: {strong_theme['name']}</div>
            <div class="sparkline-container" id="sparkline-theme" style="display: flex; align-items: center; justify-content: center; font-size: 0.75rem; font-weight: 700; color: var(--primary);">
                자세히 보기 &rarr;
            </div>
        </div>
    </div>

    <!-- Collapsible Market Charts Section -->
    <div class="market-charts-toggle" onclick="toggleMarketCharts()">
        <span>📊 실시간 시장 지표 30일 시계열 추이 분석 (클릭하여 열기)</span>
        <span class="toggle-icon" id="toggle-icon">▼</span>
    </div>
    <div class="market-charts-wrapper" id="market-charts-wrapper">
        <div class="chart-grid">
            <div class="chart-card">
                <h3>🇺🇸 미 10년물 국채금리 30일 추이</h3>
                <div id="chart-us-10y"></div>
            </div>
            <div class="chart-card">
                <h3>💱 원/달러 환율 30일 추이</h3>
                <div id="chart-fx"></div>
            </div>
            <div class="chart-card">
                <h3>📈 코스피 지수 30일 추이</h3>
                <div id="chart-kospi"></div>
            </div>
            <div class="chart-card">
                <h3>📉 코스닥 지수 30일 추이</h3>
                <div id="chart-kosdaq"></div>
            </div>
        </div>
    </div>

    <div class="top-tabs" role="tablist">
"""
    for idx, (sid, label) in enumerate(NAV_SECTIONS):
        act = " active" if idx == 0 else ""
        html_content += f'<button class="nav-tab{act}" data-target="section-{sid}">{label}<span class="tab-count">{counts.get(sid,0)}</span></button>\n'
    html_content += "</div>\n"

    # 🔥 1. 강세테마 탭 내용
    html_content += f"""<section id="section-theme" class="content-section">
        <div class="section-title">국내 강세테마 분석</div>
        <div class="theme-container">
            <div class="theme-header-box">
                <div class="theme-badge-title">
                    <span class="theme-icon-badge">HOT THEME</span>
                    <span class="theme-title-text">{strong_theme['name']}</span>
                </div>
                <span class="theme-rate-badge">{strong_theme['rate']}</span>
            </div>
            
            <div class="theme-subtitle">💡 테마 개요</div>
            <div class="theme-desc-box">{strong_theme['desc']}</div>
            
            <div class="theme-subtitle">💎 주요 대장 종목 Top 5</div>
            <div class="stocks-table-wrapper">
                <table class="stocks-table">
                    <thead>
                        <tr>
                            <th style="width: 20%;">종목명</th>
                            <th style="width: 15%;">현재가</th>
                            <th style="width: 15%;">등락률</th>
                            <th style="width: 50%;">기업 해설</th>
                        </tr>
                    </thead>
                    <tbody>
    """
    
    if not strong_theme['stocks']:
        html_content += "<tr><td colspan='4' class='no-news' style='text-align:center;'>수집된 테마 대장 종목이 없습니다.</td></tr>"
    else:
        for stock in strong_theme['stocks']:
            rate_class = "up" if "+" in stock["rate"] else "down"
            html_content += f"""
                        <tr>
                            <td class="stock-name-cell"><a href="https://finance.naver.com/item/main.naver?code={stock['code']}" target="_blank">{stock['name']}</a></td>
                            <td class="stock-price-cell">{stock['price']}원</td>
                            <td class="stock-rate-cell"><span class="{rate_class}">{stock['rate']}</span></td>
                            <td class="stock-reason-cell">{stock['reason']}</td>
                        </tr>
            """
            
    html_content += """
                    </tbody>
                </table>
            </div>
            
            <div class="theme-subtitle">📰 테마 관련 주요 최신 뉴스</div>
    """
    
    if not strong_theme['news']:
        html_content += "<div class='no-news'>관련 뉴스를 찾을 수 없습니다.</div>"
    else:
        for news in strong_theme['news']:
            html_content += f"""
            <div class="news-card">
                <div class="news-title"><a href="{news['link']}" target="_blank">{news['title']}</a></div>
                <div class="news-date">출처: {news['source']} | 발행일: {news['date']}</div>
                <ul class="news-summary">
            """
            for line in news["summary"]:
                html_content += f"<li>{line}</li>"
            html_content += "</ul></div>"
            
    html_content += """
        </div>
    </section>
    """

    # Other categories sections
    section_map = {section["id"]: section for section in search_sections}
    for sid in ("vcac", "ai", "macro"):
        section = section_map.get(sid)
        if not section:
            continue
        html_content += f'<section id="section-{section["id"]}" class="content-section"><div class="section-title">{section["label"]}</div>\n'
        for group in section["groups"]:
            html_content += f'<div class="group-title">{group["title"]}</div>\n'
            for category in group["categories"]:
                html_content += f'<div class="sub-category">📌 {category["name"]}</div>\n'
                if not category["news"]: html_content += "<div class='no-news'>전일 수집된 뉴스가 없습니다.</div>\n"
                for news in category["news"]:
                    html_content += f'<div class="news-card"><div class="news-title"><a href="{news["link"]}" target="_blank">{news["title"]}</a></div>'
                    html_content += f'<div class="news-date">출처: {news["source"]} | 발행일: {news["date"]}</div><ul class="news-summary">'
                    for line in news["summary"]: html_content += f"<li>{line}</li>"
                    html_content += "</ul></div>\n"
        html_content += "</section>\n"

    html_content += '<section id="section-impact" class="content-section active"><div class="section-title">임팩트 브리핑</div>\n'
    if not impact_groups:
        html_content += "<div class='no-news'>수집된 기사가 없습니다.</div>\n"
    for source_name, news_list in impact_groups.items():
        html_content += f'<div class="sub-category">📌 {source_name}</div>\n'
        for news in news_list:
            html_content += f'<div class="news-card"><div class="news-title"><a href="{news["link"]}" target="_blank">{news["title"]}</a></div>'
            html_content += f'<div class="news-date">출처: {news["source"]} | 발행일: {news["date"]}</div><ul class="news-summary">'
            for line in news["summary"]: html_content += f"<li>{line}</li>"
            html_content += "</ul></div>\n"
    html_content += "</section>\n"

    html_content += f"""
    <script>
        const chartData = {chart_json};
        
        // Tabs logic
        const tabs = document.querySelectorAll(".nav-tab");
        const sections = document.querySelectorAll(".content-section");
        tabs.forEach(tab => {{
            tab.addEventListener("click", () => {{
                tabs.forEach(t => t.classList.remove("active"));
                sections.forEach(s => s.classList.remove("active"));
                tab.classList.add("active");
                document.getElementById(tab.dataset.target).classList.add("active");
            }});
        }});
        
        function activateThemeTab() {{
            const themeTab = document.querySelector('.nav-tab[data-target="section-theme"]');
            if (themeTab) themeTab.click();
            document.querySelector(".header-controls").scrollIntoView({{ behavior: 'smooth' }});
        }}
        
        function toggleMarketCharts(forceExpand = false) {{
            const wrapper = document.getElementById("market-charts-wrapper");
            const icon = document.getElementById("toggle-icon");
            if (wrapper.classList.contains("expanded") && !forceExpand) {{
                wrapper.classList.remove("expanded");
                icon.textContent = "▼";
            }} else {{
                wrapper.classList.add("expanded");
                icon.textContent = "▲";
                // Trigger chart resizing for ApexCharts
                setTimeout(() => {{
                    window.dispatchEvent(new Event('resize'));
                }}, 100);
            }}
        }}
        
        if (typeof archiveDates !== 'undefined') {{
            const sel = document.getElementById('history-select');
            archiveDates.forEach(d => {{
                let opt = document.createElement('option'); opt.value = d; opt.textContent = d;
                if (d === "{target_dash}") opt.selected = true;
                sel.appendChild(opt);
            }});
            sel.addEventListener('change', e => {{
                window.location.href = e.target.value === archiveDates[0] ? 'index.html' : 'archive_' + e.target.value + '.html';
            }});
        }}
        
        // --- APEXCHARTS INITIALIZATION ---
        document.addEventListener("DOMContentLoaded", () => {{
            const colors = {{
                us10y: '#0ea5e9',
                fx: '#10b981',
                kospi: '#f59e0b',
                kosdaq: '#f59e0b'
            }};
            
            // 1. Sparklines Options
            const getSparklineOpt = (dates, values, color) => ({{
                series: [{{ name: 'Close', data: values }}],
                chart: {{ type: 'area', height: 35, sparkline: {{ enabled: true }}, animations: {{ enabled: true }} }},
                stroke: {{ curve: 'smooth', width: 2 }},
                fill: {{
                    type: 'gradient',
                    gradient: {{ shadeIntensity: 1, opacityFrom: 0.4, opacityTo: 0.02, stops: [0, 100] }}
                }},
                colors: [color],
                tooltip: {{ fixed: {{ enabled: false }}, x: {{ show: false }}, y: {{ title: {{ formatter: () => '' }} }}, marker: {{ show: false }} }}
            }});
            
            // 2. Full Charts Options
            const getFullChartOpt = (title, dates, values, color, suffix="") => ({{
                series: [{{ name: title, data: values }}],
                chart: {{ type: 'area', height: 220, toolbar: {{ show: false }}, zoom: {{ enabled: false }} }},
                colors: [color],
                dataLabels: {{ enabled: false }},
                stroke: {{ curve: 'smooth', width: 3 }},
                fill: {{
                    type: 'gradient',
                    gradient: {{ shadeIntensity: 1, opacityFrom: 0.45, opacityTo: 0.05, stops: [0, 95, 100] }}
                }},
                grid: {{ borderColor: '#f1f5f9', strokeDashArray: 4 }},
                xaxis: {{
                    categories: dates,
                    labels: {{ style: {{ colors: '#94a3b8', fontSize: '10px', fontFamily: 'Inter' }} }},
                    axisBorder: {{ show: false }},
                    axisTicks: {{ show: false }}
                }},
                yaxis: {{
                    labels: {{ 
                        style: {{ colors: '#94a3b8', fontSize: '10px', fontFamily: 'Inter' }},
                        formatter: (v) => v.toFixed(v > 100 ? 1 : 3) + suffix
                    }}
                }},
                tooltip: {{ x: {{ show: true }}, y: {{ formatter: (v) => v.toLocaleString() + suffix }} }}
            }});
            
            // Render Sparklines
            if (chartData.us_10y && chartData.us_10y.values.length) {{
                new ApexCharts(document.querySelector("#sparkline-us-10y"), getSparklineOpt(chartData.us_10y.dates, chartData.us_10y.values, colors.us10y)).render();
                new ApexCharts(document.querySelector("#chart-us-10y"), getFullChartOpt('금리', chartData.us_10y.dates, chartData.us_10y.values, colors.us10y, '%')).render();
            }}
            if (chartData.fx && chartData.fx.values.length) {{
                new ApexCharts(document.querySelector("#sparkline-fx"), getSparklineOpt(chartData.fx.dates, chartData.fx.values, colors.fx)).render();
                new ApexCharts(document.querySelector("#chart-fx"), getFullChartOpt('환율', chartData.fx.dates, chartData.fx.values, colors.fx, '원')).render();
            }}
            if (chartData.kospi && chartData.kospi.values.length) {{
                new ApexCharts(document.querySelector("#sparkline-kospi"), getSparklineOpt(chartData.kospi.dates, chartData.kospi.values, colors.kospi)).render();
                new ApexCharts(document.querySelector("#chart-kospi"), getFullChartOpt('코스피', chartData.kospi.dates, chartData.kospi.values, colors.kospi)).render();
            }}
            if (chartData.kosdaq && chartData.kosdaq.values.length) {{
                new ApexCharts(document.querySelector("#chart-kosdaq"), getFullChartOpt('코스닥', chartData.kosdaq.dates, chartData.kosdaq.values, colors.kosdaq)).render();
            }}
        }});
    </script>
</body>
</html>
"""
    return html_content

def main():
    args = parse_args()
    target_date = get_target_date(args.date)
    target_dot = target_date.strftime("%Y.%m.%d")
    target_dash = target_date.strftime("%Y-%m-%d")
    seen_links, seen_titles = set(), [] 
    
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

    env = {}
    env_path = BASE_DIR / ".env"
    if env_path.exists():
        for raw_line in env_path.read_text(encoding="utf-8", errors="ignore").splitlines():
            line = raw_line.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            key, value = line.split("=", 1)
            env[key.strip()] = value.strip().strip('"').strip("'")

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
                    seen_links.add(link); seen_titles.append(title)
                    impact_news.append({"title": title, "link": link, "date": target_dot, "source": "임팩트온", "summary": ["국내 주요 ESG 및 임팩트 비즈니스 이슈입니다."]})
            except: continue
    except: pass

    all_impact = impact_news + global_impact + trellis_news + causeartist_news + socialimpact_news + eroun_news + newsletter_news
    domestic_impact, global_impact = [], []
    for news in all_impact:
        if is_domestic_news(news["title"], news["summary"], news["source"]): domestic_impact.append(news)
        else: global_impact.append(news)

    search_sections = fetch_search_sections(target_date, seen_links, seen_titles)

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
