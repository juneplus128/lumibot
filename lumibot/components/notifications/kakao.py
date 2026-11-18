"""KakaoTalk notifications for LumiBot.

Two parts live here:

* ``KakaoNotificationProvider`` - a provider with the same shape as the Telegram/Slack
  providers. It sends a text message to the user's own KakaoTalk ("나와의 채팅") through the
  Kakao "send to me" API and refreshes the OAuth access token automatically when it expires.
* ``KakaoTradeAlerts`` - automatic trade alerts. When ``KAKAO_TRADE_ALERTS=true`` is set, the
  strategy executor calls it on every fill, partial fill and cancel, and after the market closes
  (daily summary). Strategies do not need any extra code.

Environment variables (put them in ``.env``; never commit real values):

    KAKAO_REST_API_KEY        REST API key of your Kakao Developers app
    KAKAO_CLIENT_SECRET       Client secret (only if it is enabled for the app)
    KAKAO_TOKEN_FILE          Where tokens are stored (default: kakao_token.json)
    KAKAO_ACCESS_TOKEN        Optional initial access token if the token file does not exist yet
    KAKAO_REFRESH_TOKEN       Optional initial refresh token if the token file does not exist yet

    KAKAO_TRADE_ALERTS        true -> send fill/cancel alerts automatically (default: false)
    KAKAO_DAILY_SUMMARY       true -> send a summary after the market closes (default: true)
    KAKAO_ALERT_IN_BACKTEST   true -> also send during backtests (default: false)
    KAKAO_BACKTEST_MAX_ALERTS Max messages per backtest run when enabled (default: 20)

Alert failures are logged and never interrupt trading.
"""

from __future__ import annotations

import json
import os
import re
import threading
from datetime import datetime
from pathlib import Path
from typing import Any

import requests

from .base import NotificationResult

# [상수] 카카오 API 주소와 메시지 길이 제한. 여러 곳에서 쓰므로 한곳에 모아 둔다.
TOKEN_URL = "https://kauth.kakao.com/oauth/token"
SEND_URL = "https://kapi.kakao.com/v2/api/talk/memo/default/send"

# Kakao text templates accept at most 200 characters.
MAX_TEXT_LENGTH = 200

# [환경변수 도우미] .env 값은 항상 글자라서 참/거짓·숫자로 바꿔 주는 함수들.
_TRUE_VALUES = {"1", "true", "yes", "on"}
_BUY_SIDES = {"buy", "buy_to_cover", "buy_to_open", "buy_to_close"}


def _env_flag(name: str, default: bool = False) -> bool:
    value = os.environ.get(name)
    if value is None or value.strip() == "":
        return default
    return value.strip().lower() in _TRUE_VALUES


def _env_int(name: str, default: int) -> int:
    try:
        return int(os.environ.get(name, "").strip())
    except ValueError:
        return default


# [키 검사] .env 에 넣은 키가 이상하면 고칠 방법을 한국어로 돌려준다. 괜찮으면 None.
# check_key: 한글·띄어쓰기가 있으면 설명서 예시 글자를 그대로 둔 것이라 카카오가 무조건 거절한다 (막아야 함).
# key_format_warning: REST API 키는 보통 영문·숫자 32자라서, 다르면 다른 종류의 키를 넣었을 수 있다 (경고만).
def check_key(name: str, value: str | None) -> str | None:
    value = (value or "").strip()
    if value and any(ord(ch) > 127 or ch.isspace() for ch in value):
        return (
            f"{name} 에 한글이나 띄어쓰기가 들어 있습니다. 설명서의 예시 글자가 그대로 남은 것 같아요. "
            "카카오 콘솔 [앱] → [플랫폼 키] → [REST API 키] 에서 진짜 값을 복사해 넣으세요."
        )
    return None


def key_format_warning(rest_api_key: str | None) -> str | None:
    value = (rest_api_key or "").strip()
    if value and not re.fullmatch(r"[0-9a-fA-F]{32}", value):
        return (
            f"REST API 키는 보통 영문·숫자 32자인데 지금은 {len(value)}자입니다. "
            "JavaScript 키나 어드민 키가 아니라 'REST API 키'를 넣었는지 확인하세요."
        )
    return None


