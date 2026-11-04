"""Tests for `lumibot kakao guide | login | test` (no network, no real keys)."""

import json
import re
import socket
import threading
import time
from types import SimpleNamespace

import requests

from lumibot.components.notifications import kakao_setup
from lumibot.components.notifications.kakao import KakaoTradeAlerts, SEND_URL, TOKEN_URL
from lumibot.components.notifications.kakao_setup import (
    DEFAULT_REDIRECT_URI,
    KakaoSetupError,
    build_authorize_url,
    can_use_local_server,
    cmd_login,
    exchange_code,
    explain_error,
    parse_callback,
    read_guide,
)


# [가짜 카카오 응답] 진짜 서버 대신 정해 둔 상태 코드와 내용을 돌려준다.
class _Response:
    def __init__(self, status_code=200, payload=None):
        self.status_code = status_code
        self._payload = payload or {}
        self.content = json.dumps(self._payload).encode()

    def json(self):
        return self._payload


# [가짜 requests] 준비해 둔 응답을 순서대로 돌려주고, 어떤 요청이 왔는지 기록한다.
class _FakeSession:
    def __init__(self, *responses):
        self.responses = list(responses)
        self.calls = []

    def post(self, url, **kwargs):
        self.calls.append({"url": url, **kwargs})
        return self.responses.pop(0)


TOKENS_OK = {"access_token": "AT", "refresh_token": "RT", "scope": "talk_message"}


# 테스트마다 .env 대신 환경변수를 직접 정하고, 토큰 파일은 임시 폴더에 둔다.
def _setup_env(monkeypatch, tmp_path, redirect_uri=None):
    monkeypatch.setattr(kakao_setup, "_load_dotenv", lambda: None)
    monkeypatch.setenv("KAKAO_REST_API_KEY", "rest-key")
    monkeypatch.delenv("KAKAO_CLIENT_SECRET", raising=False)
    monkeypatch.setenv("KAKAO_TOKEN_FILE", str(tmp_path / "kakao_token.json"))
    if redirect_uri:
        monkeypatch.setenv("KAKAO_REDIRECT_URI", redirect_uri)
    else:
        monkeypatch.delenv("KAKAO_REDIRECT_URI", raising=False)


# 화면에 찍힌 로그인 주소에서 state 값을 꺼낸다 (가짜 브라우저가 같은 값을 돌려주려고).
def _state_from(lines):
    for line in lines:
        match = re.search(r"state=([^&\s]+)", line)
        if match:
            return match.group(1)
    raise AssertionError("authorize URL with state was not printed")


# [1] 도우미 테스트: 설명서, 로그인 주소, code 읽기, 로컬 서버 조건, 에러 안내
# ------------------------------------------------------------------- small helpers
def test_guide_is_packaged_and_mentions_commands():
    guide = read_guide()
    assert "lumibot kakao login" in guide
    assert DEFAULT_REDIRECT_URI in guide
    assert "talk_message" in guide


def test_authorize_url_requests_talk_message():
    url = build_authorize_url("KEY", "http://localhost:18765/kakao/callback", "STATE")
    params = parse_callback(url)
    assert params["client_id"] == "KEY"
    assert params["scope"] == "talk_message"
    assert params["redirect_uri"] == "http://localhost:18765/kakao/callback"
    assert params["state"] == "STATE"
    assert params["response_type"] == "code"


def test_parse_callback_reads_code_and_error():
    assert parse_callback("http://localhost:18765/kakao/callback?code=C&state=S") == {"code": "C", "state": "S"}
    assert parse_callback("/kakao/callback?error=access_denied")["error"] == "access_denied"


def test_local_server_only_for_http_localhost():
    assert can_use_local_server("http://localhost:18765/kakao/callback")
    assert can_use_local_server("http://127.0.0.1:8080/cb")
    assert not can_use_local_server("https://localhost:18765")
    assert not can_use_local_server("https://example.com/oauth")


def test_error_hints_name_the_fix():
    assert "리다이렉트 URI" in explain_error("KOE006", "http://localhost:18765/kakao/callback")
    assert "http://localhost:18765/kakao/callback" in explain_error("KOE006", "http://localhost:18765/kakao/callback")
    assert "KAKAO_REST_API_KEY" in explain_error("KOE010")
    assert "다시 실행" in explain_error("KOE320")


