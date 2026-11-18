"""KIS(한국투자증권) 시세: 현재가, 호가, 일봉, 분봉.

example_broker_data.py(빈 틀)를 KIS API 로 채운 파일이다. 전략에서
self.get_last_price("005930"), self.get_historical_prices("005930", 20, "day") 를 부르면 여기로 온다.

지원 범위: 현재가, 상하한가, 호가(get_quote), 일봉, 분봉(오늘 정규장만, "5 minutes" 같은 N분봉 가능).
분봉은 팀원 김지용 님의 KIS 어댑터(kis_data._minute_frame)를 참고해 정규장 필터와 '채운 줄' 제거를 더했다.
백테스트에는 쓰지 않는다 (백테스트는 Yahoo·Pandas 데이터 사용).
"""

from __future__ import annotations

from datetime import datetime, time, timedelta
from zoneinfo import ZoneInfo

import pandas as pd

from lumibot.brokers.kis_client import KisClient, KisError
from lumibot.brokers.krx_rules import normalize_code
from lumibot.data_sources.data_source import DataSource
from lumibot.entities import Asset, Bars, Quote
from lumibot.tools.helpers import parse_timestep_qty_and_unit
from lumibot.tools.lumibot_logger import get_logger

logger = get_logger(__name__)

KST = ZoneInfo("Asia/Seoul")