# [메시지 문구 만들기] 카카오와 상관없이 글자만 다루는 함수들 (인터넷 없이 테스트할 수 있게 분리).
# build_text: 제목과 본문을 합치고 카카오 제한(200자)에 맞춰 자른다.
def build_text(title: str, message: str, max_length: int = MAX_TEXT_LENGTH) -> str:
    """Join title and message and cut to Kakao's text limit."""
    title = (title or "").strip()
    message = (message or "").strip()
    text = f"{title}\n{message}" if title and message else (title or message)
    if len(text) > max_length:
        text = text[: max_length - 1] + "…"
    return text


# format_money: 통화에 맞게 돈을 표시한다. KRW → 70,000원, USD → $123.40
def format_money(value: float | None, currency: str | None = None) -> str:
    """70000 -> '70,000원' for KRW, 123.4 -> '$123.40' for USD, otherwise '123.40 EUR'."""
    if value is None:
        return "-"
    currency = (currency or "USD").upper()
    if currency == "KRW":
        return f"{float(value):,.0f}원"
    if currency == "USD":
        return f"${float(value):,.2f}"
    return f"{float(value):,.2f} {currency}"


# format_quantity: 10.0 → 10 처럼 수량을 깔끔하게 표시한다.
def format_quantity(quantity: Any) -> str:
    try:
        return f"{float(quantity):g}"
    except (TypeError, ValueError):
        return str(quantity)


# format_fill_message: 체결 알림의 (제목, 본문)을 만든다. 예) [체결] 005930 10주 매수
def format_fill_message(
    *,
    symbol: str,
    is_buy: bool,
    quantity: Any,
    price: float | None,
    holding: Any = None,
    when: datetime | None = None,
    currency: str | None = None,
    partial: bool = False,
) -> tuple[str, str]:
    """Build the (title, message) pair for a fill alert."""
    side = "매수" if is_buy else "매도"
    kind = "부분 체결" if partial else "체결"
    title = f"[{kind}] {symbol} {format_quantity(quantity)}주 {side}"
    lines = [f"가격: {format_money(price, currency)}"]
    if price is not None:
        try:
            lines.append(f"금액: {format_money(float(price) * float(quantity), currency)}")
        except (TypeError, ValueError):
            pass
    if holding is not None:
        lines.append(f"보유: {format_quantity(holding)}주")
    if when is not None:
        lines.append(f"시각: {when:%Y-%m-%d %H:%M}")
    return title, "\n".join(lines)


