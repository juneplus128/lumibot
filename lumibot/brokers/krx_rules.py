"""국내 주식(한국거래소) 거래 규칙.

Lumibot 기본값은 미국 기준이라, KIS 로 주문하기 전에 여기서 한국 규칙에 맞춘다.
- 종목코드: "005930" 같은 6자리 문자열 (앞의 0 이 사라지면 안 됨)
- 수량: 1주 단위 정수만
- 가격: 가격대별 호가 단위에 맞아야 주문이 들어감

호가 단위 표는 2023년 1월 개편 기준(코스피·코스닥 공통)이다. 거래소가 바꿀 수 있으니
KIS 현재가 API 의 aspr_unit(호가 단위) 값과 비교해 확인한다.
"""

from __future__ import annotations

import math
from decimal import Decimal

# (이 가격 미만이면, 호가 단위)
TICK_TABLE: list[tuple[float, int]] = [
    (2_000, 1),
    (5_000, 5),
    (20_000, 10),
    (50_000, 50),
    (200_000, 100),
    (500_000, 500),
    (math.inf, 1_000),
]


def tick_size(price: float) -> int:
    """가격에 맞는 호가 단위 (예: 70,000원 → 100원)"""
    for limit, unit in TICK_TABLE:
        if price < limit:
            return unit
    return TICK_TABLE[-1][1]


def round_to_tick(price: float, mode: str = "down") -> int:
    """가격을 호가 단위에 맞춘다.

    mode="down": 내림 (매수 지정가 → 생각보다 비싸게 사지 않게)
    mode="up":   올림 (매도 지정가 → 생각보다 싸게 팔지 않게)
    """
    if price is None or price <= 0:
        raise ValueError("가격은 0보다 커야 합니다.")
    value = Decimal(str(price))
    unit = Decimal(tick_size(float(value)))
    steps = value / unit
    rounded = (steps.to_integral_value(rounding="ROUND_FLOOR") if mode == "down"
               else steps.to_integral_value(rounding="ROUND_CEILING")) * unit
    # 올림 때문에 다음 가격대로 넘어가면 그 가격대 단위로 다시 맞춘다 (예: 1,999.5 → 2,000)
    result = int(rounded)
    if result % tick_size(result) != 0:
        result = round_to_tick(result, mode)
    return result


def normalize_code(symbol) -> str:
    """종목코드를 KIS 형식으로: 5930 → "005930", "005930.KS" → "005930" """
    text = str(symbol).strip().upper()
    for suffix in (".KS", ".KQ", ".KRX"):
        if text.endswith(suffix):
            text = text[: -len(suffix)]
    if text.isdigit():
        text = text.zfill(6)
    return text


def to_order_qty(quantity) -> int:
    """주문 수량을 정수로. 소수(예: 1.5주)면 에러."""
    value = Decimal(str(quantity))
    if value <= 0:
        raise ValueError("주문 수량은 1주 이상이어야 합니다.")
    if value != value.to_integral_value():
        raise ValueError(f"국내 주식은 1주 단위로만 주문할 수 있습니다 (요청 수량: {quantity}).")
    return int(value)


def within_limits(price: float, lower: float, upper: float) -> bool:
    """상한가·하한가 안의 가격인지 (상하한가는 KIS 현재가 API 의 stck_mxpr / stck_llam)"""
    return lower <= price <= upper
