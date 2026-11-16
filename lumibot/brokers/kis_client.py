"""KIS(한국투자증권) Open API 통신 부품.

KIS 서버와 주고받는 일은 전부 이 파일이 맡는다. 브로커(kis.py)와 시세(kis_data.py)는
``KisClient.request()`` 만 부르고, 토큰·헤더·모의/실전 전환·호출 간격·다음 페이지는 신경 쓰지 않는다.

참고: 한국투자증권 공식 예제 https://github.com/koreainvestment/open-trading-api
(examples_llm/kis_auth.py 의 auth(), _url_fetch() 와 같은 일을 한다)

.env 에 넣는 값
    KIS_APP_KEY        앱키 (모의투자용 / 실전용이 따로 있음)
    KIS_APP_SECRET     앱시크릿
    KIS_ACCOUNT_NO     계좌번호. "50123456-01" 또는 앞 8자리만 (뒤 2자리는 KIS_ACCOUNT_PRODUCT, 기본 01)
    KIS_IS_PAPER       true 면 모의투자 (기본 true)
    KIS_TOKEN_FILE     토큰 저장 파일 (기본 kis_token_paper.json / kis_token_real.json)
"""

from __future__ import annotations

import json
import os
import threading
import time
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any, Callable

import requests

# [서버 주소] KIS 공식 예제 kis_devlp.yaml 의 prod / vps 값
REAL_URL = "https://openapi.koreainvestment.com:9443"
PAPER_URL = "https://openapivts.koreainvestment.com:29443"
TOKEN_PATH = "/oauth2/tokenP"

# [에러 코드] 토큰 만료, 초당 호출 한도 초과 (KIS 예제에서 확인한 코드)
TOKEN_EXPIRED_CODES = {"EGW00123", "EGW00121"}
# EGW00215: 모의투자에서 실제로 받은 "원장 초당 거래건수 초과" (2026-11-16 모의 주문 테스트)
RATE_LIMIT_CODES = {"EGW00201", "EGW00215"}


# KIS 가 실패를 돌려줬을 때 쓰는 에러. 코드와 KIS 의 한국어 메시지를 그대로 담는다 (키·토큰은 담지 않는다).
class KisError(Exception):
    def __init__(self, code: str | None, message: str | None, *, tr_id: str | None = None, status: int | None = None):
        self.code = code or ""
        self.message = message or ""
        self.tr_id = tr_id
        self.status = status
        text = f"[KIS {self.code}] {self.message}".strip()
        if tr_id:
            text += f" (tr_id={tr_id})"
        super().__init__(text)


# 응답 하나: 본문(body)과 헤더(다음 페이지 여부 tr_cont 가 헤더에 온다)
@dataclass
class KisResponse:
    body: dict
    headers: dict = field(default_factory=dict)

    @property
    def has_next(self) -> bool:
        return str(self.headers.get("tr_cont", "")).upper() in ("M", "F")


def _env_flag(name: str, default: bool) -> bool:
    value = os.environ.get(name)
    if value is None or value.strip() == "":
        return default
    return value.strip().lower() in ("1", "true", "yes", "on")


# "50123456-01" → ("50123456", "01"), "50123456" → ("50123456", 기본 상품코드)
def split_account(account_no: str | None, product: str | None = None) -> tuple[str, str]:
    raw = (account_no or "").strip().replace(" ", "")
    if "-" in raw:
        cano, prdt = raw.split("-", 1)
    elif len(raw) == 10 and raw.isdigit():
        cano, prdt = raw[:8], raw[8:]
    else:
        cano, prdt = raw, ""
    return cano, (prdt or product or "01")


