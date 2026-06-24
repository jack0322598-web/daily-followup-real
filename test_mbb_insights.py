import unittest
from datetime import date
from unittest.mock import patch

import main


class MbbInsightsTest(unittest.TestCase):
    def test_parse_kpmg_listing_items_extracts_article_cards(self):
        page_html = """
        <div class="cmp-teaser">
          <h3 class="cmp-teaser__title">
            <a class="cmp-teaser__title-link" href="/kr/ko/insights/eri/2026/report-a.html">첫 번째 리포트</a>
          </h3>
          <div class="cmp-teaser__description">첫 번째 설명입니다.</div>
          <div class="cmp-teaser__image"><img src="https://assets.kpmg.com/a.png"></div>
        </div>
        <div class="cmp-teaser">
          <h3 class="cmp-teaser__title">
            <a class="cmp-teaser__title-link" href="/kr/ko/insights/eri.html">카테고리 링크</a>
          </h3>
        </div>
        """

        items = main.parse_kpmg_listing_items(page_html)

        self.assertEqual(len(items), 1)
        self.assertEqual(items[0]["title"], "첫 번째 리포트")
        self.assertEqual(items[0]["url"], "https://kpmg.com/kr/ko/insights/eri/2026/report-a.html")
        self.assertEqual(items[0]["description"], "첫 번째 설명입니다.")

    def test_parse_deloitte_listing_items_extracts_promos(self):
        page_html = """
        <div class="promo cmp-promo--featured-primary">
          <a href="/kr/ko/services/consulting/perspectives/report-a.html">
            <div class="cmp-promo__content">
              <h3 class="cmp-promo__content__title">딜로이트 리포트 A</h3>
              <div class="cmp-promo__content__desc">
                <p>설명 첫 줄</p>
                <p>설명 둘째 줄</p>
              </div>
            </div>
            <div class="cmp-promo__image"><img src="/content/dam/a.png"></div>
          </a>
        </div>
        <div class="promo cmp-promo--featured-primary">
          <a href="/kr/ko/our-thinking/deloitte-insights.html">
            <div class="cmp-promo__content">
              <h3 class="cmp-promo__content__title">리스트 페이지</h3>
            </div>
          </a>
        </div>
        """

        items = main.parse_deloitte_listing_items(page_html)

        self.assertEqual(len(items), 1)
        self.assertEqual(items[0]["title"], "딜로이트 리포트 A")
        self.assertEqual(items[0]["url"], "https://www.deloitte.com/kr/ko/services/consulting/perspectives/report-a.html")
        self.assertEqual(items[0]["description"], "설명 첫 줄 설명 둘째 줄")

    def test_kpmg_sitemap_picks_latest_eri_article(self):
        sitemap_xml = """<?xml version="1.0" encoding="UTF-8"?>
        <urlset xmlns="http://www.sitemaps.org/schemas/sitemap/0.9">
          <url>
            <loc>https://kpmg.com/kr/ko/insights/eri/2026/old.html</loc>
            <lastmod>2026-06-10T02:30:28.145Z</lastmod>
          </url>
          <url>
            <loc>https://kpmg.com/kr/ko/insights/eri/2026/latest.html</loc>
            <lastmod>2026-06-11T04:25:26.923Z</lastmod>
          </url>
          <url>
            <loc>https://kpmg.com/kr/ko/insights/aci/2026/ignore.html</loc>
            <lastmod>2026-06-12T04:25:26.923Z</lastmod>
          </url>
        </urlset>
        """

        original = main.http_get_text
        main.http_get_text = lambda url, timeout=40: sitemap_xml
        try:
            latest = main.fetch_kpmg_latest_from_sitemap()
        finally:
            main.http_get_text = original

        self.assertEqual(latest, "https://kpmg.com/kr/ko/insights/eri/2026/latest.html")

    def test_bain_feed_filters_exact_target_date(self):
        payload = {
            "featuredResult": {
                "title": "Featured",
                "date": "Jun 18, 2026",
                "url": "/insights/featured/",
                "description": "Featured description with enough detail for a summary.",
                "imageSrc": {"large": "/featured.jpg"},
            },
            "results": [
                {
                    "title": "Target article",
                    "date": "Jun 18, 2026",
                    "url": "/insights/target/",
                    "description": "Target description with enough detail for a summary.",
                    "imageSrc": {},
                },
                {
                    "title": "Wrong day",
                    "date": "Jun 17, 2026",
                    "url": "/insights/wrong-day/",
                    "description": "This item must not be included.",
                    "imageSrc": {},
                },
            ],
        }

        items = main.parse_bain_feed_items(payload, date(2026, 6, 18))

        self.assertEqual([item["title"] for item in items], ["Featured", "Target article"])
        self.assertTrue(all(item["date"] == "2026.06.18" for item in items))
        self.assertTrue(all(len(item["summary"]) == 3 for item in items))

    def test_bcg_html_filters_exact_target_date(self):
        page_html = """
        <article class="Promo">
          <div class="Promo-date">June 18, 2026</div>
          <div class="Promo-title"><a href="/publications/2026/target">Target BCG article</a></div>
          <div class="Promo-description">Target BCG description.</div>
          <img src="/target.jpg">
        </article>
        <article class="Promo">
          <div class="Promo-date">June 17, 2026</div>
          <div class="Promo-title"><a href="/publications/2026/old">Old BCG article</a></div>
          <div class="Promo-description">Old BCG description.</div>
        </article>
        """

        items = main.parse_bcg_publication_items(page_html, date(2026, 6, 18))

        self.assertEqual(len(items), 1)
        self.assertEqual(items[0]["title"], "Target BCG article")
        self.assertEqual(items[0]["source"], "BCG")
        self.assertEqual(items[0]["date"], "2026.06.18")

    def test_bcg_html_excludes_chinese_language_title(self):
        page_html = """
        <article class="Promo">
          <div class="Promo-date">June 18, 2026</div>
          <div class="Promo-title"><a href="/publications/2026/china-global-fintech-report">金融科技新篇章：规模化领先者与新兴破局者</a></div>
          <div class="Promo-description">Chinese BCG description.</div>
        </article>
        <article class="Promo">
          <div class="Promo-date">June 18, 2026</div>
          <div class="Promo-title"><a href="/publications/2026/target">Target BCG article</a></div>
          <div class="Promo-description">Target BCG description.</div>
        </article>
        """

        items = main.parse_bcg_publication_items(page_html, date(2026, 6, 18))

        self.assertEqual(len(items), 1)
        self.assertEqual(items[0]["title"], "Target BCG article")

    def test_bcg_reader_markdown_filters_exact_target_date(self):
        markdown = """
        ## Most Recent Insights
        ![Image 1](https://example.com/target.jpg)
        [Artificial Intelligence](https://www.bcg.com/capabilities/artificial-intelligence)
        Article
        June 18, 2026
        [Target insight](https://www.bcg.com/publications/2026/target-insight)
        Target insight description.
        [Learn More](https://www.bcg.com/publications/2026/target-insight)

        Article
        June 17, 2026
        [Old insight](https://www.bcg.com/publications/2026/old-insight)
        Old insight description.
        [Learn More](https://www.bcg.com/publications/2026/old-insight)
        ## Featured Campaigns
        """

        items = main.parse_bcg_publication_markdown(markdown, date(2026, 6, 18))

        self.assertEqual(len(items), 1)
        self.assertEqual(items[0]["title"], "Target insight")
        self.assertEqual(items[0]["chart_image_url"], "https://example.com/target.jpg")

    def test_bcg_sitemap_candidates_include_nearby_modified_publications(self):
        sitemap = """
        [https://www.bcg.com/publications/2026/same-day](https://www.bcg.com/publications/2026/same-day)
        2026-06-17T10:00:00-04:00
        [https://www.bcg.com/publications/2026/day-before](https://www.bcg.com/publications/2026/day-before)
        2026-06-16T10:00:00-04:00
        [https://www.bcg.com/publications/2026/too-old](https://www.bcg.com/publications/2026/too-old)
        2026-06-14T10:00:00-04:00
        """

        candidates = main.parse_bcg_sitemap_candidates(sitemap, date(2026, 6, 17))

        self.assertEqual(candidates, [
            "https://www.bcg.com/publications/2026/same-day",
            "https://www.bcg.com/publications/2026/day-before",
        ])

    def test_bcg_reader_article_verifies_published_date(self):
        article = """Title: Complete BCG Article
URL Source: https://www.bcg.com/publications/2026/complete
Published Time: 2026-06-17
Markdown Content:
The first substantive paragraph explains the article and its main conclusion.
The second paragraph provides additional evidence and context for the reader.
"""

        item = main.parse_bcg_reader_article(
            article, "https://www.bcg.com/publications/2026/complete", date(2026, 6, 17)
        )

        self.assertEqual(item["title"], "Complete BCG Article")
        self.assertEqual(item["date"], "2026.06.17")
        self.assertIsNone(main.parse_bcg_reader_article(
            article, "https://www.bcg.com/publications/2026/complete", date(2026, 6, 18)
        ))

    def test_bcg_cleaner_prefers_key_takeaways_over_navigation(self):
        markdown = """Title: BCG Sample
URL Source: https://www.bcg.com/publications/2026/sample
Published Time: 2026-06-18
Markdown Content:
# BCG Sample | BCG
Log in
Manage Subscriptions
Featured Insights
Article June 18, 2026 8 MIN read

## Key Takeaways

By following a structured approach, companies can create substantial value from AI.

* Players are struggling to scale pilots and measure impact.
* AI can increase worker productivity by 15% to 25% and improve energy yield.
* Recommendations include disciplined governance and operational adoption.

Save It For Later
## Authors
"""

        cleaned = main.clean_bcg_markdown_text(markdown)

        self.assertIn("companies can create substantial value from AI", cleaned)
        self.assertIn("worker productivity by 15% to 25%", cleaned)
        self.assertNotIn("Manage Subscriptions", cleaned)
        self.assertNotIn("Featured Insights", cleaned)

    def test_extract_consulting_article_body_text_uses_source_specific_selectors(self):
        deloitte_html = """
        <html><body>
          <div class="aem-GridColumn">바로가기: noisy intro 리포트 전문 다운로드 PDF 보기 문의하기</div>
          <div class="cmp-text">
            안정적이던 상용차 비즈니스 모델이 흔들리고 있습니다.
            딜로이트는 제품 복잡성과 소유 구조를 바탕으로 네 가지 시나리오를 제시했습니다.
            국내 OEM의 전략 방향을 함께 짚었습니다.
          </div>
        </body></html>
        """
        body = main.extract_consulting_article_body_text(
            main.BeautifulSoup(deloitte_html, "html.parser"),
            "상용차 산업의 재편",
            source="Deloitte",
        )

        self.assertIn("안정적이던 상용차 비즈니스 모델이 흔들리고 있습니다.", body)
        self.assertNotIn("리포트 전문 다운로드", body)

    def test_summary_fallback_recomposes_key_points(self):
        summary = main.make_three_line_summary(
            "상용차 산업의 재편",
            (
                "안정적이던 상용차 비즈니스 모델이 흔들리고 있습니다. "
                "내연기관·소유 중심의 기존사업 모델은 재편되고 탈탄소 규제와 전동화가 산업 재편을 가속화하고 있습니다. "
                "딜로이트는 제품 복잡성과 소유 구조를 토대로 2035년 시장의 네 가지 시나리오를 도출했습니다. "
                "국내 OEM과 업계 관계자들에게 실질적인 전략 방향을 제시하고자 하였습니다."
            ),
            "Deloitte",
            "Deloitte Insights 최신 발행 자료입니다.",
        )

        self.assertEqual(len(summary), 3)
        self.assertTrue(any("네 가지 시나리오" in line for line in summary))
        self.assertTrue(any("전략 방향" in line or "산업 재편" in line for line in summary))

    def test_mbb_render_keeps_mckinsey_as_insight_card_and_others_as_news_cards(self):
        target_date = date(2026, 6, 18)
        items = [
            main.build_mbb_item(
                "McKinsey", "McKinsey insight", "https://www.mckinsey.com/insight",
                target_date, "McKinsey description.", "https://example.com/mckinsey.jpg",
            ),
            main.build_mbb_item(
                "Bain & Company", "Bain insight", "https://www.bain.com/insight",
                target_date, "Bain description.", "https://example.com/bain.jpg",
            ),
            main.build_mbb_item(
                "BCG", "BCG insight", "https://www.bcg.com/insight",
                target_date, "BCG description.", "https://example.com/bcg.jpg",
            ),
        ]

        rendered = main.render_html(
            target_date, [], [], [], "2026-06-18", {},
            {"name": "강세테마 대기중", "rate": "-", "stocks": [], "news": []},
            {}, items,
        )
        section = main.BeautifulSoup(rendered, "html.parser").select_one("#section-industry")

        self.assertIn("source-tab-section", section.get("class", []))
        self.assertEqual(len(section.select("[data-source-target]")), 3)
        self.assertEqual(len(section.select("[data-source-panel]")), 3)
        self.assertIsNotNone(section.select_one(".impact-source-strip.mbb-source-strip"))
        self.assertIsNotNone(section.select_one(".impact-news-stage"))
        self.assertIsNotNone(section.select_one(".impact-brand-mckinsey"))
        self.assertIsNotNone(section.select_one(".impact-brand-bain"))
        self.assertIsNotNone(section.select_one(".impact-brand-bcg"))
        self.assertEqual(len(section.select(".industry-card")), 1)
        self.assertEqual(len(section.select(".mbb-news-card")), 2)
        self.assertEqual(
            section.select_one('[data-source-panel="industry-mckinsey"] .industry-card h3').get_text(strip=True),
            "McKinsey insight",
        )
        self.assertIsNotNone(section.select_one('[data-source-panel="industry-mckinsey"] .industry-actions'))
        self.assertIsNone(section.select_one('[data-source-panel="industry-mckinsey"] .industry-card .industry-summary'))
        self.assertTrue(all(card.select_one(".news-title a") for card in section.select(".mbb-news-card")))
        self.assertTrue(all(card.select_one(".news-date") for card in section.select(".mbb-news-card")))
        self.assertEqual(len(section.select('[data-source-panel="industry-mckinsey"] .industry-chart-image')), 1)
        self.assertEqual(len(section.select('[data-source-panel="industry-bain-company"] .industry-chart-image')), 0)
        self.assertEqual(len(section.select('[data-source-panel="industry-bcg"] .industry-chart-image')), 0)

    def test_mckinsey_known_fallback_restores_chart_and_report_link(self):
        item = main.build_mckinsey_fallback_item({
            "title": "Trauma's toll on the workforce",
            "published_date": "2026.06.18",
            "source_url": "https://www.mckinsey.com/featured-insights/week-in-charts/traumas-toll-on-the-workforce",
        })

        self.assertEqual(item["date"], "2026.06.18")
        self.assertIn("leadersstrain-ex1.svgz", item["chart_image_url"])
        self.assertEqual(
            item["report_url"],
            "https://www.mckinsey.com/capabilities/people-and-organizational-performance/our-insights/how-leaders-can-help-their-organizations-metabolize-strain",
        )
        self.assertEqual(item["report_title"], "How leaders can help their organizations metabolize strain")
        self.assertIn("Employees who report experiencing trauma", item["chart_image_alt"])

    def test_mckinsey_ai_pricing_fallback_restores_chart_visual(self):
        item = main.build_mckinsey_fallback_item({
            "title": "The AI advantage in B2B pricing",
            "published_date": "2026.06.23",
            "source_url": "https://www.mckinsey.com/featured-insights/week-in-charts/the-ai-advantage-in-b2b-pricing",
        })

        self.assertEqual(item["date"], "2026.06.23")
        self.assertTrue(item["chart_image_url"].startswith("data:image/svg+xml"))
        self.assertIn("agentic AI breakthrough in pricing", item["chart_image_alt"])
        self.assertEqual(
            item["report_url"],
            "https://www.mckinsey.com/capabilities/growth-marketing-and-sales/our-insights/b2b-pricing-navigating-the-next-phase-of-the-ai-revolution",
        )
        self.assertIn("생성형 AI", item["description_ko"])

    def test_mckinsey_parser_prefers_chart_srcset_images(self):
        article_html = """
        <html>
          <head>
            <title>The AI advantage in B2B pricing | McKinsey</title>
            <meta name="itemdate" content="2026-06-23">
          </head>
          <body>
            <h1>The AI advantage in B2B pricing</h1>
            <picture>
              <source srcset="/~/media/mckinsey/featured%20insights/the%20week%20in%20charts/2026/june/exhibits/aipricing.svgz 1x">
              <img alt="Image: Gen AI is more mature, but an agentic AI breakthrough in pricing is on the horizon.">
            </picture>
            <p>Image description: A dot chart titled “Gen AI is more mature, but an agentic AI breakthrough in pricing is on the horizon”. Source: McKinsey AI in Pricing Survey. End of image description.</p>
            <a href="/capabilities/growth-marketing-and-sales/our-insights/b2b-pricing-navigating-the-next-phase-of-the-ai-revolution">B2B pricing: Navigating the next phase</a>
          </body>
        </html>
        """

        item = main.parse_mckinsey_week_article(
            article_html,
            "https://www.mckinsey.com/featured-insights/week-in-charts/the-ai-advantage-in-b2b-pricing",
            {"published_date": "2026.06.23"},
        )

        self.assertIn("/~/media/mckinsey/featured%20insights/", item["chart_image_url"])
        self.assertIn("aipricing.svgz", item["chart_image_url"])
        self.assertIn("Gen AI is more mature", item["chart_image_alt"])
        self.assertIn("/our-insights/b2b-pricing-navigating-the-next-phase", item["report_url"])

    def test_industry_trend_preserves_legacy_mckinsey_item_after_fetch_failure(self):
        previous_item = {
            "source": "McKinsey",
            "title": "Previous weekly insight",
            "date": "2026.06.18",
            "source_url": "https://www.mckinsey.com/previous",
        }
        legacy_cache = {
            "date": "2026.06.18",
            "items": [previous_item],
            "mckinsey_last_known": [],
        }
        saved_payload = {}

        with (
            patch.object(main, "load_industry_trend_cache", return_value=legacy_cache),
            patch.object(main, "fetch_mckinsey_items", side_effect=RuntimeError("temporary failure")),
            patch.object(main, "fetch_bain_items", return_value=[]),
            patch.object(main, "fetch_bcg_items", return_value=[]),
            patch.object(main, "save_industry_trend_cache", side_effect=saved_payload.update),
        ):
            items = main.fetch_industry_trend(date(2026, 6, 19))

        self.assertIn(previous_item, items)
        self.assertEqual(saved_payload["mckinsey_last_known"], [previous_item])

    def test_render_html_includes_industrytrend_section(self):
        target_date = date(2026, 6, 18)
        industry_source_trend = [
            main.build_industry_source_item(
                "KPMG",
                "KPMG 최신 리포트",
                "https://kpmg.com/example",
                target_date,
                "KPMG 설명입니다.",
                raw_text="KPMG 설명입니다. 추가 요약 텍스트입니다.",
            ),
            main.build_industry_source_item(
                "Deloitte",
                "Deloitte 최신 리포트",
                "https://deloitte.com/example",
                target_date,
                "Deloitte 설명입니다.",
                raw_text="Deloitte 설명입니다. 추가 요약 텍스트입니다.",
            ),
        ]

        rendered = main.render_html(
            target_date, [], [], [], "2026-06-18", {},
            {"name": "강세테마 대기중", "rate": "-", "stocks": [], "news": []},
            {}, [], industry_source_trend=industry_source_trend,
        )
        section = main.BeautifulSoup(rendered, "html.parser").select_one("#section-industrytrend")

        self.assertIsNotNone(section)
        self.assertIn("source-tab-section", section.get("class", []))
        self.assertEqual(len(section.select("[data-source-target]")), 2)
        self.assertIsNotNone(section.select_one('[data-source-panel="industrytrend-kpmg"] .news-card'))
        self.assertIsNotNone(section.select_one('[data-source-panel="industrytrend-deloitte"] .news-card'))


if __name__ == "__main__":
    unittest.main()
