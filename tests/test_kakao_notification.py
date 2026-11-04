"""Unit tests for the KakaoTalk notification provider and automatic trade alerts.

All Kakao HTTP calls are replaced by a fake session, so no network or real keys are needed.
"""

import json
from datetime import datetime
from types import SimpleNamespace

from lumibot.components.notifications import NotificationManager
from lumibot.components.notifications.kakao import (
    MAX_TEXT_LENGTH,
    SEND_URL,
    TOKEN_URL,
    KakaoNotificationProvider,
    KakaoTradeAlerts,
    build_text,
    format_fill_message,
    format_money,
)


# [가짜 카카오 응답] 진짜 서버 대신 정해 둔 상태 코드와 내용을 돌려준다.
class _Response:
    def __init__(self, status_code=200, payload=None):
        self.status_code = status_code
        self._payload = {"result_code": 0} if payload is None else payload
        self.content = json.dumps(self._payload).encode()

    def json(self):
        return self._payload


# [가짜 requests] 준비해 둔 응답을 순서대로 돌려주고, 어떤 요청이 왔는지 기록한다.
class _FakeSession:
    """Returns queued responses in order and records every call."""

    def __init__(self, *responses):
        self.responses = list(responses)
        self.calls = []

    def post(self, url, **kwargs):
        self.calls.append({"url": url, **kwargs})
        return self.responses.pop(0)


# 임시 폴더에 토큰 파일을 만들고 가짜 세션을 넣은 Provider 를 만든다.
def _provider(tmp_path, session, token=None, **kwargs):
    token_file = tmp_path / "kakao_token.json"
    if token is not None:
        token_file.write_text(json.dumps(token), encoding="utf-8")
    return KakaoNotificationProvider(
        rest_api_key="rest-key",
        client_secret="secret",
        token_file=token_file,
        session=session,
        **kwargs,
    )


# [1] Provider 테스트: 전송 성공, 토큰 만료 시 갱신·재시도, 실패 보고, 토큰 없음 처리
# --------------------------------------------------------------------- provider
def test_send_success_uses_access_token(tmp_path):
    session = _FakeSession(_Response(200, {"result_code": 0}))
    provider = _provider(tmp_path, session, {"access_token": "AT", "refresh_token": "RT"})

    result = provider.notify(title="[체결] DEMO 10주 매수", message="가격: $100.00")

    assert result.ok is True
    assert result.provider == "kakao"
    call = session.calls[0]
    assert call["url"] == SEND_URL
    assert call["headers"]["Authorization"] == "Bearer AT"
    template = json.loads(call["data"]["template_object"])
    assert template["object_type"] == "text"
    assert template["text"] == "[체결] DEMO 10주 매수\n가격: $100.00"


def test_expired_token_is_refreshed_and_message_retried(tmp_path):
    session = _FakeSession(
        _Response(401, {"code": -401, "msg": "this access token does not exist"}),
        _Response(200, {"access_token": "NEW_AT", "refresh_token": "NEW_RT"}),
        _Response(200, {"result_code": 0}),
    )
    provider = _provider(tmp_path, session, {"access_token": "OLD_AT", "refresh_token": "OLD_RT"})

    result = provider.notify(title="t", message="m")

    assert result.ok is True
    assert [c["url"] for c in session.calls] == [SEND_URL, TOKEN_URL, SEND_URL]
    refresh_data = session.calls[1]["data"]
    assert refresh_data["grant_type"] == "refresh_token"
    assert refresh_data["refresh_token"] == "OLD_RT"
    assert refresh_data["client_secret"] == "secret"
    assert session.calls[2]["headers"]["Authorization"] == "Bearer NEW_AT"
    saved = json.loads((tmp_path / "kakao_token.json").read_text(encoding="utf-8"))
    assert saved == {"access_token": "NEW_AT", "refresh_token": "NEW_RT"}


def test_refresh_keeps_old_refresh_token_when_kakao_does_not_send_one(tmp_path):
    session = _FakeSession(
        _Response(401, {"code": -401}),
        _Response(200, {"access_token": "NEW_AT"}),
        _Response(200, {"result_code": 0}),
    )
    provider = _provider(tmp_path, session, {"access_token": "OLD_AT", "refresh_token": "KEEP_RT"})

    assert provider.notify(title="t", message="m").ok is True
    saved = json.loads((tmp_path / "kakao_token.json").read_text(encoding="utf-8"))
    assert saved["refresh_token"] == "KEEP_RT"


