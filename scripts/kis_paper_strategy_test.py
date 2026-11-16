"""KIS 모의투자 전체 흐름 테스트: 진짜 Lumibot 전략을 Trader 로 실행한다.

사용자가 실제로 쓰는 방식 그대로 (self.buy 대신 self.create_order/submit_order, on_filled_order 콜백)
  1회차: 잔고·현재가·일봉 조회 → 1주 시장가 매수
  매수 체결 → on_filled_order → 다음 회차에 1주 시장가 매도
  매도 체결 → 끝 (자동으로 Trader 를 멈춘다)

안전장치: 모의투자 + 평일 09:05~15:15 KST 가 아니면 중단. 최대 5분 뒤에는 무조건 멈춘다.

실행 (저장소 맨 위에서): python scripts/kis_paper_strategy_test.py [종목코드]
"""

from __future__ import annotations

import os
import signal
import sys
import threading
import time
from datetime import datetime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from kis_paper_order_test import KST, log, market_open_now  # noqa: E402

CODE = sys.argv[1] if len(sys.argv) > 1 else "001440"
MAX_SECONDS = 300
CHECKS: dict[str, object] = {}
DONE = threading.Event()


def build_strategy(broker):
    from lumibot.entities import Asset
    from lumibot.strategies import Strategy

    class KisFlowTest(Strategy):
        def initialize(self):
            self.sleeptime = "10S"
            self.set_market("XKRX")
            self.vars.stage = "buy"
            self.asset = Asset(CODE, Asset.AssetType.STOCK)

        def on_trading_iteration(self):
            stage = self.vars.stage
            log(f"[전략] 회차 시작: 단계={stage}, 현금={self.get_cash():,.0f}, 통화={self.quote_asset.symbol}")
            if stage == "buy":
                price = self.get_last_price(self.asset)
                bars = self.get_historical_prices(self.asset, 3, "day")
                CHECKS["통화 KRW"] = self.quote_asset.symbol == "KRW"
                CHECKS["전략에서 현재가"] = bool(price)
                CHECKS["전략에서 일봉 3개"] = bars is not None and len(bars.df) == 3
                log(f"[전략] 현재가={price}, 일봉 마지막 날짜={bars.df.index[-1].date() if bars else None}")
                self.submit_order(self.create_order(self.asset, 1, "buy"))
                self.vars.stage = "wait_buy"
            elif stage == "bought":
                pos = self.get_position(self.asset)
                CHECKS["매수 후 보유 1주"] = pos is not None and float(pos.quantity) == 1
                log(f"[전략] 보유={pos.quantity if pos else None}주 → 1주 매도")
                self.submit_order(self.create_order(self.asset, 1, "sell"))
                self.vars.stage = "wait_sell"

        def on_new_order(self, order):
            log(f"[전략] on_new_order: {order.side} {order.quantity}주 (주문번호 {order.identifier})")

        def on_filled_order(self, position, order, price, quantity, multiplier):
            log(f"[전략] on_filled_order: {order.side} {quantity}주 @ {price:,.0f}원")
            if order.side == "buy":
                CHECKS["매수 체결 콜백"] = True
                self.vars.stage = "bought"
            else:
                CHECKS["매도 체결 콜백"] = True
                self.vars.stage = "done"
                DONE.set()

        def on_error_order(self, order, error=None):
            log(f"[전략] on_error_order: {error}")
            CHECKS["주문 에러 없음"] = False

    return KisFlowTest(broker=broker)


def main() -> int:
    from dotenv import load_dotenv

    load_dotenv(".env")
    from lumibot.brokers.kis import KIS
    from lumibot.brokers.kis_client import PAPER_URL, KisClient
    from lumibot.traders import Trader

    client = KisClient()
    if not (client.paper is True and client.base_url == PAPER_URL):
        log("[중단] 모의투자가 아닙니다.")
        return 2
    now = datetime.now(KST)
    if not market_open_now(now):
        log(f"[중단] 장 시간이 아닙니다 ({now:%a %H:%M} KST).")
        return 3
    log(f"[확인] 모의투자 서버, 장 시간 OK, 종목 {CODE}")

    broker = KIS(client=client)
    strategy = build_strategy(broker)

    def watchdog():
        finished = DONE.wait(MAX_SECONDS)
        if finished:
            time.sleep(5)   # 마지막 체결 처리 여유
        else:
            log(f"[감시] {MAX_SECONDS}초 안에 끝나지 않아 멈춥니다.")
        os.kill(os.getpid(), signal.SIGINT)   # Trader 의 정상 종료 경로(_stop_pool)를 탄다

    threading.Thread(target=watchdog, daemon=True).start()
    trader = Trader()
    trader.add_strategy(strategy)
    try:
        trader.run_all()
    except KeyboardInterrupt:
        pass

    left = [(p.asset.symbol, float(p.quantity)) for p in broker._pull_positions("check")]
    CHECKS["끝: 남은 주식 없음"] = not left
    CHECKS.setdefault("주문 에러 없음", True)
    for key in ["매수 체결 콜백", "매도 체결 콜백"]:
        CHECKS.setdefault(key, False)
    log("[결과]")
    for k, v in CHECKS.items():
        log(f"  {'통과' if v else '실패'}  {k}")
    if left:
        log(f"[주의] 남은 주식: {left}")
    return 0 if all(CHECKS.values()) else 1


if __name__ == "__main__":
    sys.exit(main())
