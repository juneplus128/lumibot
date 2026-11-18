"""KIS 어댑터 테스트. 진짜 KIS 서버 대신 가짜 서버(_FakeKis)로 시험한다.

실행: pytest tests/test_kis_adapter.py -q
"""

import json

import pytest

from lumibot.brokers.kis_client import KisClient, KisError, split_account
from lumibot.brokers.krx_rules import normalize_code, round_to_tick, tick_size, to_order_qty


# ---------------------------------------------------------------- 가짜 KIS 서버
class _Res:
    def __init__(self, body, status=200, headers=None):
        self._body = body
        self.status_code = status
        self.headers = headers or {}

    def json(self):
        return self._body


class _FakeKis:
    """경로마다 준비해 둔 응답을 순서대로 돌려주고, 받은 요청을 기록한다."""

    def __init__(self):
        self.routes = {}
        self.calls = []
        self.token_count = 0

    def reply(self, path_end, *responses):
        self.routes.setdefault(path_end, []).extend(responses)

    def _next(self, url):
        for end, queue in self.routes.items():
            if url.endswith(end) and queue:
                return queue.pop(0) if len(queue) > 1 else queue[0]
        raise AssertionError(f"준비 안 된 요청: {url}")

    def post(self, url, headers=None, data=None, timeout=None):
        body = json.loads(data) if data else {}
        if url.endswith("/oauth2/tokenP"):
            self.token_count += 1
            return _Res({"access_token": f"TOKEN{self.token_count}", "token_type": "Bearer",
                         "expires_in": 86400, "access_token_token_expired": "2099-12-31 23:59:59"})
        self.calls.append(("POST", url, dict(headers or {}), body))
        return self._next(url)

    def get(self, url, headers=None, params=None, timeout=None):
        self.calls.append(("GET", url, dict(headers or {}), dict(params or {})))
        return self._next(url)


OK = {"rt_cd": "0", "msg_cd": "MCA00000", "msg1": "정상처리 되었습니다."}


def _client(tmp_path, fake, paper=True):
    return KisClient(app_key="APPKEY1234", app_secret="SECRET", account_no="50123456-01", paper=paper,
                     token_file=tmp_path / "kis_token.json", session=fake, sleep=lambda s: None)


# ---------------------------------------------------------------- 1. 통신 부품
def test_split_account():
    assert split_account("50123456-01") == ("50123456", "01")
    assert split_account("5012345601") == ("50123456", "01")
    assert split_account("50123456", "22") == ("50123456", "22")


def test_paper_converts_tr_id_and_sends_headers(tmp_path):
    fake = _FakeKis()
    fake.reply("/inquire-price", _Res({**OK, "output": {"stck_prpr": "70000"}}))
    c = _client(tmp_path, fake)
    c.request("GET", "/uapi/domestic-stock/v1/quotations/inquire-price", "FHKST01010100", {"X": "1"})
    c.request("GET", "/uapi/domestic-stock/v1/quotations/inquire-price", "FHKST01010100", {"X": "1"})
    _, url, headers, _ = fake.calls[0]
    assert url.startswith("https://openapivts.koreainvestment.com:29443")
    assert headers["authorization"] == "Bearer TOKEN1" and headers["custtype"] == "P"
    assert c.tr_id("TTTC0012U") == "VTTC0012U" and c.tr_id("FHKST01010100") == "FHKST01010100"
    assert fake.token_count == 1                      # 토큰은 한 번만 발급


def test_token_is_reused_from_file(tmp_path):
    fake = _FakeKis()
    fake.reply("/x", _Res(OK))
    _client(tmp_path, fake).request("GET", "/x", "FHKST01010100")
    _client(tmp_path, fake).request("GET", "/x", "FHKST01010100")   # 새 객체여도 파일의 토큰 재사용
    assert fake.token_count == 1
    saved = json.loads((tmp_path / "kis_token.json").read_text(encoding="utf-8"))
    assert "SECRET" not in json.dumps(saved) and saved["app_key_tail"] == "1234"


