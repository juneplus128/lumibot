"""네이버 뉴스를 모아 SQLite 에 기억하고, CSV 와 HTML 파일로 내보낸다. 매매 판단은 하지 않는다.

실행 (저장소 맨 위에서)
  python scripts/naver_news_export.py 삼성전자 대한전선              # 최근 24시간
  python scripts/naver_news_export.py 삼성전자 --hours 72 --new-only  # 처음 보는 기사만 파일에 담기

결과는 news_data 폴더에 생긴다 (git 에 올라가지 않음).
"""

from __future__ import annotations

import argparse
import sys
from datetime import datetime
from pathlib import Path


def main(argv=None) -> int:
    p = argparse.ArgumentParser(description="네이버 뉴스를 모아 CSV·HTML 로 내보내기")
    p.add_argument("companies", nargs="+", help="회사 이름 (예: 삼성전자)")
    p.add_argument("--hours", type=float, default=24, help="최근 몇 시간 (기본 24)")
    p.add_argument("--limit", type=int, default=30, help="회사마다 최대 몇 건 (기본 30)")
    p.add_argument("--exclude", nargs="*", default=[], help="제목에 있으면 뺄 단어")
    p.add_argument("--new-only", action="store_true", help="처음 보는 기사만 파일에 담기")
    p.add_argument("--out", default="news_data", help="저장할 폴더 (기본 news_data)")
    args = p.parse_args(argv)

    from dotenv import load_dotenv

    load_dotenv(".env")
    from lumibot.components.news import NaverNewsCollector, NaverNewsError, NewsStore, export_csv, export_html
    from lumibot.components.news.naver_news import KST

    out, collector, store = Path(args.out), NaverNewsCollector(), NewsStore(Path(args.out) / "news.sqlite")
    picked, failed = [], 0
    for name in dict.fromkeys(args.companies):
        try:
            items = collector.recent(name, hours=args.hours, limit=args.limit, exclude=args.exclude)
        except NaverNewsError as exc:
            print(f"{name}: 수집 실패 - {exc}")
            failed += 1
            continue
        new = store.add(items)
        print(f"{name}: {len(items)}건 수집, 새 기사 {len(new)}건")
        picked += new if args.new_only else items
    store.close()
    if failed and not picked:
        return 1
    if not picked:
        print("내보낼 기사가 없어 파일을 만들지 않았습니다.")
        return 0

    now = datetime.now(KST)
    base = out / f"news_{now:%Y%m%d_%H%M%S}"
    print(f"CSV : {export_csv(picked, base.with_suffix('.csv'))}")
    print(f"HTML: {export_html(picked, base.with_suffix('.html'), made_at=now)}")
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
