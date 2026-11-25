"""본 기사 기억(SQLite)과 CSV·HTML 내보내기 테스트 (네트워크 안 씀)"""

import csv
import threading
from datetime import datetime, timedelta, timezone

import pytest

from lumibot.components.news import NewsItem, NewsStore, export_csv, export_html

KST = timezone(timedelta(hours=9))
NOW = datetime(2026, 11, 25, 21, 0, tzinfo=KST)


def item(title="삼성전자 소식", n=1, hours_ago=1.0, query="삼성전자", **kw):
    base = dict(title=title, description="요약", link=f"https://n.news.naver.com/{n}",
                original_link=f"https://www.example.com/{n}", query=query,
                published_at=None if hours_ago is None else NOW - timedelta(hours=hours_ago))
    return NewsItem(**{**base, **kw})


def make(path=":memory:"):
    return NewsStore(path, now=lambda: NOW)


# ------------------------------------------------------------ 본 기사 기억
def test_only_new_items_come_back():
    s = make()
    assert len(s.add([item("가", 1), item("나", 2)])) == 2
    assert [n.title for n in s.add([item("가", 1), item("다", 3)])] == ["다"]
    assert s.count() == 3


def test_remembers_after_restart(tmp_path):
    path = tmp_path / "n.sqlite"
    s = make(path)
    s.add([item()])
    s.close()
    again = make(path)
    assert again.add([item()]) == [] and again.items() == [item()]
    again.close()


def test_same_title_from_other_press_is_one_article():
    assert [n.title for n in make().add([item("삼성전자, 상승", 1), item("삼성전자 상승!", 2)])] == ["삼성전자, 상승"]


def test_repeated_title_days_later_is_saved_again():
    s = make()
    s.add([item("[인사] 삼성전자", 1, hours_ago=24 * 10)])
    assert len(s.add([item("[인사] 삼성전자", 2)])) == 1


def test_same_article_for_two_companies_is_kept_for_each():
    s = make()
    assert len(s.add([item(), item(query="SK하이닉스")])) == 2
    assert s.count("삼성전자") == 1 and s.count("SK하이닉스") == 1


def test_items_order_hours_limit_and_missing_date():
    s = make()
    s.add([item("옛날", 1, 50), item("최근", 2, 1), item("날짜없음", 3, None)])
    assert [n.title for n in s.items()] == ["날짜없음", "최근", "옛날"]
    assert [n.title for n in s.items(hours=24)] == ["날짜없음", "최근"]
    assert [n.title for n in s.items("삼성전자", limit=1)] == ["날짜없음"]
    assert s.items("없는종목") == []


def test_failure_in_the_middle_saves_nothing():
    s = make()

    def broken():
        yield item("가", 1)
        raise RuntimeError("중간 실패")

    with pytest.raises(RuntimeError):
        s.add(broken())
    assert s.count() == 0 and len(s.add([item("가", 1)])) == 1


def test_quotes_in_title_are_safe():
    s = make()
    s.add([item("'); DROP TABLE news; --", 1)])
    assert s.count() == 1


def test_two_threads_do_not_save_twice():
    s, got = make(), []
    ts = [threading.Thread(target=lambda: got.append(len(s.add([item("가", 1), item("나", 2)])))) for _ in range(8)]
    [t.start() for t in ts]
    [t.join() for t in ts]
    assert sum(got) == 2 and s.count() == 2


# ------------------------------------------------------------ CSV
def test_csv_opens_in_excel_with_korean(tmp_path):
    path = export_csv([item("제목, \"따옴표\"", 1)], tmp_path / "a.csv")
    assert path.read_bytes().startswith(b"\xef\xbb\xbf")
    rows = list(csv.reader(open(path, encoding="utf-8-sig", newline="")))
    assert rows[0] == ["종목", "발행시각", "제목", "요약", "언론사", "링크"]
    assert rows[1] == ["삼성전자", "2026-11-25 20:00", "제목, \"따옴표\"", "요약", "example.com", "https://www.example.com/1"]


@pytest.mark.parametrize("bad", ["=1+1", "+1", "-1", "@cmd", " =1+1"])
def test_csv_does_not_run_formulas(tmp_path, bad):
    path = export_csv([item(bad)], tmp_path / "a.csv")
    assert list(csv.reader(open(path, encoding="utf-8-sig", newline="")))[1][2] == "'" + bad


# ------------------------------------------------------------ HTML
def test_html_groups_by_company(tmp_path):
    text = export_html([item("가", 1), item("나", 2), item("다", 3, query="대한전선")], tmp_path / "a.html",
                       made_at=NOW).read_text(encoding="utf-8")
    assert "2026-11-25 21:00 기준 · 종목 2개 · 기사 3건" in text
    assert "전체 3" in text and "삼성전자 2" in text and "대한전선 1" in text
    assert text.count("<section") == 2 and 'href="https://www.example.com/1"' in text


def test_html_escapes_dangerous_text_and_links(tmp_path):
    text = export_html([item("<script>alert(1)</script>", 1, query="<b>회사", description="\"><img src=x onerror=1>"),
                        item("링크", 2, query="둘째", original_link="javascript:alert(1)", link="data:text/html,x"),
                        item("따옴표", 3, original_link="https://a.com/\"onclick=\"x")],
                       tmp_path / "a.html", title="<i>제목").read_text(encoding="utf-8")
    assert text.count("<script>") == 1 and "<img" not in text and "<b>회사" not in text and "<i>제목" not in text
    assert "javascript:" not in text and "data:text" not in text and '"onclick=' not in text


def test_html_keeps_curly_braces(tmp_path):
    assert "{괄호} {0}" in export_html([item("{괄호} {0}")], tmp_path / "a.html").read_text(encoding="utf-8")


def test_empty_list_and_missing_date(tmp_path):
    assert "기사가 없습니다" in export_html([], tmp_path / "a.html").read_text(encoding="utf-8")
    assert "소식" in export_html([item("소식", hours_ago=None)], tmp_path / "b.html").read_text(encoding="utf-8")
    assert len(export_csv([], tmp_path / "c.csv").read_text(encoding="utf-8-sig").splitlines()) == 1


def test_default_folder_is_hidden_from_git(tmp_path):
    export_csv([item()], tmp_path / "news_data" / "a.csv")
    assert (tmp_path / "news_data" / ".gitignore").read_text() == "*\n"
    export_csv([item()], tmp_path / "other" / "a.csv")
    assert not (tmp_path / "other" / ".gitignore").exists()
