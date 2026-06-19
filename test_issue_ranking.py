import unittest

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


if __name__ == "__main__":
    unittest.main()
