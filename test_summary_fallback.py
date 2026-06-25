import unittest
from unittest import mock

import main


class SummaryFallbackTests(unittest.TestCase):
    def tearDown(self):
        main.DEFER_INLINE_SUMMARIES = False
        main.SUMMARY_ENV = {}
        main.SUMMARY_CACHE = {}
        main.SUMMARY_CACHE_DIRTY = False
        main.SUMMARY_AI_DISABLED_REASON = ""

    def test_inline_summaries_can_be_deferred_until_final_display_selection(self):
        final_news = {
            "title": "최종 표시 기사",
            "source": "테스트",
            "summary": main.make_three_line_summary("최종 표시 기사", "수집 중 원문입니다.", "테스트", "테스트 기사입니다."),
            "_summary_source": "최종 화면에 표시되는 기사만 요약해야 합니다. 본문과 설명을 기준으로 핵심 내용을 정리합니다.",
            "_summary_context": "테스트 기사입니다.",
        }
        skipped_news = {
            "title": "탈락 후보 기사",
            "source": "테스트",
            "summary": [],
            "_summary_source": "이 후보는 최종 화면에 표시되지 않습니다.",
            "_summary_context": "테스트 기사입니다.",
        }

        main.DEFER_INLINE_SUMMARIES = True
        self.assertEqual(main.make_three_line_summary("수집 후보", "본문", "테스트", "테스트"), [])
        main.DEFER_INLINE_SUMMARIES = False

        section = {"groups": [{"categories": [{"news": [final_news]}]}]}
        main.ensure_final_display_summaries({}, [], [], [section])

        self.assertEqual(len(final_news["summary"]), 3)
        self.assertTrue(all(main.contains_hangul(line) for line in final_news["summary"]))
        self.assertEqual(skipped_news["summary"], [])

    def test_ai_summary_retries_missing_batch_items_individually(self):
        batch_lines = [
            "첫 번째 기사는 정책 변화의 핵심 내용을 간결하게 정리합니다.",
            "본문에 나온 배경과 주요 수치를 바탕으로 의미를 설명합니다.",
            "시장과 이해관계자에게 미칠 후속 영향을 함께 짚습니다.",
        ]
        retry_lines = [
            "두 번째 기사는 기업 전략 변화의 핵심 흐름을 정리합니다.",
            "본문에 제시된 근거를 바탕으로 배경과 이해관계를 설명합니다.",
            "산업과 투자자 관점에서 확인해야 할 영향을 짚습니다.",
        ]
        first = {
            "title": "첫 번째 최종 기사",
            "source": "테스트",
            "summary": [],
            "_summary_source": "정책 변화와 시장 영향에 관한 본문입니다. " * 8,
            "_summary_context": "테스트 기사입니다.",
        }
        second = {
            "title": "두 번째 최종 기사",
            "source": "테스트",
            "summary": [],
            "_summary_source": "기업 전략 변화와 산업 영향에 관한 본문입니다. " * 8,
            "_summary_context": "테스트 기사입니다.",
        }
        section = {"groups": [{"categories": [{"news": [first, second]}]}]}

        with (
            mock.patch.dict(main.SUMMARY_ENV, {
                "GEMINI_API_KEY": "test-key",
                "AI_SUMMARY_ENABLED": "1",
                "AI_SUMMARY_BATCH_SIZE": "2",
            }, clear=True),
            mock.patch.object(main, "apply_ai_summary_batch", return_value={"n1": batch_lines}) as batch,
            mock.patch.object(main, "generate_editor_summary_with_gemini", return_value=retry_lines) as single_retry,
        ):
            main.apply_ai_summaries_to_news({}, [], [], [section])

        self.assertEqual(first["summary"], batch_lines)
        self.assertEqual(first["_summary_mode"], "ai-batch:gemini-2.5-flash")
        self.assertEqual(second["summary"], retry_lines)
        self.assertEqual(second["_summary_mode"], "ai-single-retry")
        batch.assert_called_once()
        single_retry.assert_called_once()

    def test_korean_summary_fallback_uses_natural_briefing_language(self):
        summary = main.build_korean_summary_fallback(
            title="AP News reports on inflation and markets",
            source="AP News",
            context="거시경제 미국 경제지표 주요 뉴스입니다.",
            source_lines=[],
        )

        self.assertEqual(len(summary), 3)
        self.assertTrue(all(main.contains_hangul(line) for line in summary))
        self.assertTrue(any("보도는" in line for line in summary))
        self.assertFalse(any("원문 제목과 본문" in line or "원문 링크" in line for line in summary))

    def test_english_summary_lines_are_koreanized_instead_of_generic_fallback(self):
        lines = [
            "The Federal Reserve held interest rates steady as inflation cooled.",
            "Officials said monetary policy could remain restrictive if price pressures return.",
            "Investors watched the decision for signals on the next move in rates.",
        ]

        summary = main.ensure_korean_summary_lines(
            lines,
            title="Federal Reserve keeps interest rates steady as inflation cools",
            source="AP News",
            context="거시경제 미국 통화정책 주요 뉴스입니다.",
        )

        self.assertEqual(len(summary), 3)
        self.assertTrue(all(main.contains_hangul(line) for line in summary))
        self.assertFalse(any("원문 제목과 본문" in line for line in summary))
        self.assertTrue(any("연방준비제도" in line or "금리" in line for line in summary))

    def test_make_three_line_summary_koreanizes_english_extractive_sentences(self):
        raw_text = """
        Sharp drops in Big Tech companies pulled indexes mostly lower on Wall Street.
        Investors weighed whether inflation data would change the Federal Reserve's interest-rate path.
        The market reaction showed how technology shares and monetary policy expectations remain linked.
        """

        summary = main.make_three_line_summary(
            "Sharp drops in Big Tech companies pull indexes mostly lower on Wall Street",
            raw_text,
            "AP News",
            "거시경제 미국 경제지표 주요 뉴스입니다.",
        )

        self.assertEqual(len(summary), 3)
        self.assertTrue(all(main.contains_hangul(line) for line in summary))
        self.assertFalse(any("원문 링크" in line for line in summary))
        self.assertTrue(any("빅테크" in line or "증시" in line or "월가" in line for line in summary))


if __name__ == "__main__":
    unittest.main()
