"""Korea Investment & Securities data source for LumiBot.

This stage implements KIS paper-trading OAuth and a reusable REST request layer.
Market-data endpoint methods remain intentionally separate until their exact
request/response schemas are mapped and tested.
"""
from __future__ import annotations

import os
from datetime import datetime, timedelta, timezone
from decimal import Decimal, InvalidOperation
from importlib import import_module
from typing import TYPE_CHECKING, Any, Dict, Iterable, List, Optional, Union
from zoneinfo import ZoneInfo

if TYPE_CHECKING:
    import pandas as pd  # type: ignore[import-not-found]
else:
    pd = import_module("pandas")

from lumibot.data_sources import DataSource
from lumibot.entities import Asset, Bars, Quote
from lumibot.tools.lumibot_logger import get_logger

logger = get_logger(__name__)

KST = ZoneInfo("Asia/Seoul")
KIS_PAPER_BASE_URL = "https://openapivts.koreainvestment.com:29443"
KIS_LIVE_BASE_URL = "https://openapi.koreainvestment.com:9443"


class KoreaInvestmentAPIError(RuntimeError):
    """Raised when KIS returns an HTTP or application-level API error."""


class KoreaInvestmentData(DataSource):
    """KIS domestic-stock market data source (paper environment by default)."""

    MIN_TIMESTEP = "minute"
    SOURCE = "KOREAINVESTMENT"

    DEFAULT_OHLCV_FIELDS = {
        "datetime": "datetime",
        "open": "open",
        "high": "high",
        "low": "low",
        "close": "close",
        "volume": "volume",
    }

    def __init__(
        self,
        app_key: Optional[str] = None,
        app_secret: Optional[str] = None,
        base_url: Optional[str] = None,
        delay: Optional[float] = None,
        tzinfo=None,
        session=None,
        timeout: float = 10.0,
        **kwargs,
    ):
        super().__init__(delay=delay, tzinfo=tzinfo, **kwargs)

        # Explicit arguments take priority; otherwise read local environment.
        self.app_key = app_key or os.getenv("KIS_APP_KEY")
        self.app_secret = app_secret or os.getenv("KIS_APP_SECRET")

        # This project is being developed against KIS paper trading.
        self.base_url = (base_url or os.getenv("KIS_BASE_URL") or KIS_PAPER_BASE_URL).rstrip("/")
        self.tzinfo = tzinfo or KST
        self.timeout = timeout
        self.session = session

        self.access_token: Optional[str] = None
        self.token_expired_at: Optional[datetime] = None

        logger.info("KoreaInvestmentData initialized. base_url=%s", self.base_url)

    @staticmethod
    def _to_number(value: Any, field_name: str) -> float:
        if value is None or value == "":
            raise ValueError(f"Required numeric field '{field_name}' is empty.")
        try:
            if isinstance(value, str):
                value = value.replace(",", "").strip()
            return float(Decimal(str(value)))
        except (InvalidOperation, TypeError, ValueError) as exc:
            raise ValueError(
                f"Field '{field_name}' must be numeric; received {value!r}."
            ) from exc

    @staticmethod
    def _to_datetime(value: Any):
        if value is None or value == "":
            raise ValueError("Historical row is missing its datetime value.")
        timestamp = pd.Timestamp(value)
        if pd.isna(timestamp):
            raise ValueError(f"Invalid datetime value: {value!r}")
        if timestamp.tzinfo is None:
            timestamp = timestamp.tz_localize(KST)
        else:
            timestamp = timestamp.tz_convert(KST)
        return timestamp

    @classmethod
    def normalize_historical_rows(
        cls,
        rows: Iterable[Dict[str, Any]],
        field_map: Optional[Dict[str, str]] = None,
    ) -> pd.DataFrame:
        mapping = dict(cls.DEFAULT_OHLCV_FIELDS)
        if field_map:
            mapping.update(field_map)

        normalized: List[Dict[str, Any]] = []
        required = ("datetime", "open", "high", "low", "close", "volume")

        for row_number, row in enumerate(rows, start=1):
            if not isinstance(row, dict):
                raise TypeError(f"Row {row_number} must be a dictionary.")
            missing_keys = [mapping[name] for name in required if mapping[name] not in row]
            if missing_keys:
                raise ValueError(
                    f"Row {row_number} is missing required keys: {missing_keys}"
                )
            normalized.append(
                {
                    "datetime": cls._to_datetime(row[mapping["datetime"]]),
                    "open": cls._to_number(row[mapping["open"]], "open"),
                    "high": cls._to_number(row[mapping["high"]], "high"),
                    "low": cls._to_number(row[mapping["low"]], "low"),
                    "close": cls._to_number(row[mapping["close"]], "close"),
                    "volume": cls._to_number(row[mapping["volume"]], "volume"),
                }
            )

        if not normalized:
            raise ValueError("Historical response contains no price rows.")

        df = pd.DataFrame(normalized).sort_values("datetime")
        df = df.drop_duplicates(subset=["datetime"], keep="last")
        df = df.set_index("datetime")
        df.index.name = "datetime"

        invalid_ohlc = (
            (df["high"] < df[["open", "close", "low"]].max(axis=1))
            | (df["low"] > df[["open", "close", "high"]].min(axis=1))
            | (df["volume"] < 0)
        )
        if invalid_ohlc.any():
            bad_times = [str(x) for x in df.index[invalid_ohlc]]
            raise ValueError(f"Invalid OHLCV values at: {bad_times}")
        return df

    def build_bars(
        self,
        asset: Asset,
        rows: Iterable[Dict[str, Any]],
        field_map: Optional[Dict[str, str]] = None,
        quote: Optional[Asset] = None,
        raw: Any = None,
    ) -> Bars:
        df = self.normalize_historical_rows(rows, field_map=field_map)
        return Bars(
            df=df,
            source=self.SOURCE,
            asset=asset,
            quote=quote,
            raw=raw,
            tzinfo=self.tzinfo,
        )

    def build_quote(
        self,
        asset: Asset,
        quote_data: Dict[str, Any],
        field_map: Optional[Dict[str, str]] = None,
        raw_data: Any = None,
    ) -> Quote:
        if not isinstance(quote_data, dict):
            raise TypeError("quote_data must be a dictionary.")

        mapping = {
            "price": "price",
            "bid": "bid",
            "ask": "ask",
            "volume": "volume",
            "timestamp": "timestamp",
            "bid_size": "bid_size",
            "ask_size": "ask_size",
            "change": "change",
            "percent_change": "percent_change",
        }
        if field_map:
            mapping.update(field_map)

        def optional_number(name: str):
            value = quote_data.get(mapping[name])
            if value is None or value == "":
                return None
            return self._to_number(value, name)

        timestamp = None
        timestamp_value = quote_data.get(mapping["timestamp"])
        if timestamp_value not in (None, ""):
            timestamp = self._to_datetime(timestamp_value).to_pydatetime()

        price = optional_number("price")
        bid = optional_number("bid")
        ask = optional_number("ask")
        volume = optional_number("volume")
        if price is None and (bid is None or ask is None):
            raise ValueError("Quote needs a price, or both bid and ask values.")

        return Quote(
            asset=asset,
            price=price,
            bid=bid,
            ask=ask,
            volume=volume,
            timestamp=timestamp,
            bid_size=optional_number("bid_size"),
            ask_size=optional_number("ask_size"),
            change=optional_number("change"),
            percent_change=optional_number("percent_change"),
            raw_data=raw_data if raw_data is not None else quote_data,
        )

    @staticmethod
    def _parse_expiry(value: str) -> datetime:
        """Parse KIS access_token_token_expired (normally KST local timestamp)."""
        parsed = datetime.fromisoformat(value)
        if parsed.tzinfo is None:
            parsed = parsed.replace(tzinfo=KST)
        return parsed

    def _get_session(self):
        if self.session is None:
            import requests
            self.session = requests.Session()
        return self.session

    def _require_credentials(self):
        if not self.app_key or not self.app_secret:
            raise RuntimeError(
                "KIS credentials are missing. Set KIS_APP_KEY and KIS_APP_SECRET "
                "environment variables, or pass app_key/app_secret to "
                "KoreaInvestmentData."
            )

    def _get_access_token(self, force_refresh: bool = False) -> str:
        """Return a cached token or request a new KIS OAuth access token."""
        self._require_credentials()

        now = datetime.now(KST)
        if (
            not force_refresh
            and self.access_token
            and self.token_expired_at
            and now < self.token_expired_at - timedelta(minutes=2)
        ):
            return self.access_token

        url = f"{self.base_url}/oauth2/tokenP"
        payload = {
            "grant_type": "client_credentials",
            "appkey": self.app_key,
            "appsecret": self.app_secret,
        }
        response = self._get_session().post(
            url,
            json=payload,
            headers={"content-type": "application/json; charset=utf-8"},
            timeout=self.timeout,
        )
        response.raise_for_status()
        body = response.json()

        token = body.get("access_token")
        expires_at = body.get("access_token_token_expired")
        if not token or not expires_at:
            raise KoreaInvestmentAPIError(
                f"KIS token response is missing access_token or expiry: {body}"
            )

        self.access_token = token
        self.token_expired_at = self._parse_expiry(expires_at)
        logger.info("KIS access token acquired; expires at %s", self.token_expired_at)
        return token

    def _request(
        self,
        endpoint: str,
        tr_id: str,
        params: Optional[Dict[str, Any]] = None,
        method: str = "GET",
        json_body: Optional[Dict[str, Any]] = None,
        retry_auth: bool = True,
    ) -> Dict[str, Any]:
        """Send an authenticated KIS REST request and validate its response."""
        token = self._get_access_token()
        headers = {
            "content-type": "application/json; charset=utf-8",
            "authorization": f"Bearer {token}",
            "appkey": self.app_key,
            "appsecret": self.app_secret,
            "tr_id": tr_id,
            "custtype": "P",
        }
        url = f"{self.base_url}/{endpoint.lstrip('/')}"
        response = self._get_session().request(
            method.upper(),
            url,
            headers=headers,
            params=params,
            json=json_body,
            timeout=self.timeout,
        )
        response.raise_for_status()
        body = response.json()

        # KIS business-level errors may arrive with HTTP 200.
        if isinstance(body, dict) and body.get("rt_cd") not in (None, "0"):
            message = body.get("msg1", "Unknown KIS API error")
            code = body.get("msg_cd", "UNKNOWN")
            raise KoreaInvestmentAPIError(f"KIS API error {code}: {message}")

        return body

    # Endpoint-specific market data methods are added after request/response
    # schemas have been checked against the current KIS API reference.
    def get_last_price(self, asset, quote=None, exchange=None) -> Union[float, Decimal, None]:
        raise NotImplementedError("Current-price endpoint mapping is the next implementation step.")

    def get_historical_prices(
        self,
        asset,
        length,
        timestep="",
        timeshift=None,
        quote=None,
        exchange=None,
        include_after_hours=True,
        **kwargs,
    ) -> Bars:
        raise NotImplementedError("Historical-price endpoint mapping is the next implementation step.")

    def get_quote(self, asset: Asset, quote: Asset = None, exchange: str = None) -> Quote:
        raise NotImplementedError("Quote endpoint mapping is the next implementation step.")

    def get_chains(self, asset: Asset, quote: Asset = None) -> dict:
        raise NotImplementedError("Option chains are outside the initial domestic-stock data scope.")
