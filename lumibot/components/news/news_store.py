"""본 기사 기억하기 (SQLite).

한 번 저장한 기사를 파일에 기억해 두고, 다음에는 새 기사만 돌려준다. 프로그램을 껐다 켜도 기억한다.
SQLite 는 파이썬에 들어 있어 따로 설치하거나 서버를 켤 필요가 없다.

사용 예
    from lumibot.components.news import NaverNewsCollector, NewsStore

    store = NewsStore()                                             # news_data/news.sqlite 파일에 저장
    new_items = store.add(NaverNewsCollector().recent("삼성전자"))   # 처음 보는 기사만 돌아온다
"""

from __future__ import annotations

import sqlite3
import threading
from datetime import datetime, timedelta
from pathlib import Path
from typing import Callable, Iterable

from .naver_news import KST, NewsItem, _title_key

DEFAULT_DIR = Path("news_data")
SAME_TITLE_DAYS = 3     # 이 기간 안의 같은 제목은 같은 기사로 본다 ("[인사]"처럼 되풀이되는 제목은 나중에 다시 저장)

_SCHEMA = """
CREATE TABLE IF NOT EXISTS news (
    id            INTEGER PRIMARY KEY,
    query         TEXT NOT NULL,
    link_key      TEXT NOT NULL,
    title_key     TEXT NOT NULL,
    title         TEXT NOT NULL,
    description   TEXT NOT NULL,
    link          TEXT NOT NULL,
    original_link TEXT NOT NULL,
    published_at  TEXT,
    saved_at      TEXT NOT NULL,
    UNIQUE (query, link_key)
);
CREATE INDEX IF NOT EXISTS idx_news_title ON news (query, title_key);
CREATE INDEX IF NOT EXISTS idx_news_time ON news (query, published_at);
"""


def prepare_dir(folder: str | Path) -> Path:
    """폴더를 만든다. 기본 폴더(news_data)에는 git 에 올라가지 않게 .gitignore 도 넣는다 (기사 저작권)."""
    folder = Path(folder)
    folder.mkdir(parents=True, exist_ok=True)
    if folder.name == DEFAULT_DIR.name and not (folder / ".gitignore").exists():
        (folder / ".gitignore").write_text("*\n", encoding="utf-8")
    return folder


def _iso(value: datetime) -> str:
    """시간을 한국 시간 글자로. 자릿수를 맞춰 글자 순서 = 시간 순서가 되게 한다."""
    if value.tzinfo is None:
        value = value.replace(tzinfo=KST)
    return value.astimezone(KST).isoformat(timespec="seconds")


class NewsStore:
    """기사 저장소. 같은 종목의 같은 기사(같은 주소, 또는 며칠 안의 같은 제목)는 한 번만 저장한다."""

    def __init__(self, path: str | Path = DEFAULT_DIR / "news.sqlite", *, now: Callable[[], datetime] | None = None):
        if str(path) != ":memory:":
            prepare_dir(Path(path).parent)
        self._now = now or (lambda: datetime.now(KST))
        self._lock = threading.Lock()
        self._conn = sqlite3.connect(str(path), check_same_thread=False)
        self._conn.row_factory = sqlite3.Row
        with self._lock:
            self._conn.executescript(_SCHEMA)

    def add(self, items: Iterable[NewsItem]) -> list[NewsItem]:
        """기사를 저장하고, 처음 보는 기사만 돌려준다. 중간에 실패하면 하나도 저장하지 않는다."""
        new_items: list[NewsItem] = []
        saved_at = self._now()
        with self._lock, self._conn:
            for item in items:
                link_key = item.original_link or item.link
                title_key = _title_key(item.title)
                when = item.published_at or saved_at
                window = timedelta(days=SAME_TITLE_DAYS)
                same = self._conn.execute(
                    "SELECT 1 FROM news WHERE query = ? AND (link_key = ? OR (title_key = ? AND title_key != '' "
                    "AND COALESCE(published_at, saved_at) BETWEEN ? AND ?)) LIMIT 1",
                    (item.query, link_key, title_key, _iso(when - window), _iso(when + window)),
                ).fetchone()
                if same or not (link_key or title_key):
                    continue
                self._conn.execute(
                    "INSERT INTO news (query, link_key, title_key, title, description, link, original_link, "
                    "published_at, saved_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
                    (item.query, link_key or title_key, title_key, item.title, item.description, item.link,
                     item.original_link, _iso(item.published_at) if item.published_at else None, _iso(saved_at)),
                )
                new_items.append(item)
        return new_items

    def items(self, query: str | None = None, *, hours: float | None = None, limit: int | None = None) -> list[NewsItem]:
        """저장된 기사를 최신순으로. query 를 안 주면 모든 종목. 발행 시각이 없으면 저장한 시각으로 본다."""
        sql, args = "SELECT * FROM news WHERE 1 = 1", []
        if query is not None:
            sql += " AND query = ?"
            args.append(query)
        if hours is not None:
            sql += " AND COALESCE(published_at, saved_at) >= ?"
            args.append(_iso(self._now() - timedelta(hours=hours)))
        sql += " ORDER BY COALESCE(published_at, saved_at) DESC, id"
        if limit is not None:
            sql += " LIMIT ?"
            args.append(limit)
        with self._lock:
            rows = self._conn.execute(sql, args).fetchall()
        return [
            NewsItem(
                title=r["title"], description=r["description"], link=r["link"], original_link=r["original_link"],
                published_at=datetime.fromisoformat(r["published_at"]) if r["published_at"] else None,
                query=r["query"],
            )
            for r in rows
        ]

    def count(self, query: str | None = None) -> int:
        with self._lock:
            if query is None:
                return self._conn.execute("SELECT COUNT(*) FROM news").fetchone()[0]
            return self._conn.execute("SELECT COUNT(*) FROM news WHERE query = ?", (query,)).fetchone()[0]

    def close(self) -> None:
        with self._lock:
            self._conn.close()