# [2] 토큰 교환 테스트: 성공, 권한(talk_message) 없음, KOE010
# ------------------------------------------------------------------ token exchange
def test_exchange_code_success_sends_secret_when_set():
    session = _FakeSession(_Response(200, TOKENS_OK))
    result = exchange_code("CODE", rest_api_key="K", redirect_uri="R", client_secret="S", session=session)
    assert result["access_token"] == "AT"
    data = session.calls[0]["data"]
    assert data["grant_type"] == "authorization_code" and data["code"] == "CODE"
    assert data["client_secret"] == "S"


def test_exchange_code_without_talk_message_explains_consent_setting():
    session = _FakeSession(_Response(200, {"access_token": "AT", "refresh_token": "RT", "scope": "profile_nickname"}))
    try:
        exchange_code("CODE", rest_api_key="K", redirect_uri="R", session=session)
    except KakaoSetupError as exc:
        assert "동의항목" in str(exc)
    else:
        raise AssertionError("missing talk_message must be reported")


def test_exchange_code_error_uses_hint():
    session = _FakeSession(_Response(401, {"error": "invalid_client", "error_code": "KOE010"}))
    try:
        exchange_code("CODE", rest_api_key="K", redirect_uri="R", session=session)
    except KakaoSetupError as exc:
        assert "KOE010" in str(exc) and "KAKAO_CLIENT_SECRET" in str(exc)
    else:
        raise AssertionError("KOE010 must be reported")


# [3] 로그인 전체 흐름 테스트: 키 없음, 수동 입력, state 불일치, 동의 취소, 진짜 로컬 서버
# --------------------------------------------------------------------------- login
def test_login_without_key_points_to_guide(monkeypatch, tmp_path):
    _setup_env(monkeypatch, tmp_path)
    monkeypatch.delenv("KAKAO_REST_API_KEY", raising=False)
    lines = []
    assert cmd_login(open_browser=False, out=lines.append) == 1
    text = "\n".join(lines)
    assert "lumibot kakao guide" in text
    assert DEFAULT_REDIRECT_URI in text


def test_login_manual_flow_saves_token_and_sends_test(monkeypatch, tmp_path):
    _setup_env(monkeypatch, tmp_path)
    session = _FakeSession(_Response(200, TOKENS_OK), _Response(200, {"result_code": 0}))
    lines = []

    def ask(_prompt):
        return f"{DEFAULT_REDIRECT_URI}?code=CODE&state={_state_from(lines)}"

    code = cmd_login(manual=True, open_browser=False, out=lines.append, ask=ask, session=session)

    assert code == 0, "\n".join(lines)
    saved = json.loads((tmp_path / "kakao_token.json").read_text(encoding="utf-8"))
    assert saved == {"access_token": "AT", "refresh_token": "RT"}
    assert [c["url"] for c in session.calls] == [TOKEN_URL, SEND_URL]


def test_login_rejects_wrong_state(monkeypatch, tmp_path):
    _setup_env(monkeypatch, tmp_path)
    lines = []
    code = cmd_login(manual=True, open_browser=False, out=lines.append,
                     ask=lambda _p: f"{DEFAULT_REDIRECT_URI}?code=CODE&state=WRONG", session=_FakeSession())
    assert code == 1
    assert not (tmp_path / "kakao_token.json").exists()


def test_login_reports_cancelled_consent(monkeypatch, tmp_path):
    _setup_env(monkeypatch, tmp_path)
    lines = []
    code = cmd_login(manual=True, open_browser=False, out=lines.append,
                     ask=lambda _p: f"{DEFAULT_REDIRECT_URI}?error=access_denied", session=_FakeSession())
    assert code == 1
    assert "동의" in "\n".join(lines)


# 비어 있는 포트 번호를 하나 찾는다 (로컬 서버 테스트용).
def _free_port():
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def test_login_receives_code_on_local_server(monkeypatch, tmp_path):
    redirect = f"http://127.0.0.1:{_free_port()}/kakao/callback"
    _setup_env(monkeypatch, tmp_path, redirect_uri=redirect)
    session = _FakeSession(_Response(200, TOKENS_OK), _Response(200, {"result_code": 0}))
    lines = []

    def fake_browser():
        # Plays the role of Kakao redirecting the browser back to the local server.
        for _ in range(100):
            if any("state=" in line for line in lines):
                break
            time.sleep(0.05)
        requests.get(f"{redirect}?code=CODE&state={_state_from(lines)}", timeout=5)

    threading.Thread(target=fake_browser, daemon=True).start()
    code = cmd_login(open_browser=False, timeout=10, out=lines.append, session=session)

    assert code == 0, "\n".join(lines)
    assert (tmp_path / "kakao_token.json").exists()


