"""KIS 모의투자 주문 테스트 (진짜 KIS 모의서버 사용).

하는 일 (모두 1주씩)
  1. 하한가 지정가 매수 → 체결 안 된 상태 확인 → 취소 → 취소 확인
  2. 시장가 매수 → 체결 확인
  3. 시장가 매도 → 체결 확인 (2번에서 산 1주를 정리)

안전장치
  - 모의투자(KIS_IS_PAPER=true, 모의서버 주소)가 아니면 바로 중단한다.
  - 평일 09:05~15:15 (한국 시간)가 아니면 바로 중단한다.
  - 키·토큰·계좌번호는 출력하지 않는다.

실행 (저장소 맨 위에서)
  python scripts/kis_paper_order_test.py            # 기본 종목 005930
  python scripts/kis_paper_order_test.py 035720     # 다른 종목
"""

from __future__ import annotations

import sys
import time
from datetime import datetime, time as dtime
from zoneinfo import ZoneInfo

KST = ZoneInfo("Asia/Seoul")
# 취소가 체결 조회에 어떻게 찍히는지 확인하려고 출력하는 값 (계좌번호 같은 개인 정보는 뺀다)
ROW_KEYS = ["odno", "orgn_odno", "ord_dvsn_cd", "sll_buy_dvsn_cd", "ord_qty", "ord_unpr", "tot_ccld_qty",
            "avg_prvs", "rmn_qty", "cncl_yn", "cnc_cfrm_qty", "rjct_qty"]


def log(msg: str) -> None:
    print(f"{datetime.now(KST):%H:%M:%S} {msg}", flush=True)


def market_open_now(now: datetime) -> bool:
    return now.weekday() < 5 and dtime(9, 5) <= now.time() <= dtime(15, 15)


def wait_until(broker, order, done, label: str, timeout: float = 60, interval: float = 3) -> bool:
    """do_polling() 을 반복하면서 done(order) 가 참이 될 때까지 기다린다."""
    end = time.monotonic() + timeout
    while time.monotonic() < end:
        broker.do_polling()
        if done(order):
            log(f"  → {label}: 상태={order.status}, 평균가={order.avg_fill_price}")
            return True
        time.sleep(interval)
    log(f"  → {label}: {timeout:.0f}초 안에 확인 못 함 (현재 상태={order.status})")
    return False


def show_rows(broker, odno: str) -> None:
    """원주문과, 원주문을 가리키는 취소 줄을 함께 보여준다."""
    for row in broker._pull_broker_all_orders():
        if str(row.get("odno")) == odno or str(row.get("orgn_odno")) == odno:
            log("    체결조회 줄: " + ", ".join(f"{k}={row.get(k)}" for k in ROW_KEYS))


def run(broker, code: str, *, poll_interval: float = 3, timeout: float = 60) -> dict:
    from lumibot.entities import Asset, Order

    asset = Asset(code, Asset.AssetType.STOCK)
    results: dict[str, bool] = {}

    def order(side, limit=None):
        return Order(strategy="KisPaperTest", asset=asset, quantity=1, side=side, limit_price=limit,
                     order_type=Order.OrderType.LIMIT if limit else Order.OrderType.MARKET)

    limits = broker.data_source.get_price_limits(code)
    price = broker.data_source.get_last_price(code)
    log(f"[준비] {code} 현재가={price}, 하한가/상한가/호가단위={limits}")
    log(f"[준비] 잔고(현금, 주식, 총)={broker._get_balances_at_broker(None, None)}")
    if not limits or not price:
        log("[중단] 시세 조회 실패")
        return {"시세조회": False}

    # 1) 하한가 지정가 매수 → 취소
    log("[1] 하한가 지정가 매수 1주 (체결되지 않을 가격)")
    o1 = broker._submit_order(order("buy", limits[0]))
    if not o1.was_transmitted():
        log(f"  → 주문 실패: {o1.error_message if hasattr(o1, 'error_message') else o1.status}")
        results["지정가주문"] = False
    else:
        log(f"  → 주문번호 받음 (상태={o1.status})")
        broker.do_polling()
        results["지정가주문"] = o1.is_active() and not o1.is_filled()
        log(f"  → 미체결 상태 확인: {results['지정가주문']} (상태={o1.status})")
        log("[1] 취소 요청")
        broker.cancel_order(o1)
        results["취소"] = wait_until(broker, o1, lambda o: not o.is_active() and not o.is_filled(),
                                     "취소 확인", timeout, poll_interval)
        show_rows(broker, o1.identifier)

    # 2) 시장가 매수
    log("[2] 시장가 매수 1주")
    o2 = broker._submit_order(order("buy"))
    results["시장가매수"] = o2.was_transmitted() and wait_until(broker, o2, lambda o: o.is_filled(),
                                                              "매수 체결", timeout, poll_interval)
    if o2.was_transmitted():
        show_rows(broker, o2.identifier)

    # 3) 시장가 매도 (2번에서 산 1주 정리)
    if results["시장가매수"]:
        log("[3] 시장가 매도 1주 (정리)")
        o3 = broker._submit_order(order("sell"))
        results["시장가매도"] = o3.was_transmitted() and wait_until(broker, o3, lambda o: o.is_filled(),
                                                                  "매도 체결", timeout, poll_interval)
        if o3.was_transmitted():
            show_rows(broker, o3.identifier)

    log(f"[끝] 잔고(현금, 주식, 총)={broker._get_balances_at_broker(None, None)}")
    log(f"[끝] 보유={[(p.asset.symbol, float(p.quantity)) for p in broker._pull_positions('check')]}")
    return results


def main() -> int:
    from dotenv import load_dotenv

    load_dotenv(".env")
    from lumibot.brokers.kis import KIS
    from lumibot.brokers.kis_client import PAPER_URL, KisClient

    client = KisClient()
    if not (client.paper is True and client.base_url == PAPER_URL):
        log("[중단] 모의투자가 아닙니다. .env 의 KIS_IS_PAPER=true 를 확인하세요.")
        return 2
    now = datetime.now(KST)
    if not market_open_now(now):
        log(f"[중단] 지금은 장 시간이 아닙니다 ({now:%a %H:%M} KST). 평일 09:05~15:15 에 실행하세요.")
        return 3
    log("[확인] 모의투자 서버, 장 시간 OK")

    code = sys.argv[1] if len(sys.argv) > 1 else "005930"
    broker = KIS(client=client, connect_stream=False)
    results = run(broker, code)
    log("[결과] " + ", ".join(f"{k}={'통과' if v else '실패'}" for k, v in results.items()))
    return 0 if results and all(results.values()) else 1


if __name__ == "__main__":
    sys.exit(main())
