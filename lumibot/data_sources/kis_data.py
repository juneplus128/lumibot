"""KIS(한국투자증권) 시세: 현재가, 일봉.

example_broker_data.py(빈 틀)를 KIS API 로 채운 파일이다. 전략에서
self.get_last_price("005930"), self.get_historical_prices("005930", 20, "day") 를 부르면 여기로 온다.

지원 범위 (1차): 현재가, 일봉. 분봉은 KIS 가 당일 것만 줘서 아직 지원하지 않는다.
백테스트에는 쓰지 않는다 (백테스트는 Yahoo·Pandas 데이터 사용).
"""

from __future__ import annotations

from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

import pandas as pd

from lumibot.brokers.kis_client import KisClient, KisError
from lumibot.brokers.krx_rules import normalize_code
from lumibot.data_sources.data_source import DataSource
from lumibot.entities import Asset, Bars
from lumibot.tools.lumibot_logger import get_logger

logger = get_logger(__name__)

KST = ZoneInfo("Asia/Seoul")

PATH_PRICE = "/uapi/domestic-stock/v1/quotations/inquire-price"
PATH_DAILY = "/uapi/domestic-stock/v1/quotations/inquire-daily-itemchartprice"
TR_PRICE = "FHKST01010100"   # 주식현재가 시세 (실전·모의 공통)
TR_DAILY = "FHKST03010100"   # 기간별 시세 일/주/월/년 (한 번에 최대 100건)
MARKET_KRX = "J"


class KisData(DataSource):
    SOURCE = "KIS"
    MIN_TIMESTEP = "day"

    def __init__(self, client: KisClient | None = None, **kwargs):
        super().__init__(**kwargs)
        self.client = client or KisClient()

    def _price_row(self, asset) -> dict:
        symbol = asset.symbol if isinstance(asset, Asset) else asset
        res = self.client.request("GET", PATH_PRICE, TR_PRICE, {
            "FID_COND_MRKT_DIV_CODE": MARKET_KRX,
            "FID_INPUT_ISCD": normalize_code(symbol),
        })
        return res.body.get("output") or {}

    def get_last_price(self, asset, quote=None, exchange=None):
        """현재가 (원). 실패하면 None."""
        try:
            return float(self._price_row(asset).get("stck_prpr"))
        except (KisError, TypeError, ValueError) as e:
            logger.error(f"KIS 현재가 조회 실패 ({asset}): {e}")
            return None

    def get_price_limits(self, asset) -> tuple[float, float, int] | None:
        """(하한가, 상한가, 호가 단위). KIS 가 직접 알려주는 값이라 krx_rules 검증에도 쓴다."""
        try:
            row = self._price_row(asset)
            return float(row["stck_llam"]), float(row["stck_mxpr"]), int(float(row["aspr_unit"]))
        except (KisError, KeyError, TypeError, ValueError) as e:
            logger.error(f"KIS 상하한가 조회 실패 ({asset}): {e}")
            return None

    def get_historical_prices(self, asset, length, timestep="day", timeshift=None, quote=None, exchange=None,
                              include_after_hours=True):
        """일봉 length 개. 100일씩 끊어서 과거로 거슬러 올라가며 받는다."""
        if str(timestep or "day").lower() not in ("day", "1d", "d", "daily"):
            logger.error("KisData 는 아직 일봉(timestep='day')만 지원합니다.")
            return None
        if not isinstance(asset, Asset):
            asset = Asset(str(asset), Asset.AssetType.STOCK)
        code = normalize_code(asset.symbol)

        end = datetime.now(KST).date()
        if timeshift:
            end -= timeshift if isinstance(timeshift, timedelta) else timedelta(days=int(timeshift))

        rows: list[dict] = []
        for _ in range(30):                              # 최대 3,000일 정도
            start = end - timedelta(days=140)            # 영업일 100개 ≒ 달력 140일
            try:
                res = self.client.request("GET", PATH_DAILY, TR_DAILY, {
                    "FID_COND_MRKT_DIV_CODE": MARKET_KRX,
                    "FID_INPUT_ISCD": code,
                    "FID_INPUT_DATE_1": start.strftime("%Y%m%d"),
                    "FID_INPUT_DATE_2": end.strftime("%Y%m%d"),
                    "FID_PERIOD_DIV_CODE": "D",
                    "FID_ORG_ADJ_PRC": "0",              # 0 = 수정주가
                })
            except KisError as e:
                logger.error(f"KIS 일봉 조회 실패 ({code}): {e}")
                break
            batch = [r for r in (res.body.get("output2") or []) if r.get("stck_bsop_date")]
            if not batch:
                break
            rows.extend(batch)
            if len({r["stck_bsop_date"] for r in rows}) >= length:
                break
            oldest = min(r["stck_bsop_date"] for r in batch)
            end = datetime.strptime(oldest, "%Y%m%d").date() - timedelta(days=1)

        if not rows:
            return None
        # 일봉 시각은 장 마감(15:30 KST)으로 찍는다. Lumibot 이 시각을 미국 동부 시간으로 바꾸는데,
        # 한국 자정(00:00)으로 찍으면 미국 시간으로 전날이 되어 날짜가 하루 밀려 보이기 때문이다.
        df = pd.DataFrame({
            "datetime": [pd.Timestamp(datetime.strptime(r["stck_bsop_date"], "%Y%m%d").replace(hour=15, minute=30),
                                      tz=KST) for r in rows],
            "open": [float(r["stck_oprc"]) for r in rows],
            "high": [float(r["stck_hgpr"]) for r in rows],
            "low": [float(r["stck_lwpr"]) for r in rows],
            "close": [float(r["stck_clpr"]) for r in rows],
            "volume": [float(r["acml_vol"]) for r in rows],
        })
        df = df.drop_duplicates("datetime").set_index("datetime").sort_index().tail(length)
        return Bars(df, self.SOURCE, asset, raw=df, quote=quote)

    def get_chains(self, asset, quote=None):
        return {}   # 옵션용 기능이라 사용하지 않음