def test_failed_refresh_returns_error_without_leaking_tokens(tmp_path):
    session = _FakeSession(
        _Response(401, {"code": -401}),
        _Response(400, {"error": "invalid_grant", "error_code": "KOE322"}),
    )
    provider = _provider(tmp_path, session, {"access_token": "SECRET_AT", "refresh_token": "SECRET_RT"})

    result = provider.notify(title="t", message="m")

    assert result.ok is False
    assert "KOE322" in result.reason
    assert "SECRET" not in result.reason


def test_missing_token_is_skipped_without_http_call(tmp_path, monkeypatch):
    monkeypatch.delenv("KAKAO_ACCESS_TOKEN", raising=False)
    monkeypatch.delenv("KAKAO_REFRESH_TOKEN", raising=False)
    session = _FakeSession()
    provider = _provider(tmp_path, session)

    result = provider.notify(title="t", message="m")

    assert result.ok is False and result.skipped is True
    assert session.calls == []


def test_initial_tokens_can_come_from_environment(tmp_path, monkeypatch):
    monkeypatch.setenv("KAKAO_ACCESS_TOKEN", "ENV_AT")
    session = _FakeSession(_Response(200, {"result_code": 0}))
    provider = _provider(tmp_path, session)

    assert provider.notify(title="t", message="m").ok is True
    assert session.calls[0]["headers"]["Authorization"] == "Bearer ENV_AT"


def test_send_failure_is_reported(tmp_path):
    session = _FakeSession(_Response(403, {"msg": "insufficient scopes.", "code": -402}))
    provider = _provider(tmp_path, session, {"access_token": "AT", "refresh_token": "RT"})

    result = provider.notify(title="t", message="m")

    assert result.ok is False
    assert "403" in result.reason and "insufficient scopes" in result.reason


def test_notification_manager_can_configure_kakao(tmp_path):
    manager = NotificationManager(SimpleNamespace(is_backtesting=False), enabled=True)
    provider = manager.configure_kakao(rest_api_key="k", token_file=str(tmp_path / "t.json"))
    assert isinstance(provider, KakaoNotificationProvider)
    assert manager.providers == [provider]


# [2] 문구 테스트: 200자 자르기, 원/달러 표시, 체결·부분 체결 문구
# ------------------------------------------------------------------ formatting
def test_build_text_is_cut_to_kakao_limit():
    text = build_text("제목", "가" * 500)
    assert len(text) == MAX_TEXT_LENGTH
    assert text.endswith("…")


def test_money_format_by_currency():
    assert format_money(70000, "KRW") == "70,000원"
    assert format_money(123.4, "USD") == "$123.40"
    assert format_money(None, "KRW") == "-"


def test_fill_message_for_korean_stock():
    title, message = format_fill_message(
        symbol="005930", is_buy=True, quantity=10.0, price=70000, holding=10,
        when=datetime(2026, 11, 2, 9, 30), currency="KRW",
    )
    assert title == "[체결] 005930 10주 매수"
    assert "가격: 70,000원" in message
    assert "금액: 700,000원" in message
    assert "보유: 10주" in message
    assert "시각: 2026-11-02 09:30" in message


def test_partial_fill_title():
    title, _ = format_fill_message(symbol="005930", is_buy=False, quantity=3, price=1, partial=True)
    assert title == "[부분 체결] 005930 3주 매도"


# [3] 자동 알림 테스트: 스위치, 실전 전송, 백테스트 차단·최대 개수, hide_trades, 취소, 장 마감 요약
# ---------------------------------------------------------------- trade alerts
# 보낸 메시지를 기록만 하는 가짜 Provider (카카오에 실제로 보내지 않는다).
class _RecordingProvider:
    def __init__(self, ok=True):
        self.sent = []
        self.ok = ok

    def notify(self, *, title, message, severity="info", **kwargs):
        self.sent.append((title, message))
        return SimpleNamespace(ok=self.ok, reason=None if self.ok else "boom")


