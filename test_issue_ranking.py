import unittest
from datetime import date
from unittest.mock import patch

import main


class IssueRankingTests(unittest.TestCase):
    def make_item(self, title, source="연합뉴스", text=""):
        return {
            "title": title,
            "source": source,
            "link": f"https://example.com/{abs(hash((title, source)))}",
            "_summary_source": text or (title + " 공식 발표 내용 " * 60),
        }

    def test_same_issue_is_clustered_and_gets_coverage_bonus(self):
        items = [
            self.make_item("연준, 기준금리 동결…파월 물가 위험 강조", "연합뉴스"),
            self.make_item("FOMC 기준금리 동결, 파월 인플레이션 경고", "한국경제"),
            self.make_item("미 연준 금리 동결…인하 시점은 늦춰질 듯", "매일경제"),
            self.make_item("전문가가 본 하반기 미국 금리 전망", "조선일보"),
        ]

        ranked = main.cluster_and_rank_issues(items)

        self.assertEqual(ranked[0]["_coverage_count"], 3)
        self.assertEqual(len(ranked[0]["_related_articles"]), 2)
        self.assertGreater(ranked[0]["_importance_score"], ranked[1]["_importance_score"])

    def test_previous_briefing_issue_is_penalized(self):
        item = self.make_item("연준 기준금리 동결…파월 기자회견")
        fresh_score = main.score_issue_candidate(dict(item), [])
        repeated_score = main.score_issue_candidate(dict(item), ["연준 기준금리 동결…파월 발언"])

        self.assertEqual(fresh_score - repeated_score, 18)

    def test_macro_headline_must_directly_match_category(self):
        self.assertTrue(main.is_macro_news_match("미국", "경제지표", "美 5월 수입물가 6.7% 상승"))
        self.assertFalse(main.is_macro_news_match("미국", "경제지표", "일본, 31년 만에 기준금리 1%"))
        self.assertFalse(main.is_macro_news_match("미국", "경제지표", "뉴욕증시, FOMC 대기하며 상승"))

    def test_macro_fetch_uses_description_and_body_for_final_match(self):
        rss = """<?xml version="1.0" encoding="UTF-8"?>
        <rss><channel>
          <item>
            <title>미국 소비 둔화 조짐 - 연합뉴스</title>
            <link>https://news.google.com/rss/articles/example</link>
            <pubDate>Mon, 22 Jun 2026 01:00:00 +0900</pubDate>
            <description>미국 소매판매 지표 관련 속보</description>
          </item>
        </channel></rss>"""
        body = "미국 5월 소매판매가 전월 대비 부진하게 나오며 경기 둔화 우려가 커졌다."
        category = {
            "name": "경제지표",
            "query": "미국 소비",
            "context": "미국 경기 흐름 기사입니다.",
        }

        with (
            patch.object(main, "fetch_text", return_value=rss),
            patch.object(main, "resolve_google_news_url", return_value="https://www.yna.co.kr/view/AKR202606220001"),
            patch.object(main, "fetch_article_body_text", return_value=body),
            patch.object(main, "refine_issue_ranking_with_gemini", side_effect=lambda items, *_args: items),
            patch.object(main, "make_three_line_summary", return_value=["요약 1", "요약 2", "요약 3"]),
        ):
            items = main.fetch_google_news_for_category(
                date(2026, 6, 22),
                "macro",
                "미국",
                category,
                set(),
                [],
            )

        self.assertEqual(len(items), 1)
        self.assertEqual(items[0]["link"], "https://www.yna.co.kr/view/AKR202606220001")

    def test_ap_business_macro_uses_english_site_query(self):
        rss = """<?xml version="1.0" encoding="UTF-8"?>
        <rss><channel>
          <item>
            <title>Federal Reserve keeps interest rates steady as inflation cools - AP News</title>
            <link>https://news.google.com/rss/articles/ap-example</link>
            <pubDate>Mon, 22 Jun 2026 02:00:00 +0900</pubDate>
            <description>The Federal Reserve held interest rates steady as monetary policy officials watched inflation.</description>
          </item>
        </channel></rss>"""
        body = (
            "The Federal Reserve held interest rates steady after inflation cooled. "
            "Officials said monetary policy could remain restrictive if price pressures return."
        )
        category = {
            "name": "통화정책",
            "query": "미국 연준",
            "context": "연준 금리 경로 기사입니다.",
        }
        fetched_urls = []

        def fake_fetch_text(url, *_args, **_kwargs):
            fetched_urls.append(url)
            return rss

        with (
            patch.object(main, "fetch_text", side_effect=fake_fetch_text),
            patch.object(main, "resolve_google_news_url", return_value="https://apnews.com/article/fed-rates-inflation"),
            patch.object(main, "fetch_article_body_text", return_value=body),
            patch.object(main, "make_three_line_summary", return_value=["요약 1", "요약 2", "요약 3"]),
        ):
            items = main.fetch_ap_business_macro_news(
                date(2026, 6, 22),
                "macro",
                "미국",
                category,
                set(),
                [],
            )

        self.assertEqual(len(items), 1)
        self.assertEqual(items[0]["source"], "AP News")
        self.assertEqual(items[0]["link"], "https://apnews.com/article/fed-rates-inflation")
        self.assertIn("site%3Aapnews.com", fetched_urls[0])
        self.assertIn("Federal%20Reserve", fetched_urls[0])

    def test_yahoo_finance_macro_uses_economy_listing_articles(self):
        listing_html = """
        <html><body>
          <a href="/economy/policy/articles/fed-rates-inflation.html">Fed story</a>
          <a href="/topic/tariffs/">Tariffs topic</a>
        </body></html>"""
        article_html = """
        <html>
          <head>
            <title>Federal Reserve keeps interest rates steady as inflation cools - Yahoo Finance</title>
            <meta name="description" content="The Federal Reserve held interest rates steady as monetary policy officials watched inflation.">
            <script type="application/ld+json">{"datePublished":"2026-06-22T02:00:00+09:00"}</script>
          </head>
          <body>
            <article>
              The Federal Reserve held interest rates steady after inflation cooled.
              Officials said monetary policy could remain restrictive if price pressures return.
            </article>
          </body>
        </html>"""
        category = {
            "name": "통화정책",
            "query": "미국 연준",
            "context": "연준 금리 경로 기사입니다.",
        }
        fetched_urls = []

        def fake_fetch_text(url, *_args, **_kwargs):
            fetched_urls.append(url)
            if url == main.YAHOO_FINANCE_ECONOMY_URL:
                return listing_html
            return article_html

        with (
            patch.object(main, "fetch_text", side_effect=fake_fetch_text),
            patch.object(main, "make_three_line_summary", return_value=["요약 1", "요약 2", "요약 3"]),
        ):
            main.YAHOO_FINANCE_ECONOMY_LINK_CACHE = None
            main.YAHOO_FINANCE_ARTICLE_CACHE.clear()
            items = main.fetch_yahoo_finance_macro_news(
                date(2026, 6, 22),
                "macro",
                "미국",
                category,
                set(),
                [],
            )

        self.assertEqual(len(items), 1)
        self.assertEqual(items[0]["source"], "Yahoo Finance")
        self.assertEqual(items[0]["link"], "https://finance.yahoo.com/economy/policy/articles/fed-rates-inflation.html")
        self.assertEqual(fetched_urls[0], main.YAHOO_FINANCE_ECONOMY_URL)
        self.assertFalse(main.is_yahoo_finance_source("https://finance.yahoo.com/topic/tariffs/"))


if __name__ == "__main__":
    unittest.main()
