import unittest
from datetime import datetime
from unittest.mock import patch

import main


class DealsiteVcacTests(unittest.TestCase):
    def make_candidate(self, article_id, category, title, description=""):
        return {
            "title": title,
            "link": f"https://dealsite.co.kr/articles/{article_id}/075000",
            "date": datetime(2026, 6, 17, 9, 0),
            "description": description,
            "source": "딜사이트",
            "_dealsite_category": category,
            "_article_id": str(article_id),
        }

    def test_parse_category_api_html(self):
        html = """
        <div class="mnm-news">
          <a class="ss-news-top-title" href="/articles/12345/075033">
            <span>블라인드펀드 2000억 결성</span>
          </a>
          <a class="mnm-news-txt">기관투자가가 출자사업을 시작했다.</a>
          <div class="mnm-news-info"><span>딜사이트 기자</span><span>2026-06-17 08:30:00</span></div>
        </div>
        """

        items = main.parse_dealsite_category_items(html, "대체투자")

        self.assertEqual(len(items), 1)
        self.assertEqual(items[0]["_article_id"], "12345")
        self.assertEqual(items[0]["_dealsite_category"], "대체투자")
        self.assertEqual(items[0]["date"].strftime("%Y-%m-%d"), "2026-06-17")

    def test_balanced_selection_keeps_both_categories(self):
        candidates = {
            "대체투자": [
                self.make_candidate(1, "대체투자", "성장펀드 2000억 출자사업"),
                self.make_candidate(2, "대체투자", "벤처캐피탈 신규 투자유치"),
                self.make_candidate(3, "대체투자", "블라인드펀드 결성"),
            ],
            "인수합병": [
                self.make_candidate(4, "인수합병", "KDB생명 매각 예비입찰"),
                self.make_candidate(5, "인수합병", "경영권 인수 본입찰"),
            ],
        }

        selected = main.select_balanced_dealsite_candidates(candidates)

        self.assertEqual(len(selected), 3)
        self.assertEqual({item["_dealsite_category"] for item in selected}, {"대체투자", "인수합병"})

    def test_balanced_selection_avoids_cross_category_duplicate(self):
        candidates = {
            "대체투자": [self.make_candidate(10, "대체투자", "중앙그룹 투자금 회수")],
            "인수합병": [
                self.make_candidate(10, "인수합병", "중앙그룹 투자금 회수"),
                self.make_candidate(11, "인수합병", "보험사 매각 본입찰"),
            ],
        }

        selected = main.select_balanced_dealsite_candidates(candidates)

        self.assertEqual({item["_article_id"] for item in selected}, {"10", "11"})
        self.assertEqual({item["_dealsite_category"] for item in selected}, {"대체투자", "인수합병"})

    def test_scarce_category_gets_shared_article_first(self):
        candidates = {
            "대체투자": [
                self.make_candidate(20, "대체투자", "대형 PEF 투자금 회수"),
                self.make_candidate(21, "대체투자", "신규 벤처펀드 출자"),
            ],
            "인수합병": [self.make_candidate(20, "인수합병", "대형 PEF 투자금 회수")],
        }

        selected = main.select_balanced_dealsite_candidates(candidates)

        self.assertEqual({item["_article_id"] for item in selected}, {"20", "21"})
        self.assertEqual({item["_dealsite_category"] for item in selected}, {"대체투자", "인수합병"})

    def test_empty_day_does_not_fetch_older_articles(self):
        target_date = datetime(2026, 6, 17)
        with patch.object(main, "fetch_dealsite_category_html", return_value="") as fetch_html:
            items = main.fetch_dealsite_vcac_source(target_date, set(), [])

        self.assertEqual(items, [])
        self.assertEqual(fetch_html.call_count, 2)
        for call in fetch_html.call_args_list:
            self.assertEqual(call.args[1], target_date)
            self.assertEqual(call.args[2], target_date)


if __name__ == "__main__":
    unittest.main()
