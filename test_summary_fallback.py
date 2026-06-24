import unittest

import main


class SummaryFallbackTests(unittest.TestCase):
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
