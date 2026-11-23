"""네이버 뉴스 수집기.

네이버 검색 API(뉴스)로 종목 관련 기사의 제목·요약·링크·발행 시각을 모은다.
기사 본문은 네이버 API 가 주지 않으며, 크롤링하지 않는다 (저작권·이용약관).
호재·악재 판단은 하지 않는다. 판단은 이 수집기를 쓰는 쪽(전략, 사람)이 한다.

.env 에 넣는 값
    NAVER_CLIENT_ID        네이버 Client ID
    NAVER_CLIENT_SECRET    네이버 Client Secret
    NAVER_USE_API_HUB      true 면 NAVER API HUB(네이버 클라우드) 주소 사용 (기본 false = 개발자센터)

사용 예 (전략 안에서)
    from lumibot.components.news import NaverNewsCollector

    def initialize(self):
        self.news = NaverNewsCollector()

    def before_market_opens(self):
        for item in self.news.recent("삼성전자", hours=12, limit=20):
            self.log_message(f"{item.published_at:%H:%M} {item.title}")

주의: 네이버 API 는 날짜를 지정해 과거 뉴스를 뽑을 수 없어서 백테스트에는 쓸 수 없다 (실시간·모의투자 전용).
"""

from __future__ import annotations

import html
import os
import re
import threading
import time
from dataclasses import asdict, dataclass
from datetime import datetime, timedelta, timezone
from email.utils import parsedate_to_datetime
from typing import Any, Callable, Iterable

import requests

# [주소] 개발자센터(developers.naver.com) / NAVER API HUB(네이버 클라우드). 헤더 이름이 다르다.
CLASSIC_URL = "https://openapi.naver.com/v1/search/news.json"
HUB_URL = "https://naverapihub.apigw.ntruss.com/search/v1/news"

MAX_DISPLAY = 100   # 한 번에 받을 수 있는 최대 기사 수
MAX_START = 1000    # 시작 위치 최대값 (그래서 검색어 하나당 최대 1,000건)

KST = timezone(timedelta(hours=9))

# [에러 안내] 네이버 에러 코드 → 고칠 방법 (한국어)
ERROR_HINTS = {
    "024": "Client ID 또는 Client Secret 이 틀렸습니다. .env 의 NAVER_CLIENT_ID, NAVER_CLIENT_SECRET 을 확인하세요.",
    "010": "하루 호출 한도를 넘었습니다 (검색 API 전체 하루 25,000번). 내일 다시 시도하세요.",
    "SE01": "요청 형식이 잘못됐습니다. 검색어와 파라미터를 확인하세요.",
    "SE02": "display 값은 1~100 이어야 합니다.",
    "SE03": "start 값은 1~1000 이어야 합니다.",
    "SE04": "sort 값은 sim(정확도순) 또는 date(최신순) 이어야 합니다.",
    "SE05": "API 주소가 틀렸거나, 네이버 앱에 '검색' API 사용 권한이 없습니다. 개발자센터에서 앱의 사용 API 에 '검색'을 추가하세요.",
    "SE06": "검색어 인코딩이 잘못됐습니다 (UTF-8).",
    "SE99": "네이버 서버 오류입니다. 잠시 후 다시 시도하세요.",
}


class NaverNewsError(Exception):
    """네이버 API 실패. 코드와 한국어 안내를 담는다 (키 값은 담지 않는다)."""

    def __init__(self, code: str, message: str, status: int | None = None):
        self.code = code
        self.status = status
        hint = ERROR_HINTS.get(code, "")
        text = f"[네이버 {code}] {message}".strip()
        if hint:
            text += f" → {hint}"
        super().__init__(text)


@dataclass(frozen=True)
class NewsItem:
    """기사 한 건"""

    title: str                      # 제목 (태그 제거됨)
    description: str                # 요약 한두 줄 (태그 제거됨)
    link: str                       # 네이버 뉴스 주소 (없으면 원문 주소)
    original_link: str              # 언론사 원문 주소
    published_at: datetime | None   # 발행 시각 (한국 시간)
    query: str                      # 어떤 검색어로 찾았는지

    def to_dict(self) -> dict:
        data = asdict(self)
        data["published_at"] = self.published_at.isoformat() if self.published_at else None
        return data


# [글자 정리] 네이버는 제목·요약에 <b>검색어</b> 와 &quot; 같은 기호를 넣어서 준다.
_TAG = re.compile(r"<[^>]+>")
_SPACES = re.compile(r"\s+")


def clean_text(text: str | None) -> str:
    return _SPACES.sub(" ", html.unescape(_TAG.sub("", text or ""))).strip()


def parse_pub_date(text: str | None) -> datetime | None:
    """'Thu, 11 Jun 2026 18:34:00 +0900' → 한국 시간 datetime"""
    if not text:
        return None
    try:
        return parsedate_to_datetime(text).astimezone(KST)
    except (TypeError, ValueError):
        return None


def _title_key(title: str) -> str:
    """같은 기사가 여러 언론사에 실린 것을 찾기 위한 제목 비교용 값 (띄어쓰기·기호 무시)"""
    return re.sub(r"[\W_]+", "", title).lower()


def _env_flag(name: str) -> bool:
    return os.environ.get(name, "").strip().lower() in ("1", "true", "yes", "on")


