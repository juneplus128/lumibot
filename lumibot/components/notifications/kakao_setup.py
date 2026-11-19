"""Setup helpers for KakaoTalk alerts: ``lumibot kakao guide | login | test``.

The Kakao Developers console cannot be configured by a program, so those steps live in
``KAKAO_SETUP.md`` (shown by ``guide``). Everything after the console setup is automated by
``login``: it opens the Kakao login page, receives the authorization code on a small local
server, exchanges it for tokens, checks the ``talk_message`` permission, stores the tokens and
sends a test message. The user only clicks "동의" in the browser.
"""

from __future__ import annotations

import os
import re
import secrets
import sys
import threading
import webbrowser
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path
from typing import Any, Callable
from urllib.parse import parse_qs, urlencode, urlparse

import requests

from .kakao import TOKEN_URL, KakaoNotificationProvider, check_key, key_format_warning

# [상수] 로그인 주소, 기본 리다이렉트 URI, 설명서 파일 위치, 안내에 쓰는 명령 이름.
AUTHORIZE_URL = "https://kauth.kakao.com/oauth/authorize"
# 5000번은 macOS 의 AirPlay 수신 모드가 쓰고 있어서 겹치지 않는 18765 를 기본으로 쓴다.
DEFAULT_REDIRECT_URI = "http://localhost:18765/kakao/callback"
GUIDE_FILE = Path(__file__).with_name("KAKAO_SETUP.md")
GUIDE_COMMAND = "lumibot kakao guide"
LOGIN_COMMAND = "lumibot kakao login"

# [에러 안내] 카카오 에러 코드 → 사용자가 고칠 방법 (한국어).
ERROR_HINTS = {
    "KOE004": "카카오 로그인이 꺼져 있습니다. 콘솔 [카카오 로그인] → [사용 설정]을 ON 으로 바꾸세요.",
    "KOE006": "카카오 로그인 리다이렉트 URI 가 등록되지 않았거나 다릅니다. 콘솔 [앱] → [플랫폼 키] → [REST API 키]의 "
              "'카카오 로그인 리다이렉트 URI'(비즈니스 인증 아님)에 "
              "'{redirect_uri}' 를 글자 하나까지 똑같이 등록하세요.",
    "KOE010": "REST API 키 또는 클라이언트 시크릿이 틀렸습니다. 시크릿은 '카카오 로그인' 용(비즈니스 인증용 아님)이어야 합니다. "
              ".env 의 KAKAO_REST_API_KEY, "
              "KAKAO_CLIENT_SECRET 을 다시 확인하세요.",
    "KOE101": "REST API 키가 잘못되었습니다. .env 의 KAKAO_REST_API_KEY 에 콘솔 [앱] → [플랫폼 키] → "
              "[REST API 키]의 값을 그대로 넣으세요 (예시 글자·따옴표·띄어쓰기 없이).",
    "KOE303": "토큰 요청의 카카오 로그인 리다이렉트 URI 가 로그인 때와 다릅니다. KAKAO_REDIRECT_URI 를 확인하세요.",
    "KOE320": "인가 코드가 만료되었거나 이미 사용되었습니다. '" + LOGIN_COMMAND + "' 를 다시 실행하세요.",
    "KOE322": "로그인이 만료되었습니다 (약 2달). '" + LOGIN_COMMAND + "' 를 다시 실행하세요.",
    "-401": "토큰이 만료되었거나 잘못되었습니다. '" + LOGIN_COMMAND + "' 를 다시 실행하세요.",
    "-402": "카카오톡 메시지 전송 권한이 없습니다. 콘솔 [카카오 로그인] → [동의항목]에서 '카카오톡 메시지 전송'을 "
            "켠 뒤 '" + LOGIN_COMMAND + "' 를 다시 실행하세요.",
}


# 사용자가 읽고 바로 고칠 수 있는 메시지를 담는 에러.
class KakaoSetupError(Exception):
    """An error with a message the user can act on."""