def test_expired_token_refreshes_once(tmp_path):
    fake = _FakeKis()
    fake.reply("/x", _Res({"rt_cd": "1", "msg_cd": "EGW00123", "msg1": "기간이 만료된 token 입니다."}),
               _Res(OK))
    c = _client(tmp_path, fake)
    c._last_token_issue = None
    c._clock = iter([0, 0, 100, 100, 100, 100]).__next__   # 두 번째 발급이 1분 뒤인 것처럼
    c.request("GET", "/x", "FHKST01010100")
    assert fake.token_count == 2


@pytest.mark.parametrize("code", ["EGW00201", "EGW00215"])
def test_rate_limit_waits_and_retries_once(tmp_path, code):
    # EGW00215 는 2026-11-16 모의투자 주문 테스트에서 잔고 연속 조회 때 실제로 받은 코드
    fake = _FakeKis()
    fake.reply("/x", _Res({"rt_cd": "1", "msg_cd": code, "msg1": "초당 거래건수를 초과하였습니다."}), _Res(OK))
    slept = []
    c = _client(tmp_path, fake)
    c._sleep = slept.append
    c.request("GET", "/x", "FHKST01010100")
    assert len(fake.calls) == 2 and 1.0 in slept


def test_error_raises_kis_error_with_message(tmp_path):
    fake = _FakeKis()
    fake.reply("/order-cash", _Res({"rt_cd": "1", "msg_cd": "APBK0013", "msg1": "주문가능금액을 초과 했습니다"}))
    with pytest.raises(KisError) as err:
        _client(tmp_path, fake).request("POST", "/uapi/domestic-stock/v1/trading/order-cash", "TTTC0012U", {})
    assert err.value.code == "APBK0013" and "주문가능금액" in str(err.value)


def test_request_all_follows_next_page(tmp_path):
    fake = _FakeKis()
    fake.reply("/ccld",
               _Res({**OK, "output1": [{"odno": "1"}], "ctx_area_fk100": "FK", "ctx_area_nk100": "NK"},
                    headers={"tr_cont": "M"}),
               _Res({**OK, "output1": [{"odno": "2"}]}, headers={"tr_cont": "D"}))
    pages = _client(tmp_path, fake).request_all("GET", "/ccld", "TTTC0081R", {"CTX_AREA_FK100": "", "CTX_AREA_NK100": ""})
    assert [p.body["output1"][0]["odno"] for p in pages] == ["1", "2"]
    second = fake.calls[1]
    assert second[2]["tr_cont"] == "N" and second[3]["CTX_AREA_FK100"] == "FK"


# ---------------------------------------------------------------- 2. 한국 규칙
def test_tick_size_table():
    assert [tick_size(p) for p in (1_500, 3_000, 10_000, 30_000, 70_000, 300_000, 800_000)] == [1, 5, 10, 50, 100, 500, 1000]


def test_round_to_tick():
    assert round_to_tick(70_050, "down") == 70_000
    assert round_to_tick(70_050, "up") == 70_100
    assert round_to_tick(1_999.5, "up") == 2_000


def test_normalize_code_and_qty():
    assert normalize_code(5930) == "005930"
    assert normalize_code("005930.KS") == "005930"
    assert to_order_qty(10.0) == 10
    with pytest.raises(ValueError):
        to_order_qty(1.5)


# ---------------------------------------------------------------- 3. 브로커 (Lumibot 연결)
def _broker(tmp_path, fake):
    from lumibot.brokers.kis import KIS
    return KIS(client=_client(tmp_path, fake), connect_stream=False)


BALANCE = {**OK,
           "output1": [{"pdno": "005930", "prdt_name": "삼성전자", "hldg_qty": "10", "ord_psbl_qty": "10",
                        "pchs_avg_pric": "70000.0000", "prpr": "71000"},
                       {"pdno": "000660", "hldg_qty": "0", "ord_psbl_qty": "0", "pchs_avg_pric": "0"}],
           "output2": [{"dnca_tot_amt": "1000000", "prvs_rcdl_excc_amt": "300000",
                        "scts_evlu_amt": "710000", "tot_evlu_amt": "1010000"}]}