class NaverNewsCollector:
    """네이버 뉴스 수집기. 테스트 때는 session 에 가짜 서버를 넣는다."""

    def __init__(
        self,
        *,
        client_id: str | None = None,
        client_secret: str | None = None,
        use_api_hub: bool | None = None,
        session: Any = None,
        timeout: float = 10,
        cache_seconds: float = 60,
        clock: Callable[[], float] = time.monotonic,
        now: Callable[[], datetime] | None = None,
    ):
        self.client_id = (client_id or os.environ.get("NAVER_CLIENT_ID", "")).strip()
        self.client_secret = (client_secret or os.environ.get("NAVER_CLIENT_SECRET", "")).strip()
        self.use_api_hub = _env_flag("NAVER_USE_API_HUB") if use_api_hub is None else bool(use_api_hub)
        self.http = session or requests
        self.timeout = timeout
        self.cache_seconds = cache_seconds      # 같은 요청은 이 시간 동안 다시 묻지 않는다 (호출 수 절약)
        self._clock = clock
        self._now = now or (lambda: datetime.now(KST))
        self._cache: dict[tuple, tuple[float, list[NewsItem]]] = {}
        self._lock = threading.Lock()

    def is_configured(self) -> bool:
        return bool(self.client_id and self.client_secret)

    # ------------------------------------------------------------------ 요청 1번
    def _headers(self) -> dict:
        if self.use_api_hub:
            return {"X-NCP-APIGW-API-KEY-ID": self.client_id, "X-NCP-APIGW-API-KEY": self.client_secret}
        return {"X-Naver-Client-Id": self.client_id, "X-Naver-Client-Secret": self.client_secret}

    def search(self, query: str, *, display: int = 10, start: int = 1, sort: str = "date") -> list[NewsItem]:
        """검색 1번. sort="date" 최신순, "sim" 정확도순."""
        query = (query or "").strip()
        if not query:
            raise ValueError("검색어가 비어 있습니다.")
        if not 1 <= display <= MAX_DISPLAY:
            raise ValueError("display 는 1~100 이어야 합니다.")
        if not 1 <= start <= MAX_START:
            raise ValueError("start 는 1~1000 이어야 합니다.")
        if sort not in ("date", "sim"):
            raise ValueError("sort 는 'date' 또는 'sim' 이어야 합니다.")
        if not self.is_configured():
            raise NaverNewsError("CONFIG", "NAVER_CLIENT_ID, NAVER_CLIENT_SECRET 을 .env 에 넣어 주세요.")

        key = (query, display, start, sort)
        with self._lock:
            hit = self._cache.get(key)
            if hit and self._clock() - hit[0] < self.cache_seconds:
                return list(hit[1])

        try:
            res = self.http.get(
                HUB_URL if self.use_api_hub else CLASSIC_URL,
                headers=self._headers(),
                params={"query": query, "display": display, "start": start, "sort": sort},
                timeout=self.timeout,
            )
        except requests.RequestException as exc:
            raise NaverNewsError("NETWORK", f"네이버에 연결하지 못했습니다 ({exc.__class__.__name__}).") from exc
        try:
            body = res.json()
        except ValueError:
            body = {}
        if res.status_code != 200:
            raise NaverNewsError(str(body.get("errorCode") or res.status_code),
                                 str(body.get("errorMessage") or "요청 실패"), status=res.status_code)

        items = [
            NewsItem(
                title=clean_text(row.get("title")),
                description=clean_text(row.get("description")),
                link=row.get("link") or row.get("originallink") or "",
                original_link=row.get("originallink") or row.get("link") or "",
                published_at=parse_pub_date(row.get("pubDate")),
                query=query,
            )
            for row in body.get("items") or []
        ]
        with self._lock:
            self._cache[key] = (self._clock(), items)
        return list(items)

    # ------------------------------------------------------------------ 최근 기사 모으기
    def recent(
        self,
        query: str,
        *,
        hours: float = 24,
        limit: int = 50,
        must_contain: bool = True,
        exclude: Iterable[str] = (),
    ) -> list[NewsItem]:
        """최근 hours 시간 안의 기사를 최신순으로 최대 limit 건.

        must_contain: 제목이나 요약에 검색어가 그대로 들어간 기사만 남긴다 (관련 없는 기사 줄이기)
        exclude:      이 단어가 제목에 있으면 뺀다 (예: "카카오" 검색에서 ["카카오뱅크", "카카오게임즈"])
        중복(같은 원문 주소, 또는 여러 언론사의 같은 제목)은 하나만 남긴다.
        """
        cutoff = self._now() - timedelta(hours=hours)
        excludes = [w for w in exclude if w]
        needle = (query or "").strip().lower()
        seen_links: set[str] = set()
        seen_titles: set[str] = set()
        result: list[NewsItem] = []

        start = 1
        while start <= MAX_START and len(result) < limit:
            batch = self.search(query, display=MAX_DISPLAY, start=start, sort="date")
            if not batch:
                break
            reached_old = False
            for item in batch:
                if item.published_at is not None and item.published_at < cutoff:
                    reached_old = True          # 최신순이라 이후 기사는 모두 더 오래됨
                    break
                text = f"{item.title} {item.description}".lower()
                if must_contain and needle not in text:
                    continue
                if any(word in item.title for word in excludes):
                    continue
                link_key = item.original_link or item.link
                title_key = _title_key(item.title)
                if link_key in seen_links or (title_key and title_key in seen_titles):
                    continue
                seen_links.add(link_key)
                seen_titles.add(title_key)
                result.append(item)
                if len(result) >= limit:
                    break
            if reached_old or len(batch) < MAX_DISPLAY:
                break
            start += MAX_DISPLAY
        return result

    def clear_cache(self) -> None:
        with self._lock:
            self._cache.clear()