# --------------------------------------------------------------------------- helpers
# [작은 도우미들] 설명서 읽기, 리다이렉트 URI 정하기, 로그인 주소 만들기, 주소에서 code 꺼내기.
def read_guide() -> str:
    return GUIDE_FILE.read_text(encoding="utf-8")


def get_redirect_uri() -> str:
    return os.environ.get("KAKAO_REDIRECT_URI", "").strip() or DEFAULT_REDIRECT_URI


def build_authorize_url(rest_api_key: str, redirect_uri: str, state: str) -> str:
    query = urlencode(
        {
            "client_id": rest_api_key,
            "redirect_uri": redirect_uri,
            "response_type": "code",
            "scope": "talk_message",
            "state": state,
        }
    )
    return f"{AUTHORIZE_URL}?{query}"


def parse_callback(url: str) -> dict[str, str]:
    """Read code/error/state from a redirected URL or path."""
    params = parse_qs(urlparse(url.strip()).query)
    return {key: values[0] for key, values in params.items() if values}


# 에러 코드를 해결 방법 문장으로 바꾼다.
def explain_error(error_code: str | None, redirect_uri: str = DEFAULT_REDIRECT_URI, fallback: str = "") -> str:
    hint = ERROR_HINTS.get(error_code or "")
    if hint:
        return f"[{error_code}] " + hint.format(redirect_uri=redirect_uri)
    return fallback or f"카카오 오류: {error_code}"


# 전송 실패 이유(영어)에서 에러 코드를 찾아 한국어 해결 방법으로 바꾼다. 모르는 이유면 빈 문자열.
def explain_reason(reason: str | None, redirect_uri: str = DEFAULT_REDIRECT_URI) -> str:
    reason = reason or ""
    match = re.search(r"KOE\d{3}", reason)
    if match and match.group(0) in ERROR_HINTS:
        return explain_error(match.group(0), redirect_uri)
    if "insufficient scopes" in reason or "status 403" in reason:
        return explain_error("-402", redirect_uri)
    if "status 401" in reason:
        return explain_error("-401", redirect_uri)
    if "refresh failed" in reason or "refresh_token is missing" in reason:
        return f"토큰을 새로 받지 못했습니다. '{LOGIN_COMMAND}' 를 다시 실행하세요."
    if "request failed" in reason:
        return "카카오 서버에 연결하지 못했습니다. 인터넷 연결을 확인하세요."
    return ""


# .env 의 키를 검사해 문제 문장 목록(막아야 하는 것)과 경고 문장을 돌려준다.
def _key_problems(rest_api_key: str, client_secret: str) -> tuple[list[str], str | None]:
    problems = [p for p in (check_key("KAKAO_REST_API_KEY", rest_api_key),
                            check_key("KAKAO_CLIENT_SECRET", client_secret)) if p]
    return problems, (None if problems else key_format_warning(rest_api_key))


# http://localhost 주소일 때만 내 컴퓨터에 작은 서버를 띄워 code 를 자동으로 받을 수 있다.
def can_use_local_server(redirect_uri: str) -> bool:
    parsed = urlparse(redirect_uri)
    return parsed.scheme == "http" and parsed.hostname in ("localhost", "127.0.0.1")


# [토큰 교환] code → 토큰. 받자마자 talk_message(카카오톡 메시지 전송) 권한이 있는지 확인한다.
def exchange_code(
    code: str,
    *,
    rest_api_key: str,
    redirect_uri: str,
    client_secret: str = "",
    session: Any = None,
    timeout: float = 10,
) -> dict[str, Any]:
    """Exchange the authorization code for tokens and check the talk_message permission."""
    http = session or requests
    data = {
        "grant_type": "authorization_code",
        "client_id": rest_api_key,
        "redirect_uri": redirect_uri,
        "code": code,
    }
    if client_secret:
        data["client_secret"] = client_secret
    response = http.post(TOKEN_URL, data=data, timeout=timeout)
    try:
        result = response.json()
    except ValueError:
        result = {}
    if not result.get("access_token"):
        raise KakaoSetupError(
            explain_error(
                result.get("error_code"),
                redirect_uri,
                fallback=f"토큰 발급 실패 (HTTP {getattr(response, 'status_code', '?')}): "
                         f"{result.get('error_description', '')}",
            )
        )
    scopes = str(result.get("scope", "")).split()
    if "scope" in result and "talk_message" not in scopes:
        raise KakaoSetupError(
            "토큰은 받았지만 '카카오톡 메시지 전송(talk_message)' 권한이 없습니다.\n"
            "  → 콘솔 [카카오 로그인] → [동의항목]에서 '카카오톡 메시지 전송'을 '이용 중 동의'로 설정한 뒤\n"
            f"    '{LOGIN_COMMAND}' 를 다시 실행하세요."
        )
    return result


