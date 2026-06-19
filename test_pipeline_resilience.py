import io
import tempfile
import unittest
import urllib.error
from datetime import date
from pathlib import Path
from unittest import mock

import agent_a
import agent_b
import main


class PipelineSelectionTests(unittest.TestCase):
    def make_news(self, index, source="source"):
        return {
            "title": f"기사 {index}",
            "link": f"https://example.com/{index}",
            "source": source,
            "summary": [f"기사 {index} 요약"],
        }

    def test_selected_news_count_has_no_global_cap(self):
        theme = {"news": [self.make_news(i, "theme") for i in range(3)]}
        impact = [self.make_news(10 + i, f"impact-{i // 3}") for i in range(15)]
        sections = [{
            "groups": [{
                "categories": [
                    {"news": [self.make_news(100 + i * 3 + j, f"category-{i}") for j in range(3)]}
                    for i in range(8)
                ]
            }]
        }]

        selected = main.count_selected_news(theme, impact, sections)

        self.assertEqual(selected, 42)
        self.assertEqual(len(impact), 15)

    def test_agent_a_archive_mode_fetches_every_final_body(self):
        cards = "".join(
            f'<article class="news-card"><div class="news-title"><a href="https://example.com/{i}">기사 {i}</a></div>'
            f'<div class="news-date">출처: 테스트 | 발행일: 2026.06.17</div><ul class="news-summary"><li>요약</li></ul></article>'
            for i in range(35)
        )
        html = f'<section id="section-impact" class="content-section"><h2>임팩트 브리핑</h2>{cards}</section>'
        with tempfile.TemporaryDirectory() as tmp:
            archive = Path(tmp) / "archive.html"
            archive.write_text(html, encoding="utf-8")
            options = agent_a.AgentAOptions(
                target_date=date(2026, 6, 17),
                output_dir=Path(tmp),
                selection_archive=archive,
            )
            with mock.patch.object(main, "fetch_article_body_text", return_value="본문 " * 200) as fetch_body:
                rows = agent_a.collect_raw_articles(options)

        self.assertEqual(len(rows), 35)
        self.assertEqual(fetch_body.call_count, 35)


class GeminiCircuitBreakerTests(unittest.TestCase):
    def test_first_429_stops_all_remaining_batches(self):
        rows = [
            {"article_id": f"a{i}", "title": f"기사 {i}", "original_text": "본문 " * 100}
            for i in range(6)
        ]
        error = urllib.error.HTTPError(
            "https://example.com",
            429,
            "Too Many Requests",
            {},
            io.BytesIO(b'{"error":{"status":"RESOURCE_EXHAUSTED"}}'),
        )
        with mock.patch.object(main, "call_gemini_json", side_effect=error) as call:
            results, errors = agent_b.summarize_with_gemini(
                rows,
                {"GEMINI_API_KEY": "test"},
                "gemini-test",
                batch_size=2,
                retry_attempts=2,
                retry_base_delay=1,
                inter_batch_delay=0,
            )
        error.close()

        self.assertEqual(results, {})
        self.assertEqual(set(errors), {row["article_id"] for row in rows})
        self.assertEqual(call.call_count, 1)


if __name__ == "__main__":
    unittest.main()
