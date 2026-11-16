"""KIS 모의투자 여러 상황 테스트 (진짜 KIS 모의서버). 정상 주문과 일부러 실패시키는 주문을 섞어서 시험한다.

시나리오
  A. 호가 단위에 안 맞는 지정가 → 자동으로 맞춰서 접수되는지 (카카오) → 취소
  B. 바로 체결되는 지정가 매수 (현재가보다 높게)
  C. 체결 안 되는 지정가 매도 (상한가) → 취소
  D. 시장가 매도로 B 정리
  E. 주문 2건 동시에 내고 한 번에 둘 다 취소
  F. 비싼 종목(호가 1,000원 단위) 시장가 매수 → 매도
  G. 실패해야 정상인 주문 5가지 (없는 종목, 돈 부족, 상한가 초과, 없는 주식 매도, 소수 수량)

안전장치: 모의투자 + 평일 09:05~15:15 KST 가 아니면 중단. 수량은 G 의 '돈 부족'을 빼면 모두 1주.
키·토큰·계좌번호는 출력하지 않는다.

실행 (저장소 맨 위에서): python scripts/kis_paper_scenarios.py
"""

from __future__ import annotations

import sys
import time
from datetime import datetime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from kis_paper_order_test import KST, log, market_open_now, show_rows, wait_until  # noqa: E402

CHEAP = "001440"    # 대한전선 (1만원대, 호가 10원)
MID = "035720"      # 카카오 (3만원대, 호가 50원)
BIG = "000660"      # SK하이닉스 (100만원대, 호가 1,000원)
SAMSUNG = "005930"