# ------------------------------------------------------------------ local callback
# [로컬 콜백 서버] 카카오가 브라우저를 리다이렉트 URI 로 돌려보낼 때 code 를 받아 두는 1회용 서버.
class _CallbackServer:
    """Tiny one-shot HTTP server that receives Kakao's redirect on localhost."""

    def __init__(self, redirect_uri: str) -> None:
        parsed = urlparse(redirect_uri)
        self.path = parsed.path or "/"
        self.result: dict[str, str] | None = None
        self._done = threading.Event()
        outer = self

        class Handler(BaseHTTPRequestHandler):
            def do_GET(self):  # noqa: N802 (http.server API)
                if urlparse(self.path).path != outer.path:
                    self.send_response(404)
                    self.end_headers()
                    return
                outer.result = parse_callback(self.path)
                ok = "code" in outer.result
                body = (
                    "카카오 로그인 완료! 이 창을 닫고 터미널로 돌아가세요."
                    if ok
                    else "카카오 로그인에 실패했습니다. 터미널 안내를 확인하세요."
                )
                self.send_response(200)
                self.send_header("Content-Type", "text/html; charset=utf-8")
                self.end_headers()
                self.wfile.write(f"<html><body><h2>{body}</h2></body></html>".encode("utf-8"))
                outer._done.set()

            def log_message(self, *args):  # keep the terminal clean
                return

        self.httpd = HTTPServer((parsed.hostname or "localhost", parsed.port or 80), Handler)
        self._thread = threading.Thread(target=self.httpd.serve_forever, daemon=True)

    def __enter__(self) -> _CallbackServer:
        self._thread.start()
        return self

    def wait(self, timeout: float) -> dict[str, str] | None:
        self._done.wait(timeout)
        return self.result

    def __exit__(self, *exc) -> None:
        self.httpd.shutdown()
        self.httpd.server_close()


# --------------------------------------------------------------------------- commands
# [명령들] guide(설명서 출력) / login(로그인 자동화) / test(테스트 메시지). 성공하면 0, 실패하면 1.
def _load_dotenv() -> None:
    try:
        from dotenv import find_dotenv, load_dotenv

        load_dotenv(find_dotenv(usecwd=True))
    except Exception:
        pass


def cmd_guide(out: Callable[[str], None] = print) -> int:
    out(read_guide())
    return 0