PATH_PRICE = "/uapi/domestic-stock/v1/quotations/inquire-price"
PATH_DAILY = "/uapi/domestic-stock/v1/quotations/inquire-daily-itemchartprice"
PATH_ASKING = "/uapi/domestic-stock/v1/quotations/inquire-asking-price-exp-ccn"
TR_PRICE = "FHKST01010100"   # 주식현재가 시세 (실전·모의 공통)
TR_ASKING = "FHKST01010200"  # 주식현재가 호가/예상체결 (실전·모의 공통)
TR_DAILY = "FHKST03010100"   # 기간별 시세 일/주/월/년 (한 번에 최대 100건)
PATH_MINUTE = "/uapi/domestic-stock/v1/quotations/inquire-time-itemchartprice"
TR_MINUTE = "FHKST03010200"  # 주식 당일 분봉 (한 번에 30건, 오늘 것만)
MINUTE_MAX_PAGES = 15        # 30건 x 15 = 450분 (정규장 390분을 넘김)
MARKET_KRX = "J"
SESSION_OPEN, SESSION_CLOSE = time(9, 0), time(15, 30)   # 정규장


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

    def get_quote(self, asset, quote=None, exchange=None) -> Quote | None:
        """매수·매도 1호가와 잔량. 실패하면 None.

        KIS 호가 API(FHKST01010200)에는 누적 거래량이 없어서 volume 은 비워 둔다.
        등락(change)은 현재가 - 기준가(전일 종가)로 계산한다.
        (팀원 이도연 님의 korea_investment_data.get_quote 를 참고해 KisClient 기반으로 옮김)
        """
        if not isinstance(asset, Asset):
            asset = Asset(str(asset), Asset.AssetType.STOCK)
        try:
            res = self.client.request("GET", PATH_ASKING, TR_ASKING, {
                "FID_COND_MRKT_DIV_CODE": MARKET_KRX,
                "FID_INPUT_ISCD": normalize_code(asset.symbol),
            })
        except KisError as e:
            logger.error(f"KIS 호가 조회 실패 ({asset}): {e}")
            return None
        book = res.body.get("output1") or {}
        info = res.body.get("output2") or {}

        def num(value):
            try:
                return float(str(value).replace(",", ""))
            except (TypeError, ValueError):
                return None

        price, base = num(info.get("stck_prpr")), num(info.get("stck_sdpr"))
        change = price - base if price is not None and base else None
        stamp = None
        hhmmss = str(book.get("aspr_acpt_hour") or "")
        if len(hhmmss) == 6 and hhmmss.isdigit():
            stamp = datetime.now(KST).replace(hour=int(hhmmss[:2]), minute=int(hhmmss[2:4]),
                                              second=int(hhmmss[4:]), microsecond=0)
        return Quote(
            asset=asset,
            price=price,
            bid=num(book.get("bidp1")),
            ask=num(book.get("askp1")),
            bid_size=num(book.get("bidp_rsqn1")),
            ask_size=num(book.get("askp_rsqn1")),
            change=change,
            percent_change=round(change / base * 100, 2) if change is not None else None,
            timestamp=stamp,
            quote_time=stamp,
            raw_data=res.body,
        )

    def get_historical_prices(self, asset, length, timestep="day", timeshift=None, quote=None, exchange=None,
                              include_after_hours=True):
        """과거 시세 length 개.

        - timestep="day"                 : 일봉. 100일씩 끊어서 과거로 거슬러 올라가며 받는다.
        - timestep="minute", "5 minutes" : 오늘 정규장(09:00~15:30) 분봉. N분봉은 1분봉을 묶어서 만든다.
        """
        if not isinstance(asset, Asset):
            asset = Asset(str(asset), Asset.AssetType.STOCK)
        code = normalize_code(asset.symbol)
        try:
            qty, unit = parse_timestep_qty_and_unit(timestep or "day")
        except Exception:  # noqa: BLE001 - 해석할 수 없는 값
            qty, unit = 0, str(timestep)
        if unit == "minute" and qty >= 1:
            return self._minute_bars(asset, code, int(length), int(qty), timeshift, quote)
        if unit != "day" or qty != 1:
            logger.error(f"KisData 는 일봉('day')과 분봉('minute', '5 minutes' 등)만 지원합니다 (요청: {timestep}).")
            return None

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

    # ------------------------------------------------------------------ 분봉
    def _minute_rows(self, code: str, need: int, end: datetime) -> list[dict]:
        """오늘 정규장 1분봉을 end 시각부터 거꾸로 need 개까지 모은다 (API 1회 30개).

        장 마감 뒤에 부르면 15:30 부터 거꾸로 받는다. 그냥 지금 시각으로 부르면 시간외 분봉이 섞여 온다.
        """
        if end.time() < SESSION_OPEN:
            return []                                    # 오늘 정규장이 아직 시작 안 함
        until = min(end.time(), SESSION_CLOSE).strftime("%H%M%S")
        rows: dict[tuple, dict] = {}
        for _ in range(MINUTE_MAX_PAGES):
            try:
                res = self.client.request("GET", PATH_MINUTE, TR_MINUTE, {
                    "FID_ETC_CLS_CODE": "",
                    "FID_COND_MRKT_DIV_CODE": MARKET_KRX,
                    "FID_INPUT_ISCD": code,
                    "FID_INPUT_HOUR_1": until,
                    "FID_PW_DATA_INCU_YN": "Y",          # 과거 데이터 포함
                })
            except KisError as e:
                logger.error(f"KIS 분봉 조회 실패 ({code}): {e}")
                break
            page = [r for r in (res.body.get("output2") or []) if r.get("stck_cntg_hour")]
            if not page:
                break
            for r in page:
                rows[(r.get("stck_bsop_date") or end.strftime("%Y%m%d"), r["stck_cntg_hour"])] = r
            oldest = min(r["stck_cntg_hour"] for r in page)
            if len(rows) >= need + 30 or oldest <= "090000" or oldest >= until:   # +30: 걸러질 줄 여유
                break
            until = (datetime.strptime(oldest, "%H%M%S") - timedelta(minutes=1)).strftime("%H%M%S")
        return [rows[k] for k in sorted(rows)]

    def _minute_bars(self, asset, code, length, qty, timeshift, quote):
        end = datetime.now(KST)
        if timeshift:
            end -= timeshift if isinstance(timeshift, timedelta) else timedelta(minutes=int(timeshift))
        raw = self._minute_rows(code, length * qty, end)
        keep = []
        for r in raw:
            t = datetime.strptime(r["stck_cntg_hour"], "%H%M%S").time()
            if not (SESSION_OPEN <= t <= SESSION_CLOSE):
                continue                                 # 정규장 밖(시간외) 분봉 제외
            if float(r.get("cntg_vol") or 0) <= 0:
                # 거래가 없던 분은 KIS 가 직전 가격으로 채워서 보낸다. 채운 값은 진짜 시세가 아니므로 버린다
                # (Lumibot RULE #1: 없는 데이터를 만들어 넣지 않는다).
                continue
            keep.append(r)
        if not keep:
            logger.warning(f"KIS 에서 {code} 의 오늘 정규장 분봉을 찾지 못했습니다.")
            return None
        df = pd.DataFrame({
            "datetime": [pd.Timestamp(datetime.strptime(r["stck_bsop_date"] + r["stck_cntg_hour"], "%Y%m%d%H%M%S"),
                                      tz=KST) for r in keep],
            "open": [float(r["stck_oprc"]) for r in keep],
            "high": [float(r["stck_hgpr"]) for r in keep],
            "low": [float(r["stck_lwpr"]) for r in keep],
            "close": [float(r["stck_prpr"]) for r in keep],
            "volume": [float(r["cntg_vol"]) for r in keep],
        }).drop_duplicates("datetime").set_index("datetime").sort_index()
        if qty > 1:   # 1분봉을 N분봉으로 묶기 (09:00 기준으로 구간을 나눔)
            df = df.resample(f"{qty}min", label="left", closed="left").agg(
                {"open": "first", "high": "max", "low": "min", "close": "last", "volume": "sum"}
            ).dropna(subset=["close"])
        return Bars(df.tail(length), self.SOURCE, asset, raw=df, quote=quote)

    def get_chains(self, asset, quote=None):
        return {}   # 옵션용 기능이라 사용하지 않음