# [4] 시작할 때 설정 점검과 lumibot kakao 명령 등록 테스트
# ------------------------------------------------------------- startup setup check
def test_trade_alerts_warn_when_setup_is_missing(monkeypatch, tmp_path):
    monkeypatch.setenv("KAKAO_TRADE_ALERTS", "true")
    monkeypatch.delenv("KAKAO_REST_API_KEY", raising=False)
    monkeypatch.delenv("KAKAO_ACCESS_TOKEN", raising=False)
    monkeypatch.setenv("KAKAO_TOKEN_FILE", str(tmp_path / "none.json"))
    warnings = []
    strategy = SimpleNamespace(is_backtesting=False, logger=SimpleNamespace(warning=warnings.append))

    alerts = KakaoTradeAlerts.from_env(strategy)

    assert alerts is not None
    assert warnings and "lumibot kakao guide" in warnings[0]


def test_cli_has_kakao_command():
    from lumibot.cli import build_parser

    args = build_parser().parse_args(["kakao", "login", "--manual"])
    assert args.action == "login" and args.manual is True


# [4] 키 검사와 한국어 원인 안내 (예시 글자를 그대로 둔 실수, 알 수 없는 KOE 코드)
# ------------------------------------------------------------------- key checks & hints
def test_check_key_catches_placeholder_text():
    from lumibot.components.notifications.kakao import check_key, key_format_warning

    assert "예시 글자" in check_key("KAKAO_REST_API_KEY", "복사한_REST_API_키")
    assert check_key("KAKAO_REST_API_KEY", "ab cd") is not None
    assert check_key("KAKAO_REST_API_KEY", "0123456789abcdef0123456789abcdef") is None
    assert key_format_warning("0123456789abcdef0123456789abcdef") is None
    assert "32자" in key_format_warning("short-key")


def test_explain_reason_turns_codes_into_korean():
    from lumibot.components.notifications.kakao_setup import explain_reason

    assert "KAKAO_REST_API_KEY" in explain_reason("kakao request failed: Kakao token refresh failed: KOE101")
    assert "동의항목" in explain_reason("kakao send failed with status 403: insufficient scopes.")
    assert "lumibot kakao login" in explain_reason("Kakao token refresh failed: 400")
    assert "인터넷" in explain_reason("kakao request failed: ConnectionError")
    assert explain_reason("something else") == ""


def test_cmd_test_stops_when_key_is_placeholder(monkeypatch, tmp_path):
    _setup_env(monkeypatch, tmp_path)
    monkeypatch.setenv("KAKAO_REST_API_KEY", "복사한_REST_API_키")
    session = _FakeSession()
    lines = []
    assert kakao_setup.cmd_test(out=lines.append, session=session) == 1
    assert "예시 글자" in "\n".join(lines)
    assert session.calls == []  # 카카오에 요청조차 보내지 않는다


def test_cmd_test_explains_koe101(monkeypatch, tmp_path):
    _setup_env(monkeypatch, tmp_path)
    (tmp_path / "kakao_token.json").write_text(json.dumps({"access_token": "old", "refresh_token": "RT"}))
    session = _FakeSession(_Response(401, {"code": -401}), _Response(401, {"error_code": "KOE101"}))
    lines = []
    assert kakao_setup.cmd_test(out=lines.append, session=session) == 1
    text = "\n".join(lines)
    assert "해결 방법" in text and "KOE101" in text


def test_login_stops_when_key_is_placeholder(monkeypatch, tmp_path):
    _setup_env(monkeypatch, tmp_path)
    monkeypatch.setenv("KAKAO_REST_API_KEY", "복사한_REST_API_키")
    lines = []
    assert cmd_login(open_browser=False, out=lines.append) == 1
    assert "키가 이상함" in "\n".join(lines)


def test_trade_alerts_warn_when_key_is_placeholder(monkeypatch, tmp_path):
    monkeypatch.setenv("KAKAO_TRADE_ALERTS", "true")
    monkeypatch.setenv("KAKAO_REST_API_KEY", "복사한_REST_API_키")
    monkeypatch.setenv("KAKAO_TOKEN_FILE", str(tmp_path / "none.json"))
    warnings = []
    strategy = SimpleNamespace(is_backtesting=False, logger=SimpleNamespace(warning=warnings.append))
    KakaoTradeAlerts.from_env(strategy)
    assert warnings and "예시 글자" in warnings[0]