# login 순서: 키 확인 → 로컬 서버+브라우저 → code 받기 → 동의·state 확인 → 토큰 교환 → 저장 → 테스트 메시지.
# out/ask/session 을 입력으로 받는 이유: 테스트에서 화면 출력·키보드 입력·카카오 서버를 가짜로 바꾸려고.
def cmd_login(
    *,
    manual: bool = False,
    open_browser: bool = True,
    timeout: float = 180,
    out: Callable[[str], None] = print,
    ask: Callable[[str], str] = input,
    session: Any = None,
) -> int:
    _load_dotenv()
    rest_api_key = os.environ.get("KAKAO_REST_API_KEY", "").strip()
    client_secret = os.environ.get("KAKAO_CLIENT_SECRET", "").strip()
    redirect_uri = get_redirect_uri()

    out("카카오톡 알림 로그인을 시작합니다.\n")
    # 1) REST API 키 확인. 없으면 설명서(guide)를 안내하고 끝낸다.
    if not rest_api_key:
        out("[1/4] REST API 키 확인 ............ ✗ 없음\n")
        out("  먼저 카카오 콘솔 설정(1단계)과 .env 에 키 넣기(2단계)가 필요합니다.")
        out(f"  설정 안내 보기:  {GUIDE_COMMAND}")
        out(f"  카카오 로그인 리다이렉트 URI 로 등록할 주소:  {redirect_uri}")
        return 1
    # 키에 예시 글자(한글)가 남아 있으면 카카오가 무조건 거절하므로 여기서 멈춘다.
    problems, warning = _key_problems(rest_api_key, client_secret)
    if problems:
        out("[1/4] REST API 키 확인 ............ ✗ 키가 이상함\n")
        for problem in problems:
            out(f"  {problem}")
        return 1
    out("[1/4] REST API 키 확인 ............ ✓")
    if warning:
        out(f"      ⚠ {warning}")

    # 2) 로그인 주소 만들기. state = 내가 보낸 요청이 맞는지 확인하는 무작위 값.
    state = secrets.token_urlsafe(16)
    authorize_url = build_authorize_url(rest_api_key, redirect_uri, state)
    use_server = not manual and can_use_local_server(redirect_uri)

    # 3) 로컬 서버로 code 받기. 서버를 못 열거나 https 주소면 '주소 붙여넣기' 방식으로 바꾼다.
    callback: dict[str, str] | None = None
    if use_server:
        try:
            with _CallbackServer(redirect_uri) as server:
                out("[2/4] 브라우저에서 카카오 로그인 화면을 엽니다.")
                out("      로그인 후 '카카오톡 메시지 전송'에 동의해 주세요.")
                out(f"      브라우저가 안 열리면 이 주소를 직접 여세요:\n      {authorize_url}")
                if open_browser:
                    webbrowser.open(authorize_url)
                out(f"      기다리는 중... (최대 {int(timeout)}초)")
                callback = server.wait(timeout)
        except OSError as exc:
            out(f"      로컬 서버를 열 수 없어 수동 입력으로 진행합니다 ({exc}).")
            use_server = False
        if use_server and callback is None:
            out("\n[2/4] 시간 안에 로그인 결과를 받지 못했습니다.")
            out("  로그인 화면에 오류가 보였다면:")
            out("   - KOE004: " + ERROR_HINTS["KOE004"])
            out("   - KOE006: " + ERROR_HINTS["KOE006"].format(redirect_uri=redirect_uri))
            out(f"  해결 후 '{LOGIN_COMMAND}' 를 다시 실행하세요.")
            return 1

    if not use_server:
        out("[2/4] 아래 주소를 브라우저에서 열고 로그인·동의하세요.")
        out(f"      {authorize_url}")
        if open_browser:
            webbrowser.open(authorize_url)
        redirected = ask("      동의 후 바뀐 주소창의 주소 전체를 붙여 넣으세요: ")
        callback = parse_callback(redirected)

    # 4) 돌아온 값 검사: 동의 취소, state 불일치, code 없음.
    if callback.get("error"):
        reason = callback.get("error_description") or callback["error"]
        if callback["error"] == "access_denied":
            reason = "동의를 취소했습니다. 다시 실행해서 '동의하고 계속하기'를 눌러 주세요."
        out(f"\n[2/4] 로그인 실패: {reason}")
        return 1
    if callback.get("state") != state:
        out("\n[2/4] 로그인 응답을 확인할 수 없습니다(state 불일치). 다시 실행하세요.")
        return 1
    if not callback.get("code"):
        out("\n[2/4] 주소에서 code 를 찾지 못했습니다. 동의 후 주소 전체를 붙여 넣었는지 확인하세요.")
        return 1
    out("[2/4] 로그인·동의 확인 ............ ✓")

    try:
        # 5) code → 토큰 (권한 확인 포함)
        tokens = exchange_code(
            callback["code"],
            rest_api_key=rest_api_key,
            redirect_uri=redirect_uri,
            client_secret=client_secret,
            session=session,
        )
    except KakaoSetupError as exc:
        out(f"[3/4] 토큰 발급 ............ ✗\n  {exc}")
        return 1
    except requests.RequestException as exc:
        out(f"[3/4] 토큰 발급 ............ ✗\n  네트워크 오류: {exc}")
        return 1

    provider = KakaoNotificationProvider(session=session)
    # 6) 토큰을 kakao_token.json 에 저장하고 7) 테스트 메시지를 보낸다.
    provider.save_token({"access_token": tokens["access_token"], "refresh_token": tokens.get("refresh_token", "")})
    out(f"[3/4] 토큰 발급·저장 ............ ✓  → {provider.token_file}")

    result = provider.notify(
        title="[Lumibot] 카카오 알림 연결 완료",
        message="이제 .env 에 KAKAO_TRADE_ALERTS=true 를 넣으면 체결 알림을 받을 수 있어요.",
    )
    if not result.ok:
        out(f"[4/4] 테스트 메시지 ............ ✗  {result.reason}")
        hint = explain_reason(result.reason, redirect_uri)
        if hint:
            out(f"  해결 방법: {hint}")
        return 1
    out("[4/4] 테스트 메시지 전송 ............ ✓  카카오톡 '나와의 채팅'을 확인하세요.\n")
    out("완료! .env 에 KAKAO_TRADE_ALERTS=true 를 넣고 전략을 실행하면 체결 알림이 시작됩니다.")
    out(f"이 파일은 GitHub 에 올리지 마세요: {provider.token_file}")
    return 0