def _strategy(is_backtesting=False, hide_trades=False):
    return SimpleNamespace(
        is_backtesting=is_backtesting,
        hide_trades=hide_trades,
        quote_asset=SimpleNamespace(symbol="KRW"),
        portfolio_value=10_700_000,
        cash=10_000_000,
        logger=SimpleNamespace(info=lambda *a, **k: None),
        get_datetime=lambda: datetime(2026, 11, 2, 15, 30),
        get_positions=lambda: [SimpleNamespace(asset=SimpleNamespace(symbol="005930"), quantity=10)],
    )


def _order(side="buy", symbol="005930", quantity=10):
    return SimpleNamespace(
        side=side,
        quantity=quantity,
        asset=SimpleNamespace(symbol=symbol),
        is_buy_order=lambda: side == "buy",
    )


def test_trade_alerts_are_off_unless_enabled(monkeypatch):
    monkeypatch.delenv("KAKAO_TRADE_ALERTS", raising=False)
    assert KakaoTradeAlerts.from_env(_strategy()) is None
    monkeypatch.setenv("KAKAO_TRADE_ALERTS", "true")
    assert isinstance(KakaoTradeAlerts.from_env(_strategy()), KakaoTradeAlerts)


def test_fill_alert_is_sent_live():
    provider = _RecordingProvider()
    alerts = KakaoTradeAlerts(_strategy(), provider)

    alerts.on_fill(SimpleNamespace(quantity=10), _order(), 70000, 10, 1)

    title, message = provider.sent[0]
    assert title == "[체결] 005930 10주 매수"
    assert "70,000원" in message and "보유: 10주" in message


def test_backtest_alerts_are_off_by_default():
    provider = _RecordingProvider()
    alerts = KakaoTradeAlerts(_strategy(is_backtesting=True), provider)

    assert alerts.on_fill(None, _order(), 100, 1) is None
    assert provider.sent == []


def test_backtest_alerts_respect_max_limit():
    provider = _RecordingProvider()
    alerts = KakaoTradeAlerts(_strategy(is_backtesting=True), provider, in_backtest=True, max_backtest_alerts=2)

    for _ in range(5):
        alerts.on_fill(None, _order(), 100, 1)

    assert len(provider.sent) == 2


def test_hide_trades_hides_details():
    provider = _RecordingProvider()
    alerts = KakaoTradeAlerts(_strategy(hide_trades=True), provider)

    alerts.on_fill(SimpleNamespace(quantity=10), _order(), 70000, 10)

    title, message = provider.sent[0]
    assert "005930" not in title + message


def test_cancel_alert():
    provider = _RecordingProvider()
    KakaoTradeAlerts(_strategy(), provider).on_cancel(_order(side="sell", quantity=7))
    assert provider.sent[0][0] == "[취소] 005930 7주 매도 주문"


def test_daily_summary_counts_fills_and_resets():
    provider = _RecordingProvider()
    alerts = KakaoTradeAlerts(_strategy(), provider)
    alerts.on_fill(None, _order("buy"), 70000, 10)
    alerts.on_fill(None, _order("sell"), 71000, 5)
    provider.sent.clear()

    alerts.on_market_close()

    title, message = provider.sent[0]
    assert title == "[장 마감 요약] 2026-11-02"
    assert "체결 2건 (매수 1, 매도 1)" in message
    assert "평가액: 10,700,000원" in message
    assert "보유: 005930 10주" in message

    alerts.on_market_close()
    assert "체결 0건" in provider.sent[-1][1]


def test_daily_summary_can_be_turned_off():
    provider = _RecordingProvider()
    alerts = KakaoTradeAlerts(_strategy(), provider, daily_summary=False)
    assert alerts.on_market_close() is None
    assert provider.sent == []


# [4] 엔진 테스트: 카카오에서 에러가 나도 엔진(_notify_kakao)이 멈추지 않는지 확인.
def test_executor_hook_never_raises():
    from lumibot.strategies.strategy_executor import StrategyExecutor

    warnings = []

    class _BrokenAlerts:
        def on_fill(self, *args, **kwargs):
            raise RuntimeError("kakao is down")

    fake_executor = SimpleNamespace(
        _kakao_trade_alerts=lambda: _BrokenAlerts(),
        strategy=SimpleNamespace(logger=SimpleNamespace(warning=warnings.append)),
    )

    StrategyExecutor._notify_kakao(fake_executor, "on_fill", None, None, 1, 1, 1)

    assert warnings and "kakao is down" in warnings[0]
