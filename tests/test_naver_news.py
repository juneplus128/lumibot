"""네이버 뉴스 수집기 테스트 (진짜 네이버에 연결하지 않음, 가짜 서버 사용)"""

from datetime import datetime, timedelta, timezone

import pytest

from lumibot.components.news.naver_news import (
    CLASSIC_URL,
    HUB_URL,
    NaverNewsCollector,
    NaverNewsError,
    clean_text,
    parse_pub_date,
)

KST = timezone(timedelta(hours=9))
NOW = datetime(2026, 11, 23, 12, 0, tzinfo=KST)


def pub(hours_ago):
    return (NOW - timedelta(hours=hours_ago)).strftime("%a, %d %b %Y %H:%M:%S +0900")


def row(title, hours_ago, link=None, desc="요약"):
    link = link or f"https://news.example.com/{abs(hash((title, hours_ago)))}"
    return {"title": title, "description": desc, "originallink": link,
            "link": link.replace("example", "naver"), "pubDate": pub(hours_ago)}


class FakeResponse:
    def __init__(self, status, body):
        self.status_code = status
        self._body = body

    def json(self):
        return self._body


class FakeSession:
    """start 값에 따라 정해진 페이지를 돌려주는 가짜 네이버"""

    def __init__(self, pages=None, status=200, error=None):
        self.pages = pages or {}
        self.status = status
        self.error = error
        self.calls = []

    def get(self, url, headers=None, params=None, timeout=None):
        self.calls.append({"url": url, "headers": headers, "params": params})
        if self.status != 200:
            return FakeResponse(self.status, self.error or {})
        return FakeResponse(200, {"items": self.pages.get(params["start"], [])})


def make(session, **kw):
    return NaverNewsCollector(client_id="id", client_secret="secret", session=session,
                              now=lambda: NOW, **kw)


def test_clean_text_removes_tags_and_entities():
    assert clean_text("<b>삼성전자</b> &quot;신고가&quot;  돌파") == '삼성전자 "신고가" 돌파'


def test_parse_pub_date_to_kst():
    d = parse_pub_date("Thu, 11 Jun 2026 09:34:00 +0000")
    assert d.utcoffset() == timedelta(hours=9) and d.hour == 18
    assert parse_pub_date("엉터리") is None


def test_search_classic_headers_and_params():
    s = FakeSession({1: [row("<b>삼성전자</b> 상승", 1)]})
    items = make(s).search("삼성전자", display=5)
    call = s.calls[0]
    assert call["url"] == CLASSIC_URL
    assert call["headers"] == {"X-Naver-Client-Id": "id", "X-Naver-Client-Secret": "secret"}
    assert call["params"] == {"query": "삼성전자", "display": 5, "start": 1, "sort": "date"}
    assert items[0].title == "삼성전자 상승"
    assert items[0].published_at == NOW - timedelta(hours=1)


def test_search_api_hub_headers():
    s = FakeSession({1: []})
    make(s, use_api_hub=True).search("삼성전자")
    assert s.calls[0]["url"] == HUB_URL
    assert set(s.calls[0]["headers"]) == {"X-NCP-APIGW-API-KEY-ID", "X-NCP-APIGW-API-KEY"}


def test_cache_avoids_second_call():
    s = FakeSession({1: [row("삼성전자 a", 1)]})
    c = make(s)
    c.search("삼성전자")
    c.search("삼성전자")
    assert len(s.calls) == 1


def test_recent_filters_old_dupes_unrelated_and_excluded():
    page = [
        row("삼성전자 실적 발표", 1, link="https://a.com/1"),
        row("삼성전자 실적 발표", 1, link="https://b.com/2"),      # 다른 언론사 같은 제목 → 중복
        row("삼성전자 신고가", 2, link="https://a.com/1"),         # 같은 원문 주소 → 중복
        row("반도체 업황 전망", 3, desc="메모리 가격"),              # 검색어 없음 → 제외
        row("삼성전자우 급등", 4),                                  # 제외 단어
        row("삼성전자 배당", 5),
        row("삼성전자 옛날 기사", 30),                              # 24시간 지남 → 여기서 멈춤
        row("삼성전자 더 옛날", 40),
    ]
    s = FakeSession({1: page})
    items = make(s).recent("삼성전자", hours=24, exclude=["삼성전자우"])
    assert [i.title for i in items] == ["삼성전자 실적 발표", "삼성전자 배당"]
    assert len(s.calls) == 1


def test_recent_pages_until_limit():
    page1 = [row(f"삼성전자 뉴스 {i}", 1, link=f"https://a.com/{i}") for i in range(100)]
    page2 = [row(f"삼성전자 뉴스 {i}", 2, link=f"https://a.com/{i}") for i in range(100, 200)]
    s = FakeSession({1: page1, 101: page2})
    items = make(s).recent("삼성전자", limit=150)
    assert len(items) == 150
    assert [c["params"]["start"] for c in s.calls] == [1, 101]


def test_auth_error_has_korean_hint_and_no_secret():
    s = FakeSession(status=401, error={"errorCode": "024", "errorMessage": "Authentication failed"})
    with pytest.raises(NaverNewsError) as e:
        make(s).search("삼성전자")
    assert e.value.code == "024" and "NAVER_CLIENT_ID" in str(e.value)
    assert "secret" not in str(e.value)


def test_quota_error():
    s = FakeSession(status=429, error={"errorCode": "010", "errorMessage": "limit"})
    with pytest.raises(NaverNewsError) as e:
        make(s).search("삼성전자")
    assert "한도" in str(e.value)


def test_missing_keys():
    c = NaverNewsCollector(client_id="", client_secret="", session=FakeSession())
    with pytest.raises(NaverNewsError):
        c.search("삼성전자")


@pytest.mark.parametrize("kw", [{"display": 0}, {"display": 101}, {"start": 1001}, {"sort": "new"}])
def test_bad_params(kw):
    with pytest.raises(ValueError):
        make(FakeSession()).search("삼성전자", **kw)