# ==============================================================================
# [카카오 알림 부품] 토큰으로 '나에게 보내기'를 호출하고, 토큰이 만료(401)되면 갱신 후 한 번 다시 보낸다.
# Lumibot의 TelegramNotificationProvider 와 같은 모양이다 (notify → NotificationResult).
# ==============================================================================
class KakaoNotificationProvider:
    """KakaoTalk "send to me" provider with automatic token refresh."""

    provider = "kakao"

    def __init__(
        self,
        *,
        rest_api_key: str | None = None,
        client_secret: str | None = None,
        token_file: str | os.PathLike | None = None,
        access_token: str | None = None,
        refresh_token: str | None = None,
        link_url: str = "https://github.com/Lumiwealth/lumibot",
        timeout: float = 10,
        session: Any = None,
    ) -> None:
        # 설정값은 '직접 넘긴 값 → .env 값 → 기본값' 순서로 정한다.
        # session: 평소엔 진짜 requests, 테스트 때는 가짜 서버를 넣는 칸 (의존성 주입).
        self.rest_api_key = rest_api_key or os.environ.get("KAKAO_REST_API_KEY", "")
        self.client_secret = client_secret or os.environ.get("KAKAO_CLIENT_SECRET", "")
        self.token_file = Path(token_file or os.environ.get("KAKAO_TOKEN_FILE") or "kakao_token.json")
        self._initial_access_token = access_token or os.environ.get("KAKAO_ACCESS_TOKEN", "")
        self._initial_refresh_token = refresh_token or os.environ.get("KAKAO_REFRESH_TOKEN", "")
        self.link_url = link_url
        self.timeout = timeout
        self.http = session or requests
        self._lock = threading.Lock()

    # ------------------------------------------------------------------ tokens
    # [토큰 파일] kakao_token.json 읽기/쓰기. 파일이 없거나 깨졌으면 .env 의 KAKAO_ACCESS_TOKEN 등으로 채운다.
    def load_token(self) -> dict[str, str]:
        token: dict[str, str] = {}
        if self.token_file.exists():
            try:
                with open(self.token_file, encoding="utf-8") as f:
                    token = json.load(f) or {}
            except (OSError, ValueError):
                token = {}
        token.setdefault("access_token", self._initial_access_token)
        token.setdefault("refresh_token", self._initial_refresh_token)
        if not token.get("access_token"):
            token["access_token"] = self._initial_access_token
        if not token.get("refresh_token"):
            token["refresh_token"] = self._initial_refresh_token
        return token

    def save_token(self, token: dict[str, str]) -> None:
        with open(self.token_file, "w", encoding="utf-8") as f:
            json.dump(token, f, ensure_ascii=False, indent=2)
        try:
            os.chmod(self.token_file, 0o600)
        except OSError:
            pass

    def is_configured(self) -> bool:
        return bool(self.load_token().get("access_token"))

    # [토큰 갱신] refresh_token 으로 새 access_token 을 받아 파일에 저장한다.
    # Lock 으로 동시에 두 번 갱신하지 않게 막고, 에러 메시지에는 토큰 값을 절대 넣지 않는다.
    def refresh_access_token(self) -> dict[str, str]:
        """Get a new access token with the refresh token and store it."""
        with self._lock:
            token = self.load_token()
            if not self.rest_api_key:
                raise RuntimeError("KAKAO_REST_API_KEY is required to refresh the Kakao token")
            if not token.get("refresh_token"):
                raise RuntimeError("Kakao refresh_token is missing; log in again to get new tokens")
            data = {
                "grant_type": "refresh_token",
                "client_id": self.rest_api_key,
                "refresh_token": token["refresh_token"],
            }
            if self.client_secret:
                data["client_secret"] = self.client_secret
            response = self.http.post(TOKEN_URL, data=data, timeout=self.timeout)
            try:
                result = response.json()
            except ValueError:
                result = {}
            if not result.get("access_token"):
                raise RuntimeError(f"Kakao token refresh failed: {result.get('error_code') or response.status_code}")
            token["access_token"] = result["access_token"]
            if result.get("refresh_token"):  # Kakao sends a new one only when the old one is close to expiry
                token["refresh_token"] = result["refresh_token"]
            self.save_token(token)
            return token

    # ---------------------------------------------------------------- sending
    # [실제 전송] 카카오 '나에게 보내기' API 를 한 번 호출한다.
    def _post_message(self, text: str, access_token: str) -> Any:
        template = {"object_type": "text", "text": text, "link": {"web_url": self.link_url}}
        return self.http.post(
            SEND_URL,
            headers={"Authorization": f"Bearer {access_token}"},
            data={"template_object": json.dumps(template, ensure_ascii=False)},
            timeout=self.timeout,
        )

    # [메시지 보내기] 토큰 확인 → 전송 → 401 이면 갱신 후 딱 한 번 재시도 → 결과 객체로 돌려준다.
    # 실패해도 예외를 던지지 않고 ok=False 결과를 돌려준다 (부르는 쪽이 멈추지 않게).
    def notify(self, *, title: str, message: str, severity: str = "info", **kwargs: Any) -> NotificationResult:
        token = self.load_token()
        if not token.get("access_token"):
            return NotificationResult(
                ok=False,
                provider=self.provider,
                title=title,
                message=message,
                severity=severity,
                skipped=True,
                reason="Kakao access token is missing (kakao_token.json or KAKAO_ACCESS_TOKEN)",
            )

        prefix = "" if severity in (None, "", "info") else f"[{severity.upper()}] "
        text = build_text(prefix + (title or ""), message)

        try:
            response = self._post_message(text, token["access_token"])
            if response.status_code == 401:  # expired access token -> refresh once and retry
                token = self.refresh_access_token()
                response = self._post_message(text, token["access_token"])
        except (requests.RequestException, RuntimeError) as exc:
            return NotificationResult(
                ok=False,
                provider=self.provider,
                title=title,
                message=message,
                severity=severity,
                reason=f"kakao request failed: {exc}",
            )

        try:
            payload = response.json() if getattr(response, "content", b"") else {}
        except ValueError:
            payload = {}

        ok = response.status_code == 200 and payload.get("result_code") == 0
        return NotificationResult(
            ok=ok,
            provider=self.provider,
            title=title,
            message=message,
            severity=severity,
            reason=None if ok else f"kakao send failed with status {response.status_code}: {payload.get('msg', '')}",
            payload=payload,
        )