def test_balances_and_positions(tmp_path):
    fake = _FakeKis()
    fake.reply("/inquire-balance", _Res(BALANCE))
    broker = _broker(tmp_path, fake)
    assert broker._get_balances_at_broker(None, None) == (300000.0, 710000.0, 1010000.0)
    positions = broker._pull_positions("MyStrategy")
    assert len(positions) == 1
    p = positions[0]
    assert p.asset.symbol == "005930" and float(p.quantity) == 10 and p.avg_fill_price == 70000.0
    assert fake.calls[0][2]["tr_id"] == "VTTC8434R"


def _stock_order(broker, qty=10, limit=70050, side="buy"):
    from lumibot.entities import Asset, Order
    return Order(strategy="MyStrategy", asset=Asset("005930", Asset.AssetType.STOCK), quantity=qty,
                 side=side, limit_price=limit, order_type=Order.OrderType.LIMIT if limit else Order.OrderType.MARKET)


def test_submit_limit_buy_rounds_price_and_remembers_order_no(tmp_path):
    fake = _FakeKis()
    fake.reply("/order-cash", _Res({**OK, "output": {"KRX_FWDG_ORD_ORGNO": "91252", "ODNO": "0000117057",
                                                     "ORD_TMD": "121052"}}))
    broker = _broker(tmp_path, fake)
    order = broker._submit_order(_stock_order(broker))
    _, url, headers, body = fake.calls[0]
    assert headers["tr_id"] == "VTTC0012U"
    assert body["PDNO"] == "005930" and body["ORD_DVSN"] == "00"
    assert body["ORD_QTY"] == "10" and body["ORD_UNPR"] == "70000"      # 70,050 → 호가 단위 내림
    assert order.identifier == "0000117057"
    assert broker._kis_orgno["0000117057"] == "91252"


def test_submit_fractional_qty_is_rejected_without_calling_kis(tmp_path):
    fake = _FakeKis()
    broker = _broker(tmp_path, fake)
    order = broker._submit_order(_stock_order(broker, qty=1.5))
    assert fake.calls == []
    assert order.is_active() is False


def test_kis_rejection_message_reaches_order(tmp_path):
    # 2026-11-16 모의 시나리오 점검에서 발견: 거부 사유가 "Unknown order error" 로 덮어써지던 문제
    fake = _FakeKis()
    fake.reply("/order-cash", _Res({"rt_cd": "1", "msg_cd": "APBK0952", "msg1": "주문가능금액을 초과 했습니다"}))
    broker = _broker(tmp_path, fake)
    order = broker._submit_order(_stock_order(broker))
    assert order.status == "error"
    assert "주문가능금액" in order.error_message and "APBK0952" in order.error_message


def test_market_sell_and_cancel_params(tmp_path):
    fake = _FakeKis()
    fake.reply("/order-cash", _Res({**OK, "output": {"KRX_FWDG_ORD_ORGNO": "91252", "ODNO": "0000200001"}}))
    fake.reply("/order-rvsecncl", _Res({**OK, "output": {"odno": "0000200002"}}))
    broker = _broker(tmp_path, fake)
    order = broker._submit_order(_stock_order(broker, limit=None, side="sell"))
    body = fake.calls[0][3]
    assert fake.calls[0][2]["tr_id"] == "VTTC0011U" and body["ORD_DVSN"] == "01" and body["SLL_TYPE"] == "01"
    broker.cancel_order(order)
    cancel = fake.calls[1][3]
    assert fake.calls[1][2]["tr_id"] == "VTTC0013U"
    assert cancel["ORGN_ODNO"] == "0000200001" and cancel["RVSE_CNCL_DVSN_CD"] == "02"
    assert cancel["KRX_FWDG_ORD_ORGNO"] == "91252" and cancel["QTY_ALL_ORD_YN"] == "Y"


