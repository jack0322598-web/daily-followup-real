import unittest
from datetime import date
from unittest.mock import patch

import main


class GlobalImpactFeedTests(unittest.TestCase):
    def test_fetch_global_impact_preserves_rss_link_text(self):
        feed = """<?xml version="1.0" encoding="UTF-8"?>
        <rss version="2.0">
          <channel>
            <item>
              <title>Impact investing expands into a new market</title>
              <link>https://example.com/example-impact-story/</link>
              <pubDate>Fri, 19 Jun 2026 11:12:00 +0000</pubDate>
              <description><![CDATA[An impact investing article with useful details.]]></description>
            </item>
            <broken-tag></mismatched-tag>
          </channel>
        </rss>"""

        with (
            patch.object(main, "GLOBAL_IMPACT_FEEDS", [("Example Impact", "https://example.com/feed/")]),
            patch.object(main, "fetch_text", return_value=feed),
            patch.object(main, "fetch_article_body_text", return_value=""),
            patch.object(main, "make_three_line_summary", return_value=["one", "two", "three"]),
        ):
            items = main.fetch_global_impact(date(2026, 6, 19), set(), [])

        self.assertEqual(len(items), 1)
        self.assertEqual(items[0]["source"], "Example Impact")
        self.assertEqual(items[0]["link"], "https://example.com/example-impact-story/")
        self.assertEqual(items[0]["date"], "2026.06.19")


class AiNewsFeedTests(unittest.TestCase):
    def test_ai_news_rss_keeps_both_articles_on_kst_date(self):
        feed = """<?xml version="1.0" encoding="UTF-8"?>
        <rss><channel>
          <item>
            <title>SAP and Google Cloud deploy agentic commerce architecture</title>
            <link>https://www.artificialintelligence-news.com/news/sap-google/</link>
            <pubDate>Fri, 19 Jun 2026 14:02:20 +0000</pubDate>
            <description>First AI article.</description>
          </item>
          <item>
            <title>e2e-assure introduces an AI-driven SOC platform</title>
            <link>https://www.artificialintelligence-news.com/news/e2e-assure/</link>
            <pubDate>Fri, 19 Jun 2026 09:57:55 +0000</pubDate>
            <description>Second AI article.</description>
          </item>
          <item>
            <title>Older AI article</title>
            <link>https://www.artificialintelligence-news.com/news/older/</link>
            <pubDate>Thu, 18 Jun 2026 16:00:00 +0000</pubDate>
            <description>Older article.</description>
          </item>
        </channel></rss>"""
        config = next(item for item in main.AI_RSS_SOURCE_CONFIGS if item["source"] == "AI News")

        def build_item(source_name, title, link, *_args, **_kwargs):
            return {"source": source_name, "title": title, "link": link}

        with (
            patch.object(main, "fetch_source_text", return_value=feed),
            patch.object(main, "build_source_news_item", side_effect=build_item),
            patch.object(main.time, "sleep"),
        ):
            items = main.fetch_ai_rss_source(config, date(2026, 6, 19), set(), [])

        self.assertEqual(len(items), 2)
        self.assertTrue(all(item["source"] == "AI News" for item in items))


class MarketingTechFeedTests(unittest.TestCase):
    def test_marketingtech_uses_backup_feed_and_filters_categories(self):
        challenge_page = "<html><title>Just a moment...</title></html>"
        backup_feed = """<?xml version="1.0" encoding="UTF-8"?>
        <rss><channel>
          <item>
            <title>Yext opens platform access for AI marketing workflows</title>
            <link>https://www.marketingtechnews.net/news/yext-ai/</link>
            <pubDate>Fri, 19 Jun 2026 12:53:25 +0000</pubDate>
            <category>AI &amp; Intelligent Marketing</category>
            <description>First marketing AI article.</description>
          </item>
          <item>
            <title>Warner Bros expands agentic AI use in ad buying</title>
            <link>https://www.marketingtechnews.net/news/warner-ai/</link>
            <pubDate>Fri, 19 Jun 2026 09:00:00 +0000</pubDate>
            <category>AI &amp; Intelligent Marketing</category>
            <description>Second marketing AI article.</description>
          </item>
          <item>
            <title>General social media marketing article</title>
            <link>https://www.marketingtechnews.net/news/social/</link>
            <pubDate>Fri, 19 Jun 2026 08:00:00 +0000</pubDate>
            <category>Social Media Marketing</category>
            <description>Not an AI category article.</description>
          </item>
        </channel></rss>"""
        config = dict(next(item for item in main.AI_RSS_SOURCE_CONFIGS if item["source"] == "MarketingTech"))
        config["feed_attempts"] = 1

        def build_item(source_name, title, link, *_args, **_kwargs):
            return {"source": source_name, "title": title, "link": link}

        with (
            patch.object(main, "fetch_source_text", side_effect=[challenge_page, backup_feed]),
            patch.object(main, "build_source_news_item", side_effect=build_item),
            patch.object(main.time, "sleep"),
        ):
            items = main.fetch_ai_rss_source(config, date(2026, 6, 19), set(), [])

        self.assertEqual(len(items), 2)
        self.assertTrue(all(item["source"] == "MarketingTech" for item in items))


if __name__ == "__main__":
    unittest.main()