class KisClient:
    """KIS 서버에 요청을 보내는 부품. 테스트 때는 session 에 가짜 서버를 넣는다."""

    def __init__(
        self,
        *,
        app_key: str | None = None,
        app_secret: str | None = None,
        account_no: str | None = None,
        account_product: str | None = None,
        paper: bool | None = None,
        token_file: str | os.PathLike | None = None,
        session: Any = None,
        timeout: float = 10,
        min_interval: float | None = None,
        clock: Callable[[], float] = time.monotonic,
        sleep: Callable[[float], None] = time.sleep,
    ):
        self.paper = _env_flag("KIS_IS_PAPER", True) if paper is None else bool(paper)
        self.base_url = PAPER_URL if self.paper else REAL_URL
        self.app_key = (app_key or os.environ.get("KIS_APP_KEY", "")).strip()
        self.app_secret = (app_secret or os.environ.get("KIS_APP_SECRET", "")).strip()
        self.cano, self.product = split_account(
            account_no or os.environ.get("KIS_ACCOUNT_NO"),
            account_product or os.environ.get("KIS_ACCOUNT_PRODUCT"),
        )
        default_token = "kis_token_paper.json" if self.paper else "kis_token_real.json"
        self.token_file = Path(token_file or os.environ.get("KIS_TOKEN_FILE") or default_token)
        self.http = session or requests
        self.timeout = timeout
        # [호출 간격] KIS 예제의 smart_sleep 값: 실전 0.05초, 모의 0.5초
        self.min_interval = (0.5 if self.paper else 0.05) if min_interval is None else min_interval
        self._clock = clock
        self._sleep = sleep
        self._lock = threading.RLock()
        self._last_call = None
        self._last_token_issue = None
        self._token: dict | None = None

    # ------------------------------------------------------------------ 설정 확인
    def is_configured(self) -> bool:
        return bool(self.app_key and self.app_secret and self.cano)

    def account_params(self) -> dict:
        """주문·잔고 요청에 공통으로 들어가는 계좌 값"""
        return {"CANO": self.cano, "ACNT_PRDT_CD": self.product}

    # [모의투자 코드] 실전 tr_id 첫 글자가 T/J/C 면 V 로 바꾼다 (KIS 예제 _url_fetch 와 같은 규칙)
    def tr_id(self, real_tr_id: str) -> str:
        if self.paper and real_tr_id and real_tr_id[0] in ("T", "J", "C"):
            return "V" + real_tr_id[1:]
        return real_tr_id

    # ------------------------------------------------------------------ 토큰
    # 토큰은 1일 유효, 1분에 1번만 발급, 발급할 때마다 알림톡이 온다 → 파일에 저장해 두고 다시 쓴다.
    def _load_token(self) -> dict | None:
        if self._token:
            return self._token
        try:
            with open(self.token_file, encoding="utf-8") as f:
                data = json.load(f)
        except (OSError, ValueError):
            return None
        # 다른 앱키로 받은 토큰은 쓰지 않는다
        if data.get("app_key_tail") != self.app_key[-4:]:
            return None
        self._token = data
        return data

    def _save_token(self, data: dict) -> None:
        self._token = data
        try:
            with open(self.token_file, "w", encoding="utf-8") as f:
                json.dump(data, f, ensure_ascii=False)
            os.chmod(self.token_file, 0o600)
        except OSError:
            pass  # 저장을 못 해도 이번 실행에서는 메모리의 토큰을 쓴다

    @staticmethod
    def _is_fresh(data: dict | None, now: datetime) -> bool:
        if not data or not data.get("access_token") or not data.get("expires_at"):
            return False
        try:
            expires = datetime.strptime(data["expires_at"], "%Y-%m-%d %H:%M:%S")
        except ValueError:
            return False
        return expires - now > timedelta(minutes=10)  # 만료 10분 전에 미리 새로 받는다

    def token(self, *, force: bool = False) -> str:
        with self._lock:
            data = self._load_token()
            if not force and self._is_fresh(data, datetime.now()):
                return data["access_token"]
            if not self.is_configured():
                raise KisError("CONFIG", "KIS_APP_KEY, KIS_APP_SECRET, KIS_ACCOUNT_NO 를 .env 에 넣어 주세요.")
            now = self._clock()
            if self._last_token_issue is not None and now - self._last_token_issue < 60:
                raise KisError("TOKEN_RATE", "토큰은 1분에 1번만 발급할 수 있습니다. 잠시 후 다시 시도하세요.")
            self._last_token_issue = now
            try:
                res = self.http.post(
                    self.base_url + TOKEN_PATH,
                    headers={"content-type": "application/json"},
                    data=json.dumps({"grant_type": "client_credentials",
                                     "appkey": self.app_key, "appsecret": self.app_secret}),
                    timeout=self.timeout,
                )
            except requests.RequestException as exc:
                raise KisError("NETWORK", f"KIS 서버에 연결하지 못했습니다: {exc.__class__.__name__}") from exc
            try:
                body = res.json()
            except ValueError:
                body = {}
            access = body.get("access_token")
            if not access:
                raise KisError(body.get("error_code") or str(res.status_code),
                               body.get("error_description") or "토큰 발급에 실패했습니다. 앱키·앱시크릿을 확인하세요.")
            expires_at = body.get("access_token_token_expired")
            if not expires_at:
                seconds = int(body.get("expires_in") or 86400)
                expires_at = (datetime.now() + timedelta(seconds=seconds)).strftime("%Y-%m-%d %H:%M:%S")
            self._save_token({"access_token": access, "expires_at": expires_at,
                              "app_key_tail": self.app_key[-4:], "paper": self.paper})
            return access

    # ------------------------------------------------------------------ 요청
    def _wait_turn(self) -> None:
        with self._lock:
            now = self._clock()
            if self._last_call is not None:
                gap = self.min_interval - (now - self._last_call)
                if gap > 0:
                    self._sleep(gap)
                    now = self._clock()
            self._last_call = now

    def request(self, method: str, path: str, tr_id: str, params: dict | None = None, *,
                tr_cont: str = "", _retried: bool = False) -> KisResponse:
        """KIS 에 요청 1번. 실패하면 KisError. 토큰 만료·호출 한도 초과는 한 번 다시 시도한다."""
        method = method.upper()
        real_tr = tr_id
        headers = {
            "content-type": "application/json; charset=utf-8",
            "authorization": f"Bearer {self.token()}",
            "appkey": self.app_key,
            "appsecret": self.app_secret,
            "tr_id": self.tr_id(tr_id),
            "custtype": "P",          # 개인
            "tr_cont": tr_cont,       # 다음 페이지 요청이면 "N"
        }
        self._wait_turn()
        url = self.base_url + path
        try:
            if method == "POST":
                # POST 본문의 키는 대문자, 숫자도 문자열로 보내야 한다 (KIS 안내)
                res = self.http.post(url, headers=headers, data=json.dumps(params or {}), timeout=self.timeout)
            else:
                res = self.http.get(url, headers=headers, params=params or {}, timeout=self.timeout)
        except requests.RequestException as exc:
            raise KisError("NETWORK", f"KIS 서버에 연결하지 못했습니다: {exc.__class__.__name__}",
                           tr_id=headers["tr_id"]) from exc

        try:
            body = res.json()
        except ValueError:
            body = {}
        code = body.get("msg_cd") or body.get("error_code")

        if not _retried and (res.status_code in (401, 403) or code in TOKEN_EXPIRED_CODES):
            self._token = None
            self.token(force=True)
            return self.request(method, path, real_tr, params, tr_cont=tr_cont, _retried=True)
        if not _retried and code in RATE_LIMIT_CODES:
            self._sleep(1.0)
            return self.request(method, path, real_tr, params, tr_cont=tr_cont, _retried=True)

        if res.status_code != 200 or str(body.get("rt_cd", "")) != "0":
            raise KisError(code or str(res.status_code), body.get("msg1") or body.get("error_description"),
                           tr_id=headers["tr_id"], status=res.status_code)
        return KisResponse(body=body, headers=dict(getattr(res, "headers", {}) or {}))

    def request_all(self, method: str, path: str, tr_id: str, params: dict, *, max_pages: int = 20) -> list[KisResponse]:
        """다음 페이지까지 모두 받기. 응답 헤더 tr_cont 가 M/F 면 ctx_area 값을 넣어 다시 요청한다."""
        pages: list[KisResponse] = []
        params = dict(params)
        cont = ""
        for _ in range(max_pages):
            res = self.request(method, path, tr_id, params, tr_cont=cont)
            pages.append(res)
            if not res.has_next:
                break
            params["CTX_AREA_FK100"] = res.body.get("ctx_area_fk100", "")
            params["CTX_AREA_NK100"] = res.body.get("ctx_area_nk100", "")
            cont = "N"
        return pages