def _ccld(odno, qty, filled, avg, **extra):
    return {"odno": odno, "orgn_odno": "", "pdno": "005930", "prdt_name": "삼성전자", "sll_buy_dvsn_cd": "02",
            "ord_dvsn_cd": "00", "ord_qty": str(qty), "ord_unpr": "70000", "tot_ccld_qty": str(filled),
            "avg_prvs": str(avg), "rmn_qty": str(qty - filled), "cncl_yn": "N", "cnc_cfrm_qty": "0",
            "rjct_qty": "0", "ord_gno_brno": "91252", **extra}


def test_polling_reports_partial_then_full_fill_once_each(tmp_path, monkeypatch):
    fake = _FakeKis()
    fake.reply("/order-cash", _Res({**OK, "output": {"KRX_FWDG_ORD_ORGNO": "91252", "ODNO": "0000300001"}}))
    fake.reply("/inquire-balance", _Res(BALANCE))
    fake.reply("/inquire-daily-ccld",
               _Res({**OK, "output1": [_ccld("0000300001", 10, 3, 70000)]}),
               _Res({**OK, "output1": [_ccld("0000300001", 10, 3, 70000)]}),     # 변화 없음 → 알림 없음
               _Res({**OK, "output1": [_ccld("0000300001", 10, 10, 70030)]}))
    broker = _broker(tmp_path, fake)
    events = []
    real = broker._process_trade_event

    def spy(order, event, price=None, filled_quantity=None, **kw):
        if event in (broker.FILLED_ORDER, broker.PARTIALLY_FILLED_ORDER):
            events.append((event, round(price, 2), float(filled_quantity)))
        return real(order, event, price=price, filled_quantity=filled_quantity, **kw)

    monkeypatch.setattr(broker, "_process_trade_event", spy)
    broker._submit_order(_stock_order(broker, limit=70100))      # 매수 지정가 70,100원
    broker.do_polling()
    broker.do_polling()
    broker.do_polling()
    assert events == [(broker.PARTIALLY_FILLED_ORDER, 70000.0, 3.0),
                      (broker.FILLED_ORDER, 70042.86, 7.0)]       # 7주 몫 평균가 = (70030*10-70000*3)/7


def test_polling_marks_cancel(tmp_path, monkeypatch):
    fake = _FakeKis()
    fake.reply("/order-cash", _Res({**OK, "output": {"KRX_FWDG_ORD_ORGNO": "91252", "ODNO": "0000400001"}}))
    fake.reply("/inquire-balance", _Res(BALANCE))
    fake.reply("/inquire-daily-ccld", _Res({**OK, "output1": [
        _ccld("0000400001", 10, 0, 0, cnc_cfrm_qty="10", rmn_qty="0"),
        _ccld("0000400002", 10, 0, 0, orgn_odno="0000400001", cncl_yn="Y"),
    ]}))
    broker = _broker(tmp_path, fake)
    seen = []
    real = broker._process_trade_event
    monkeypatch.setattr(broker, "_process_trade_event",
                        lambda order, event, **kw: (seen.append(event), real(order, event, **kw))[1])
    order = broker._submit_order(_stock_order(broker, limit=70000))
    broker.do_polling()
    assert broker.CANCELED_ORDER in seen
    assert order.is_active() is False


# ---------------------------------------------------------------- 4. 시세
def test_kis_data_last_price_and_daily_bars(tmp_path):
    from lumibot.data_sources.kis_data import KisData
    fake = _FakeKis()
    fake.reply("/inquire-price", _Res({**OK, "output": {"stck_prpr": "71000", "stck_mxpr": "92300",
                                                       "stck_llam": "49700", "aspr_unit": "100"}}))
    days = [{"stck_bsop_date": f"202611{d:02d}", "stck_oprc": "70000", "stck_hgpr": "71000",
             "stck_lwpr": "69000", "stck_clpr": str(70000 + d), "acml_vol": "1000"} for d in range(10, 2, -1)]
    fake.reply("/inquire-daily-itemchartprice", _Res({**OK, "output1": {}, "output2": days}))
    data = KisData(client=_client(tmp_path, fake))
    assert data.get_last_price("005930") == 71000.0
    assert data.get_price_limits("005930") == (49700.0, 92300.0, 100)
    bars = data.get_historical_prices("005930", 5, "day")
    df = bars.df
    assert len(df) == 5 and list(df["close"]) == [70006.0, 70007.0, 70008.0, 70009.0, 70010.0]
    # 미국 시간으로 바뀌어도 날짜가 하루 밀리지 않아야 한다 (11월 10일 봉 → 11월 10일)
    assert df.index[-1].strftime("%Y-%m-%d") == "2026-11-10"