def run(broker, *, poll_interval: float = 3, timeout: float = 60) -> dict:
    from lumibot.brokers.krx_rules import tick_size
    from lumibot.entities import Asset, Order

    ds = broker.data_source
    results: dict[str, bool] = {}

    def mk(code, side, qty=1, limit=None):
        return Order(strategy="KisPaperScenario", asset=Asset(code, Asset.AssetType.STOCK), quantity=qty,
                     side=side, limit_price=limit,
                     order_type=Order.OrderType.LIMIT if limit else Order.OrderType.MARKET)

    def canceled(o):
        return not o.is_active() and not o.is_filled() and o.status == Order.OrderStatus.CANCELED

    def rejected(o):
        # 거부된 주문도 Lumibot 임시 ID 가 붙어 있으므로 identifier 대신 상태와 KIS 응답(raw)으로 판단한다
        return o.status == Order.OrderStatus.ERROR and not o.was_transmitted()

    def pause():
        time.sleep(poll_interval / 3)

    # ------------------------------------------------------------ A
    log("[A] 호가 단위에 안 맞는 지정가 매수 (카카오, 하한가 + 23원)")
    low, high, unit = ds.get_price_limits(MID)
    want = low + 23
    o = broker._submit_order(mk(MID, "buy", limit=want))
    if o.was_transmitted():
        broker.do_polling()
        row = broker._pull_broker_order(o.identifier) or {}
        sent = int(float(row.get("ord_unpr") or 0))
        ok = sent % unit == 0 and sent <= want and o.is_active()
        log(f"  → 요청 {want:,.0f}원 → 접수 {sent:,}원 (호가 {unit}원, 우리표 {tick_size(sent)}원) {'OK' if ok else '이상'}")
        broker.cancel_order(o)
        ok = wait_until(broker, o, canceled, "취소 확인", timeout, poll_interval) and ok
    else:
        log(f"  → 접수 실패: {getattr(o, '_error_message', None) or o.status}")
        ok = False
    results["A 호가맞춤+취소"] = ok
    pause()

    # ------------------------------------------------------------ B
    price = ds.get_last_price(CHEAP)
    _, high_c, unit_c = ds.get_price_limits(CHEAP)
    limit = min(price + unit_c * 5, high_c)
    log(f"[B] 바로 체결되는 지정가 매수 ({CHEAP}, 현재가 {price:,.0f} → 지정가 {limit:,.0f})")
    ob = broker._submit_order(mk(CHEAP, "buy", limit=limit))
    results["B 지정가즉시체결"] = ob.was_transmitted() and wait_until(broker, ob, lambda x: x.is_filled(),
                                                                    "체결", timeout, poll_interval)
    if ob.was_transmitted() and ob.avg_fill_price:
        log(f"  → 체결가 {ob.avg_fill_price:,.0f}원 ≤ 지정가 {limit:,.0f}원: {ob.avg_fill_price <= limit}")
    pause()

    # ------------------------------------------------------------ C
    if results["B 지정가즉시체결"]:
        log(f"[C] 체결 안 되는 지정가 매도 ({CHEAP}, 상한가 {high_c:,.0f}원) → 취소")
        oc = broker._submit_order(mk(CHEAP, "sell", limit=high_c))
        ok = oc.was_transmitted()
        if ok:
            broker.do_polling()
            ok = oc.is_active() and not oc.is_filled()
            log(f"  → 미체결 대기 중: {ok}")
            broker.cancel_order(oc)
            ok = wait_until(broker, oc, canceled, "취소 확인", timeout, poll_interval) and ok
            show_rows(broker, oc.identifier)
        results["C 지정가매도+취소"] = ok
        pause()

        # -------------------------------------------------------- D
        log(f"[D] 시장가 매도로 정리 ({CHEAP})")
        od = broker._submit_order(mk(CHEAP, "sell"))
        results["D 시장가매도"] = od.was_transmitted() and wait_until(broker, od, lambda x: x.is_filled(),
                                                                   "체결", timeout, poll_interval)
        pause()

    # ------------------------------------------------------------ E
    log(f"[E] 주문 2건 동시 ({SAMSUNG}, {MID} 하한가 매수) → 둘 다 취소")
    pair = []
    for code in (SAMSUNG, MID):
        lo, _, _ = ds.get_price_limits(code)
        pair.append(broker._submit_order(mk(code, "buy", limit=lo)))
    if all(x.was_transmitted() for x in pair):
        for x in pair:
            broker.cancel_order(x)
        end = time.monotonic() + timeout
        while time.monotonic() < end and not all(canceled(x) for x in pair):
            broker.do_polling()
            time.sleep(poll_interval)
        ok = all(canceled(x) for x in pair)
        log(f"  → 둘 다 취소 확인: {ok} ({[str(x.status) for x in pair]})")
    else:
        ok = False
        log("  → 주문 접수 실패")
    results["E 동시2건취소"] = ok
    pause()

    # ------------------------------------------------------------ F
    log(f"[F] 비싼 종목 시장가 매수 → 매도 ({BIG}, 현재가 {ds.get_last_price(BIG):,.0f}원)")
    of = broker._submit_order(mk(BIG, "buy"))
    ok = of.was_transmitted() and wait_until(broker, of, lambda x: x.is_filled(), "매수 체결", timeout, poll_interval)
    if ok:
        pause()
        og = broker._submit_order(mk(BIG, "sell"))
        ok = og.was_transmitted() and wait_until(broker, og, lambda x: x.is_filled(), "매도 체결", timeout,
                                                poll_interval)
    results["F 고가주매수매도"] = ok
    pause()

    # ------------------------------------------------------------ G
    lo_big, _, _ = ds.get_price_limits(BIG)
    _, hi_s, _ = ds.get_price_limits(SAMSUNG)
    bad = [
        ("G1 없는종목", mk("999999", "buy")),
        ("G2 돈부족(50주)", mk(BIG, "buy", qty=50, limit=lo_big)),
        ("G3 상한가초과", mk(SAMSUNG, "buy", limit=hi_s + 10000)),
        ("G4 없는주식매도", mk(MID, "sell")),
        ("G5 소수수량", mk(SAMSUNG, "buy", qty=1.5)),
    ]
    for name, order in bad:
        log(f"[{name}] 실패해야 정상")
        out = broker._submit_order(order)
        if out.was_transmitted():   # 혹시 KIS 가 접수했으면 바로 취소해서 남기지 않는다
            log(f"  → 예상과 달리 접수됨 (주문번호 있음) → 취소 시도")
            try:
                broker.cancel_order(out)
                wait_until(broker, out, lambda x: not x.is_active(), "정리", timeout, poll_interval)
            except Exception as e:  # noqa: BLE001
                log(f"  → 취소 실패: {e}")
        results[name] = rejected(out)
        log(f"  → 거부됨: {results[name]} / 사유: {getattr(out, 'error_message', None)}")
        pause()

    # ------------------------------------------------------------ 마무리
    time.sleep(1)
    left = [(p.asset.symbol, float(p.quantity)) for p in broker._pull_positions("check")]
    results["끝: 남은주식없음"] = not left
    log(f"[끝] 보유={left}, 잔고(현금,주식,총)={broker._get_balances_at_broker(None, None)}")
    return results


def main() -> int:
    from dotenv import load_dotenv

    load_dotenv(".env")
    from lumibot.brokers.kis import KIS
    from lumibot.brokers.kis_client import PAPER_URL, KisClient

    client = KisClient()
    if not (client.paper is True and client.base_url == PAPER_URL):
        log("[중단] 모의투자가 아닙니다.")
        return 2
    now = datetime.now(KST)
    if not market_open_now(now):
        log(f"[중단] 장 시간이 아닙니다 ({now:%a %H:%M} KST).")
        return 3
    log("[확인] 모의투자 서버, 장 시간 OK")
    broker = KIS(client=client, connect_stream=False)
    results = run(broker)
    log("[결과]")
    for k, v in results.items():
        log(f"  {'통과' if v else '실패'}  {k}")
    return 0 if all(results.values()) else 1


if __name__ == "__main__":
    sys.exit(main())