# ==============================================================================
# [자동 매매 알림] 엔진(strategy_executor.py)이 체결·부분 체결·취소·장 마감 때 부르는 관리자.
# 언제 무엇을 보낼지 정하고, 문구는 위의 format_* 함수로, 전송은 KakaoNotificationProvider 로 한다.
# ==============================================================================
class KakaoTradeAlerts:
    """Automatic KakaoTalk alerts for fills, partial fills, cancels and the daily summary."""

    def __init__(
        self,
        strategy: Any,
        provider: KakaoNotificationProvider | None = None,
        *,
        in_backtest: bool = False,
        daily_summary: bool = True,
        max_backtest_alerts: int = 20,
    ) -> None:
        self.strategy = strategy
        self.provider = provider or KakaoNotificationProvider()
        self.in_backtest = in_backtest
        self.daily_summary = daily_summary
        self.max_backtest_alerts = max_backtest_alerts
        self.sent_count = 0
        self._limit_logged = False
        self._today_fills: list[dict[str, Any]] = []

    # [만들기] .env 에 KAKAO_TRADE_ALERTS=true 일 때만 만든다. 꺼져 있으면 None → 엔진은 아무것도 안 한다.
    @classmethod
    def from_env(cls, strategy: Any) -> KakaoTradeAlerts | None:
        """Return an instance when KAKAO_TRADE_ALERTS is on, otherwise None."""
        if not _env_flag("KAKAO_TRADE_ALERTS"):
            return None
        alerts = cls(
            strategy,
            in_backtest=_env_flag("KAKAO_ALERT_IN_BACKTEST"),
            daily_summary=_env_flag("KAKAO_DAILY_SUMMARY", True),
            max_backtest_alerts=_env_int("KAKAO_BACKTEST_MAX_ALERTS", 20),
        )
        alerts.check_setup()
        return alerts

    # [설정 점검] 키나 토큰이 없으면 시작할 때 경고 한 줄로 설명서 위치를 알려 준다 (매매는 계속된다).
    def setup_problem(self) -> str | None:
        """Return what is missing for alerts to work, or None when ready."""
        if not self.provider.rest_api_key:
            return "KAKAO_REST_API_KEY 가 .env 에 없습니다"
        for name, value in (("KAKAO_REST_API_KEY", self.provider.rest_api_key),
                            ("KAKAO_CLIENT_SECRET", self.provider.client_secret)):
            problem = check_key(name, value)
            if problem:
                return problem
        if not self.provider.is_configured():
            return "카카오 토큰이 없습니다 (kakao_token.json)"
        return None

    def check_setup(self) -> str | None:
        """Log a short hint pointing at the guide when setup is incomplete. Trading continues."""
        problem = self.setup_problem()
        if problem:
            logger = getattr(self.strategy, "logger", None)
            hint = (
                f"[kakao] 카카오 알림이 켜져 있지만 설정이 끝나지 않았습니다: {problem}. "
                "설정 안내: 'lumibot kakao guide' / 로그인: 'lumibot kakao login'"
            )
            log = getattr(logger, "warning", None) or getattr(logger, "info", None)
            if log is not None:
                log(hint)
        return problem

    # ---------------------------------------------------------------- helpers
    # [내부 도우미] 백테스트 여부, 보내도 되는지, 실제 전송, 통화·시각·매수 여부·종목 이름 확인.
    @property
    def is_backtesting(self) -> bool:
        return bool(getattr(self.strategy, "is_backtesting", False))

    def _log(self, message: str) -> None:
        logger = getattr(self.strategy, "logger", None)
        if logger is not None:
            logger.info(message)

    # 백테스트 중에는 기본으로 안 보내고, 켜더라도 최대 개수(max_backtest_alerts)까지만 보낸다.
    def _can_send(self) -> bool:
        if not self.is_backtesting:
            return True
        if not self.in_backtest:
            return False
        if self.sent_count >= self.max_backtest_alerts:
            if not self._limit_logged:
                self._log(f"[kakao] backtest alert limit reached ({self.max_backtest_alerts}); further alerts skipped")
                self._limit_logged = True
            return False
        return True

    def _send(self, title: str, message: str) -> NotificationResult | None:
        if not self._can_send():
            return None
        result = self.provider.notify(title=title, message=message)
        if result.ok:
            self.sent_count += 1
        else:
            self._log(f"[kakao] alert not sent: {result.reason}")
        return result

    def _currency(self) -> str:
        quote = getattr(self.strategy, "quote_asset", None)
        return str(getattr(quote, "symbol", None) or os.environ.get("KAKAO_CURRENCY") or "USD")

    def _now(self) -> datetime | None:
        try:
            return self.strategy.get_datetime()
        except Exception:
            return None

    @staticmethod
    def _is_buy(order: Any) -> bool:
        is_buy_order = getattr(order, "is_buy_order", None)
        if callable(is_buy_order):
            try:
                return bool(is_buy_order())
            except Exception:
                pass
        return str(getattr(order, "side", "")).lower() in _BUY_SIDES

    @staticmethod
    def _symbol(order: Any) -> str:
        asset = getattr(order, "asset", None)
        return str(getattr(asset, "symbol", None) or asset or "?")

    # ----------------------------------------------------------------- events
    # [이벤트] 체결/부분 체결: 오늘 기록에 추가하고 체결 알림을 보낸다. hide_trades 면 상세 내용을 숨긴다.
    def on_fill(self, position: Any, order: Any, price: Any, quantity: Any, multiplier: Any = 1,
                *, partial: bool = False) -> NotificationResult | None:
        is_buy = self._is_buy(order)
        symbol = self._symbol(order)
        self._today_fills.append({"symbol": symbol, "is_buy": is_buy, "quantity": quantity, "price": price})

        if getattr(self.strategy, "hide_trades", False):
            return self._send("[체결] 매매가 체결되었습니다", "hide_trades 설정으로 상세 내용은 숨겨졌습니다.")

        title, message = format_fill_message(
            symbol=symbol,
            is_buy=is_buy,
            quantity=quantity,
            price=price,
            holding=getattr(position, "quantity", None),
            when=self._now(),
            currency=self._currency(),
            partial=partial,
        )
        return self._send(title, message)

    # [이벤트] 주문 취소 알림.
    def on_cancel(self, order: Any) -> NotificationResult | None:
        side = "매수" if self._is_buy(order) else "매도"
        title = f"[취소] {self._symbol(order)} {format_quantity(getattr(order, 'quantity', '?'))}주 {side} 주문"
        now = self._now()
        message = f"시각: {now:%Y-%m-%d %H:%M}" if now is not None else ""
        return self._send(title, message)

    # [이벤트] 장 마감 요약: 오늘 체결 수, 평가액, 현금, 보유 종목. 보낸 뒤 오늘 기록을 비운다.
    def on_market_close(self) -> NotificationResult | None:
        fills, self._today_fills = self._today_fills, []
        if not self.daily_summary:
            return None
        buys = sum(1 for f in fills if f["is_buy"])
        sells = len(fills) - buys
        currency = self._currency()
        now = self._now()
        day = f"{now:%Y-%m-%d}" if now is not None else ""

        lines = [f"체결 {len(fills)}건 (매수 {buys}, 매도 {sells})"]
        try:
            lines.append(f"평가액: {format_money(float(self.strategy.portfolio_value), currency)}")
        except Exception:
            pass
        try:
            lines.append(f"현금: {format_money(float(self.strategy.cash), currency)}")
        except Exception:
            pass
        try:
            quote = getattr(self.strategy, "quote_asset", None)
            held = [
                f"{getattr(p.asset, 'symbol', p.asset)} {format_quantity(p.quantity)}주"
                for p in self.strategy.get_positions()
                if quote is None or p.asset != quote
            ]
            if held:
                lines.append("보유: " + ", ".join(held[:5]) + (" 외" if len(held) > 5 else ""))
        except Exception:
            pass
        return self._send(f"[장 마감 요약] {day}".strip(), "\n".join(lines))