# ---------------------------------------------------------------- 5. 원화(KRW) 기본 통화
def test_strategy_with_kis_uses_krw_by_default(tmp_path):
    from lumibot.strategies import Strategy

    class _Dummy(Strategy):
        def on_trading_iteration(self):
            pass

    fake = _FakeKis()
    fake.reply("/inquire-balance", _Res(BALANCE))   # 전략을 만들 때 잔고를 한 번 조회한다
    broker = _broker(tmp_path, fake)
    strategy = _Dummy(broker=broker)
    assert strategy.quote_asset.symbol == "KRW"


# ---------------------------------------------------------------- 6. 호가 (get_quote)
ASKING = {**OK,
          # 2026-11-16 모의서버 실제 응답의 항목 이름 그대로 (output2 에는 누적 거래량이 없다)
          "output1": {"askp1": "198600", "bidp1": "198500", "askp_rsqn1": "1354", "bidp_rsqn1": "9932",
                      "aspr_acpt_hour": "105810"},
          "output2": {"stck_prpr": "198500", "stck_sdpr": "200000", "stck_shrn_iscd": "005930"}}


def test_kis_data_get_quote(tmp_path):
    from lumibot.data_sources.kis_data import KisData
    fake = _FakeKis()
    fake.reply("/inquire-asking-price-exp-ccn", _Res(ASKING))
    q = KisData(client=_client(tmp_path, fake)).get_quote("5930")
    _, url, headers, params = fake.calls[0]
    assert headers["tr_id"] == "FHKST01010200" and params["FID_INPUT_ISCD"] == "005930"
    assert (q.bid, q.ask, q.bid_size, q.ask_size, q.price) == (198500.0, 198600.0, 9932.0, 1354.0, 198500.0)
    assert q.mid_price == 198550.0
    assert q.change == -1500.0 and q.percent_change == -0.75
    assert q.timestamp.strftime("%H:%M:%S") == "10:58:10"


def test_kis_data_get_quote_error_returns_none(tmp_path):
    from lumibot.data_sources.kis_data import KisData
    fake = _FakeKis()
    fake.reply("/inquire-asking-price-exp-ccn", _Res({"rt_cd": "1", "msg_cd": "X", "msg1": "실패"}))
    assert KisData(client=_client(tmp_path, fake)).get_quote("005930") is None


# ---------------------------------------------------------------- 7. 정정 주문
def test_modify_limit_order_gets_new_order_no_and_keeps_tracking(tmp_path, monkeypatch):
    fake = _FakeKis()
    fake.reply("/order-cash", _Res({**OK, "output": {"KRX_FWDG_ORD_ORGNO": "91252", "ODNO": "0000500001"}}))
    fake.reply("/order-rvsecncl", _Res({**OK, "output": {"KRX_FWDG_ORD_ORGNO": "91252", "ODNO": "0000500009"}}))
    fake.reply("/inquire-balance", _Res(BALANCE))
    fake.reply("/inquire-daily-ccld", _Res({**OK, "output1": [
        _ccld("0000500001", 1, 0, 0, rmn_qty="0"),                               # 원주문 (정정되어 잔량 0)
        _ccld("0000500009", 1, 1, 70500, orgn_odno="0000500001", ord_unpr="70500"),  # 정정 주문 체결
    ]}))
    broker = _broker(tmp_path, fake)
    events = []
    real = broker._process_trade_event
    monkeypatch.setattr(broker, "_process_trade_event",
                        lambda order, event, **kw: (events.append(event), real(order, event, **kw))[1])
    order = broker._submit_order(_stock_order(broker, qty=1, limit=70000))
    broker.modify_order(order, limit_price=70520)            # 매수 → 호가 단위(100원)로 내림 = 70,500
    _, _, headers, body = fake.calls[1]
    assert headers["tr_id"] == "VTTC0013U"
    assert body["RVSE_CNCL_DVSN_CD"] == "01" and body["ORD_UNPR"] == "70500"
    assert body["ORGN_ODNO"] == "0000500001" and body["KRX_FWDG_ORD_ORGNO"] == "91252"
    assert order.identifier == "0000500009" and order.previous_identifiers == ["0000500001"]
    assert order.limit_price == 70500
    broker.do_polling()
    assert broker.FILLED_ORDER in events and order.is_filled()
    assert broker.CANCELED_ORDER not in events                # 원주문 줄 때문에 취소로 오해하지 않는다


