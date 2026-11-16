"""KIS 모의투자: 오늘(또는 지정한 날) 주문·체결 내역과 잔고를 KIS 서버에서 조회해 보여준다. 주문은 하지 않는다.

테스트가 정말 KIS 서버에서 일어났는지 직접 확인하는 용도. 키·토큰·계좌번호는 출력하지 않는다.

실행 (저장소 맨 위에서)
  python scripts/kis_paper_history.py              # 오늘
  python scripts/kis_paper_history.py 20261116     # 특정 날짜
"""

from __future__ import annotations

import sys
from datetime import datetime
from zoneinfo import ZoneInfo

KST = ZoneInfo("Asia/Seoul")
COLS = [("ord_tmd", "주문시각"), ("odno", "주문번호"), ("orgn_odno", "원주문번호"), ("prdt_name", "종목"),
        ("sll_buy_dvsn_cd_name", "매수/매도"), ("ord_dvsn_cd", "주문구분(00지정/01시장)"), ("ord_qty", "주문수량"),
        ("ord_unpr", "주문가"), ("tot_ccld_qty", "체결수량"), ("avg_prvs", "체결평균가"), ("cncl_yn", "취소여부"),
        ("rmn_qty", "잔여")]


def main() -> int:
    from dotenv import load_dotenv

    load_dotenv(".env")
    from lumibot.brokers.kis import KIS, PATH_CCLD, TR_CCLD
    from lumibot.brokers.kis_client import PAPER_URL, KisClient

    client = KisClient()
    if not (client.paper is True and client.base_url == PAPER_URL):
        print("[중단] 모의투자 설정이 아닙니다.")
        return 2
    day = sys.argv[1] if len(sys.argv) > 1 else datetime.now(KST).strftime("%Y%m%d")
    print(f"조회 시각: {datetime.now(KST):%Y-%m-%d %H:%M:%S} KST / 서버: {client.base_url} (모의투자)")
    print(f"조회 날짜: {day}\n")

    params = {**client.account_params(), "INQR_STRT_DT": day, "INQR_END_DT": day, "SLL_BUY_DVSN_CD": "00",
              "PDNO": "", "CCLD_DVSN": "00", "INQR_DVSN": "00", "INQR_DVSN_3": "00", "ORD_GNO_BRNO": "",
              "ODNO": "", "INQR_DVSN_1": "", "CTX_AREA_FK100": "", "CTX_AREA_NK100": ""}
    rows = []
    for page in client.request_all("GET", "/uapi/domestic-stock/v1/trading/inquire-daily-ccld", TR_CCLD, params):
        out = page.body.get("output1") or []
        rows.extend(out if isinstance(out, list) else [out])
    rows = [r for r in rows if r.get("odno")]
    rows.sort(key=lambda r: (r.get("ord_tmd", ""), r.get("odno", "")))

    print(f"## 주문·체결 내역 ({len(rows)}건, KIS 서버 응답 그대로)\n")
    print("| " + " | ".join(h for _, h in COLS) + " |")
    print("|" + "---|" * len(COLS))
    for r in rows:
        print("| " + " | ".join(str(r.get(k, "")) for k, _ in COLS) + " |")

    # 엑셀로 열 수 있는 CSV 도 저장 (utf-8-sig: 엑셀에서 한글이 안 깨지게)
    import csv
    from pathlib import Path

    out = Path("reports") / f"kis_paper_orders_{day}.csv"
    out.parent.mkdir(exist_ok=True)
    with open(out, "w", newline="", encoding="utf-8-sig") as f:
        w = csv.writer(f)
        w.writerow([h for _, h in COLS])
        w.writerows([[r.get(k, "") for k, _ in COLS] for r in rows])
    print(f"\nCSV 저장: {out}")

    broker = KIS(client=client, connect_stream=False)
    _, s = broker._balance()
    print("\n## 현재 잔고 (KIS 서버 응답)\n")
    for key, name in [("dnca_tot_amt", "예수금"), ("prvs_rcdl_excc_amt", "D+2 예수금"),
                      ("scts_evlu_amt", "주식 평가금액"), ("tot_evlu_amt", "총 평가금액")]:
        print(f"- {name}: {int(float(s.get(key) or 0)):,}원")
    return 0


if __name__ == "__main__":
    sys.exit(main())
