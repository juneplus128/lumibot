"""기사 목록을 CSV(엑셀·Numbers) 또는 HTML(브라우저) 파일로 내보낸다. 새 의존성 없음.

사용 예
    from lumibot.components.news import export_csv, export_html

    export_csv(items, "news_data/news.csv")
    export_html(items, "news_data/news.html")

파일에는 네이버 API 가 준 제목·요약·시각·링크만 들어간다 (본문 없음). 기사 저작권이 있으니 git 에 올리지 않는다.
"""

from __future__ import annotations

import csv
import html
from datetime import datetime
from pathlib import Path
from typing import Iterable
from urllib.parse import urlparse

from .naver_news import KST, NewsItem
from .news_store import prepare_dir

CSV_HEADER = ["종목", "발행시각", "제목", "요약", "언론사", "링크"]


def _link(item: NewsItem) -> str:
    """원문 주소 (없으면 네이버 주소). http 로 시작하지 않는 주소는 버린다 (javascript: 같은 위험한 링크 방지)."""
    for url in (item.original_link, item.link):
        if url.lower().startswith(("http://", "https://")):
            return url
    return ""


def _press(item: NewsItem) -> str:
    try:
        host = urlparse(_link(item)).hostname or ""
    except ValueError:
        return ""
    return host.removeprefix("www.")


def _safe_cell(text: str) -> str:
    """=, +, -, @ 로 시작하는 제목을 엑셀이 수식으로 실행하지 않게 앞에 ' 를 붙인다."""
    return "'" + text if text.lstrip()[:1] in ("=", "+", "-", "@") else text


def export_csv(items: Iterable[NewsItem], path: str | Path) -> Path:
    """CSV 로 저장한다. 엑셀에서 한글이 깨지지 않는 형식(utf-8-sig)."""
    path = Path(path)
    prepare_dir(path.parent)
    with open(path, "w", encoding="utf-8-sig", newline="") as f:
        writer = csv.writer(f)
        writer.writerow(CSV_HEADER)
        for n in items:
            when = f"{n.published_at:%Y-%m-%d %H:%M}" if n.published_at else ""
            writer.writerow([_safe_cell(v) for v in (n.query, when, n.title, n.description, _press(n), _link(n))])
    return path


_PAGE = """<!doctype html>
<html lang="ko"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width, initial-scale=1">
<title>{title}</title>
<style>
:root {{ --bg:#f5f6f8; --card:#fff; --text:#1c2330; --sub:#5f6875; --line:#e3e6ea; --link:#1a56b8; --on:#e4edfb; }}
@media (prefers-color-scheme: dark) {{
  :root {{ --bg:#121419; --card:#1b1e25; --text:#e6e8ec; --sub:#9aa3b0; --line:#2c313b; --link:#86b3f7; --on:#223350; }} }}
* {{ box-sizing:border-box; }}
body {{ margin:0; padding:24px 16px; background:var(--bg); color:var(--text); line-height:1.6;
       font-family:-apple-system,"Apple SD Gothic Neo","Malgun Gothic",sans-serif; }}
main {{ max-width:820px; margin:0 auto; background:var(--card); border:1px solid var(--line); border-radius:12px; padding:24px; }}
h1 {{ font-size:22px; margin:0; }} h2 {{ font-size:17px; margin:24px 0 4px; }}
.sub {{ color:var(--sub); font-size:13px; }}
nav {{ display:flex; flex-wrap:wrap; gap:8px; margin-top:16px; }}
button {{ font:inherit; font-size:14px; padding:4px 12px; border:1px solid var(--line); border-radius:8px;
         background:none; color:var(--text); cursor:pointer; }}
button.on {{ background:var(--on); color:var(--link); }}
article {{ border-top:1px solid var(--line); padding:12px 0; overflow-wrap:anywhere; }}
article a {{ color:var(--link); text-decoration:none; font-weight:600; }} article a:hover {{ text-decoration:underline; }}
article p {{ margin:2px 0; font-size:14px; }}
footer {{ border-top:1px solid var(--line); margin-top:8px; padding-top:12px; color:var(--sub); font-size:12px; }}
</style></head><body><main>
<h1>{title}</h1>
<div class="sub">{made:%Y-%m-%d %H:%M} 기준 · 종목 {n_query}개 · 기사 {n_item}건</div>
{nav}{sections}
<footer>제목과 요약은 네이버 검색 API 결과이며, 각 기사의 저작권은 해당 언론사에 있습니다.</footer>
</main>
<script>
var buttons = document.querySelectorAll("button[data-show]");
buttons.forEach(function (b) {{ b.addEventListener("click", function () {{
  buttons.forEach(function (x) {{ x.classList.toggle("on", x === b); }});
  document.querySelectorAll("section").forEach(function (s) {{
    s.hidden = b.dataset.show !== "all" && s.dataset.group !== b.dataset.show; }});
}}); }});
</script>
</body></html>
"""


def export_html(items: Iterable[NewsItem], path: str | Path, *, title: str = "종목 뉴스",
                made_at: datetime | None = None) -> Path:
    """브라우저로 여는 HTML 로 저장한다. 종목별로 묶고, 버튼으로 종목을 고를 수 있다."""
    items = list(items)
    groups: dict[str, list[NewsItem]] = {}
    for item in items:
        groups.setdefault(item.query, []).append(item)

    e = html.escape     # 제목에 <script> 같은 글자가 있어도 글자로만 보이게 한다
    nav, sections = "", ""
    if len(groups) > 1:
        nav = f'<button class="on" data-show="all">전체 {len(items)}</button>' + "".join(
            f'<button data-show="{i}">{e(name)} {len(rows)}</button>' for i, (name, rows) in enumerate(groups.items()))
        nav = f"<nav>{nav}</nav>"
    for i, (name, rows) in enumerate(groups.items()):
        cards = ""
        for n in rows:
            link, head = _link(n), e(n.title)
            if link:
                head = f'<a href="{e(link)}" target="_blank" rel="noopener noreferrer">{head}</a>'
            meta = " · ".join(v for v in (f"{n.published_at:%m-%d %H:%M}" if n.published_at else "", e(_press(n))) if v)
            cards += f'<article>{head}<p>{e(n.description)}</p><div class="sub">{meta}</div></article>\n'
        sections += f'<section data-group="{i}"><h2>{e(name)}</h2>\n{cards}</section>\n'

    path = Path(path)
    prepare_dir(path.parent)
    path.write_text(_PAGE.format(title=e(title), made=made_at or datetime.now(KST), n_query=len(groups),
                                 n_item=len(items), nav=nav, sections=sections or "<p>기사가 없습니다.</p>"),
                    encoding="utf-8")
    return path