def test_modify_refuses_market_and_partially_filled_orders(tmp_path):
    from lumibot.brokers.broker import LumibotBrokerAPIError
    fake = _FakeKis()
    fake.reply("/order-cash", _Res({**OK, "output": {"KRX_FWDG_ORD_ORGNO": "91252", "ODNO": "0000600001"}}))
    broker = _broker(tmp_path, fake)
    market = broker._submit_order(_stock_order(broker, qty=1, limit=None))
    with pytest.raises(LumibotBrokerAPIError):
        broker.modify_order(market, limit_price=70000)
    limit = broker._submit_order(_stock_order(broker, qty=10, limit=70000))
    limit.status = "partially_filled"
    with pytest.raises(LumibotBrokerAPIError):
        broker.modify_order(limit, limit_price=70100)
    assert not any(c[1].endswith("/order-rvsecncl") for c in fake.calls)   # KIS 에는 보내지 않았다


# ---------------------------------------------------------------- 8. .env 자동 설정 (TRADING_BROKER=kis)
def test_paper_flag_accepts_text(tmp_path):
    fake = _FakeKis()
    assert KisClient(paper="false", session=fake, app_key="A", app_secret="B", account_no="1").paper is False
    assert KisClient(paper="true", session=fake, app_key="A", app_secret="B", account_no="1").paper is True


@pytest.mark.parametrize("name", ["kis", "KIS", "koreainvestment"])
def test_trading_broker_env_selects_kis(monkeypatch, name):
    import lumibot.credentials as cred
    made = {}

    class _FakeBroker:
        name = "KIS"
        client = object()

        def __init__(self, config, connect_stream=True):
            made["config"] = config

    monkeypatch.setattr(cred, "trading_broker_name", name)
    monkeypatch.setattr(cred, "data_source_name", "kis")
    monkeypatch.setattr(cred, "_broker_class", lambda cls_name: _FakeBroker if cls_name == "KIS" else None)
    monkeypatch.setattr(cred, "KIS_CONFIG", {"APP_KEY": "k", "APP_SECRET": "s", "ACCOUNT_NO": "1", "IS_PAPER": True})
    broker, data_source = cred._build_default_live_credentials()
    assert isinstance(broker, _FakeBroker) and made["config"]["IS_PAPER"] is True
    assert type(data_source).__name__ == "KisData" and data_source.client is _FakeBroker.client


# ---------------------------------------------------------------- 9. 한국거래소 장 시간 (폐장 15:30)
def test_krx_market_hours_close_is_1530_not_lunch_break(tmp_path):
    # 2026-11-18 팀원 김지용 님이 발견: XKRX 달력 열 순서가 market_open, break_start, break_end, market_close 라서
    # 본체가 1번째 열(break_start=12:00)을 폐장으로 읽던 문제. 오후에 전략이 '장 마감'으로 착각할 수 있었다.
    import datetime as dt
    from zoneinfo import ZoneInfo
    kst = ZoneInfo("Asia/Seoul")
    broker = _broker(tmp_path, _FakeKis())
    day = dt.date(2026, 11, 19)
    assert broker.market_hours(close=False, date=day).astimezone(kst).strftime("%H:%M") == "09:00"
    assert broker.market_hours(close=True, date=day).astimezone(kst).strftime("%H:%M") == "15:30"


