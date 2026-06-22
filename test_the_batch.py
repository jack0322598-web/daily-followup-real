import unittest
from datetime import date, datetime
from unittest import mock

import main


class TheBatchCollectionTests(unittest.TestCase):
    def test_reader_timestamp_is_converted_from_pacific_time_to_kst(self):
        item = main.parse_the_batch_reader_article(
            """Title: Data Points: Claude Fable 5, or Mythos for the masses
Published Time: 2026-06-10T08:06:21.000-07:00
Markdown Content:
Today's Data Points summary.
""",
            "https://www.deeplearning.ai/the-batch/claude-fable-5-or-mythos-for-the-masses",
        )

        self.assertEqual(item["title"], "Claude Fable 5, or Mythos for the masses")
        self.assertEqual(item["date"].strftime("%Y.%m.%d %H:%M"), "2026.06.11 00:06")

    def test_data_points_missing_dates_are_selected_on_their_kst_day(self):
        items = [
            {"title": "DiffusionGemma", "link": "https://example.com/16", "date": datetime(2026, 6, 16, 5, tzinfo=main.KST), "description": "summary"},
            {"title": "GLM 5.2", "link": "https://example.com/18", "date": datetime(2026, 6, 18, 2, tzinfo=main.KST), "description": "summary"},
        ]
        built = lambda source, title, link, *_args, **_kwargs: {"title": title, "link": link}

        with mock.patch.object(main, "build_source_news_item", side_effect=built):
            june_16 = main.fetch_ai_listing_items("The Batch Data Points", items, date(2026, 6, 16), set(), [], "context")
            june_18 = main.fetch_ai_listing_items("The Batch Data Points", items, date(2026, 6, 18), set(), [], "context")

        self.assertEqual([item["title"] for item in june_16], ["DiffusionGemma"])
        self.assertEqual([item["title"] for item in june_18], ["GLM 5.2"])

    def test_weekly_listing_is_not_requested_outside_friday(self):
        requested_pages = []

        def collect(page_url, **_kwargs):
            requested_pages.append(page_url)
            return []

        with (
            mock.patch.object(main, "AI_RSS_SOURCE_CONFIGS", []),
            mock.patch.object(main, "collect_aitimes_listing_items", return_value=[]),
            mock.patch.object(main, "collect_ai_news_listing_items", return_value=[]),
            mock.patch.object(main, "collect_the_batch_listing_items", side_effect=collect),
        ):
            section = main.fetch_ai_sources(date(2026, 6, 18), set(), [])

        weekly = next(category for category in section["groups"][0]["categories"] if category["name"] == "The Batch Weekly Issues")
        self.assertEqual(weekly["news"], [])
        self.assertEqual(requested_pages, ["https://www.deeplearning.ai/the-batch/tag/data-points"])

    def test_weekly_listing_is_requested_on_friday(self):
        requested_pages = []

        def collect(page_url, **_kwargs):
            requested_pages.append(page_url)
            return []

        with (
            mock.patch.object(main, "AI_RSS_SOURCE_CONFIGS", []),
            mock.patch.object(main, "collect_aitimes_listing_items", return_value=[]),
            mock.patch.object(main, "collect_ai_news_listing_items", return_value=[]),
            mock.patch.object(main, "collect_the_batch_listing_items", side_effect=collect),
        ):
            main.fetch_ai_sources(date(2026, 6, 19), set(), [])

        self.assertEqual(requested_pages, [
            "https://www.deeplearning.ai/the-batch/tag/data-points",
            "https://www.deeplearning.ai/the-batch",
        ])


class TheBatchRenderTests(unittest.TestCase):
    def render(self, target_date):
        ai_section = {
            "id": "ai",
            "label": "AI",
            "groups": [{
                "title": "AI",
                "categories": [
                    {"name": source_name, "news": []}
                    for source_name in main.AI_SOURCE_PRIORITY
                ],
            }],
        }
        return main.render_html(
            target_date, [], [], [ai_section], target_date.isoformat(), {},
            {"name": "강세테마 대기중", "rate": "-", "stocks": [], "news": []},
            {}, [],
        )

    def test_friday_weekly_panel_shows_schedule_notice(self):
        soup = main.BeautifulSoup(self.render(date(2026, 6, 19)), "html.parser")
        panel = soup.select_one('[data-source-panel="ai-the-batch-weekly-issues"]')

        self.assertIn("매주 금요일에만 발행", panel.select_one(".source-schedule-note").get_text(" ", strip=True))

    def test_non_friday_weekly_panel_has_zero_items_and_no_notice(self):
        soup = main.BeautifulSoup(self.render(date(2026, 6, 18)), "html.parser")
        panel = soup.select_one('[data-source-panel="ai-the-batch-weekly-issues"]')
        card = soup.select_one('[data-source-target="ai-the-batch-weekly-issues"]')

        self.assertIsNone(panel.select_one(".source-schedule-note"))
        self.assertIn("0건", card.get_text(" ", strip=True))


if __name__ == "__main__":
    unittest.main()
