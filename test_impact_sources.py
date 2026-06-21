import unittest
from datetime import date
from unittest.mock import patch

import main


class ImpactAlphaFeedTests(unittest.TestCase):
    def test_fetch_global_impact_preserves_rss_link_text(self):
        feed = """<?xml version="1.0" encoding="UTF-8"?>
        <rss version="2.0">
          <channel>
            <item>
              <title>Impact investing expands into a new market</title>
              <link>https://impactalpha.com/example-impact-story/</link>
              <pubDate>Fri, 19 Jun 2026 11:12:00 +0000</pubDate>
              <description><![CDATA[An impact investing article with useful details.]]></description>
            </item>
            <broken-tag></mismatched-tag>
          </channel>
        </rss>"""

        with (
            patch.object(main, "GLOBAL_IMPACT_FEEDS", [("ImpactAlpha", "https://impactalpha.com/feed/")]),
            patch.object(main, "fetch_text", return_value=feed),
            patch.object(main, "fetch_article_body_text", return_value=""),
            patch.object(main, "make_three_line_summary", return_value=["one", "two", "three"]),
        ):
            items = main.fetch_global_impact(date(2026, 6, 19), set(), [])

        self.assertEqual(len(items), 1)
        self.assertEqual(items[0]["source"], "ImpactAlpha")
        self.assertEqual(items[0]["link"], "https://impactalpha.com/example-impact-story/")
        self.assertEqual(items[0]["date"], "2026.06.19")


if __name__ == "__main__":
    unittest.main()