# ---------------------------------------------------------------- 10. 분봉 (오늘 정규장만)
def _minute(hhmm, price, vol, date="20261119"):
    return {"stck_bsop_date": date, "stck_cntg_hour": f"{hhmm}00", "stck_prpr": str(price),
            "stck_oprc": str(price - 100), "stck_hgpr": str(price + 100), "stck_lwpr": str(price - 200),
            "cntg_vol": str(vol)}


def _fixed_now(monkeypatch, hh, mm):
    import datetime as real
    import lumibot.data_sources.kis_data as mod

    class _Fixed(real.datetime):
        @classmethod
        def now(cls, tz=None):
            return real.datetime(2026, 11, 19, hh, mm, tzinfo=tz)

    monkeypatch.setattr(mod, "datetime", _Fixed)


def test_minute_bars_after_close_start_at_1530_and_drop_filled_rows(tmp_path, monkeypatch):
    from lumibot.data_sources.kis_data import KisData
    _fixed_now(monkeypatch, 20, 20)                        # 장 마감 뒤 (저녁 8시 20분)
    fake = _FakeKis()
    fake.reply("/inquire-time-itemchartprice", _Res({**OK, "output2": [
        _minute("1530", 70500, 900), _minute("1529", 70400, 0),   # 15:29 는 거래 없음 → KIS 가 채운 줄
        _minute("1528", 70300, 120), _minute("1527", 70200, 80),
    ]}))
    bars = KisData(client=_client(tmp_path, fake)).get_historical_prices("005930", 10, "minute")
    _, _, headers, params = fake.calls[0]
    assert headers["tr_id"] == "FHKST03010200"
    assert params["FID_INPUT_HOUR_1"] == "153000"          # 지금 시각(20:20)이 아니라 15:30 부터
    df = bars.df
    assert list(df["close"]) == [70200.0, 70300.0, 70500.0]  # 거래량 0 줄 제거, 시간 순
    assert df.index[-1].tz_convert("Asia/Seoul").strftime("%H:%M") == "15:30"


def test_minute_bars_page_backwards_and_build_5_minute_bars(tmp_path, monkeypatch):
    from lumibot.data_sources.kis_data import KisData
    _fixed_now(monkeypatch, 9, 10)
    fake = _FakeKis()
    page1 = [_minute(f"09{m:02d}", 70000 + m * 10, 10) for m in range(10, 4, -1)]   # 09:10 ~ 09:05
    page2 = [_minute(f"09{m:02d}", 70000 + m * 10, 10) for m in range(4, -1, -1)]   # 09:04 ~ 09:00
    fake.reply("/inquire-time-itemchartprice", _Res({**OK, "output2": page1}), _Res({**OK, "output2": page2}))
    bars = KisData(client=_client(tmp_path, fake)).get_historical_prices("005930", 2, "5 minutes")
    assert fake.calls[1][3]["FID_INPUT_HOUR_1"] == "090400"   # 두 번째 요청은 받은 것 중 가장 이른 시각 - 1분
    df = bars.df
    kst_times = [t.tz_convert("Asia/Seoul").strftime("%H:%M") for t in df.index]
    assert kst_times == ["09:05", "09:10"]                     # 마지막 2개 (09:00, 09:05, 09:10 중)
    assert df.iloc[0]["open"] == 70050 - 100 and df.iloc[0]["close"] == 70090   # 09:05~09:09 묶음
    assert df.iloc[0]["volume"] == 50


def test_minute_bars_before_open_returns_none_without_calling_kis(tmp_path, monkeypatch):
    from lumibot.data_sources.kis_data import KisData
    _fixed_now(monkeypatch, 8, 30)
    fake = _FakeKis()
    assert KisData(client=_client(tmp_path, fake)).get_historical_prices("005930", 5, "minute") is None
    assert fake.calls == []


def test_unsupported_timestep_returns_none(tmp_path):
    from lumibot.data_sources.kis_data import KisData
    fake = _FakeKis()
    assert KisData(client=_client(tmp_path, fake)).get_historical_prices("005930", 5, "hour") is None
    assert fake.calls == []