# test 순서: 키 검사 → 토큰 확인 → 테스트 메시지 → 실패하면 한국어 해결 방법.
def cmd_test(out: Callable[[str], None] = print, session: Any = None) -> int:
    _load_dotenv()
    provider = KakaoNotificationProvider(session=session)
    problems, warning = _key_problems(provider.rest_api_key, provider.client_secret)
    if problems:
        out("전송 전에 멈췄습니다: .env 의 키가 이상합니다.")
        for problem in problems:
            out(f"  {problem}")
        return 1
    if warning:
        out(f"⚠ {warning}")
    if not provider.is_configured():
        out(f"토큰이 없습니다. 먼저 실행하세요: {LOGIN_COMMAND}")
        return 1
    result = provider.notify(title="[Lumibot] 테스트", message="카카오 알림 연결이 정상입니다.")
    if result.ok:
        out("전송 성공. 카카오톡 '나와의 채팅'을 확인하세요.")
        return 0
    out(f"전송 실패: {result.reason}")
    hint = explain_reason(result.reason, get_redirect_uri())
    if hint:
        out(f"해결 방법: {hint}")
    return 1


# [입구] python -m 으로 직접 실행할 때와 lumibot kakao 명령(cli.py)에서 쓰는 진입점.
def main(argv: list[str] | None = None) -> int:
    import argparse

    parser = argparse.ArgumentParser(prog="lumibot kakao", description="KakaoTalk alert setup")
    parser.add_argument("action", choices=["guide", "login", "test"])
    parser.add_argument("--manual", action="store_true", help="Paste the redirected URL instead of a local server")
    parser.add_argument("--no-browser", action="store_true", help="Do not open the browser automatically")
    parser.add_argument("--timeout", type=float, default=180, help="Seconds to wait for login (default 180)")
    args = parser.parse_args(argv if argv is not None else sys.argv[1:])
    return run(args.action, manual=args.manual, open_browser=not args.no_browser, timeout=args.timeout)


def run(action: str, *, manual: bool = False, open_browser: bool = True, timeout: float = 180) -> int:
    if action == "guide":
        return cmd_guide()
    if action == "login":
        return cmd_login(manual=manual, open_browser=open_browser, timeout=timeout)
    return cmd_test()


if __name__ == "__main__":
    raise SystemExit(main())
