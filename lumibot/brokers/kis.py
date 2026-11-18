"""KIS(한국투자증권) 브로커: Lumibot 과 한국투자증권 Open API 를 연결한다.

example_broker.py(빈 틀)의 메소드를 KIS API 로 채운 파일이다. 체결은 KIS 가 알려주지 않으므로
Tradier 처럼 몇 초마다 '일별 주문 체결 조회'를 해서 늘어난 체결 수량을 엔진에 알린다
(do_polling). 엔진이 체결 이벤트를 받으면 on_filled_order() 와 카카오 알림이 그대로 동작한다.

지원 범위: 국내 주식, 지정가·시장가, 매수·매도, 취소, 지정가 정정(부분 체결 전). 모의투자로 검증.
지원 안 함: 손절·bracket 같은 복합 주문, 공매도, 해외 주식.

사용 예
    # 1) .env 에 TRADING_BROKER=kis 를 넣으면 Strategy() 가 자동으로 KIS 를 쓴다
    # 2) 또는 코드에서 직접
    from lumibot.brokers import KIS
    broker = KIS()                      # .env 의 KIS_APP_KEY 등을 읽음 (모의투자 기본)
    strategy = MyStrategy(broker=broker)
"""

from __future__ import annotations

import os
from datetime import datetime
from decimal import Decimal, InvalidOperation
from zoneinfo import ZoneInfo

from lumibot.entities import Asset, Order, Position
from lumibot.tools.lumibot_logger import get_logger

from .broker import Broker, LumibotBrokerAPIError
from .kis_client import KisClient, KisError
from .krx_rules import normalize_code, round_to_tick, to_order_qty

logger = get_logger(__name__)

KST = ZoneInfo("Asia/Seoul")

# [KIS API 경로와 거래 코드] 실전 코드 기준. 모의투자면 KisClient 가 첫 글자를 V 로 바꾼다.
PATH_ORDER = "/uapi/domestic-stock/v1/trading/order-cash"           # 현금 주문
PATH_CANCEL = "/uapi/domestic-stock/v1/trading/order-rvsecncl"      # 정정·취소
PATH_BALANCE = "/uapi/domestic-stock/v1/trading/inquire-balance"    # 잔고
PATH_CCLD = "/uapi/domestic-stock/v1/trading/inquire-daily-ccld"    # 일별 주문 체결 조회
TR_BUY, TR_SELL = "TTTC0012U", "TTTC0011U"
TR_CANCEL = "TTTC0013U"
TR_BALANCE = "TTTC8434R"
TR_CCLD = "TTTC0081R"           # 3개월 이내

ORD_LIMIT, ORD_MARKET = "00", "01"   # 주문구분: 지정가 / 시장가
SIDE_SELL, SIDE_BUY = "01", "02"     # 체결 조회의 매도매수구분코드


def _first_set(*values):
    """None 이 아닌 첫 값 (명시한 인자 > config > 환경변수 순서)"""
    return next((v for v in values if v is not None), None)


def _truthy(value) -> bool:
    if isinstance(value, str):
        return value.strip().lower() in ("1", "true", "yes", "on")
    return bool(value)


def _num(value, default=0.0) -> float:
    try:
        return float(str(value).replace(",", ""))
    except (TypeError, ValueError):
        return default


def _dec(value) -> Decimal:
    try:
        return Decimal(str(value).replace(",", "") or "0")
    except (InvalidOperation, TypeError):
        return Decimal(0)


class KIS(Broker):
    """한국투자증권 Open API 브로커 (국내 주식)"""

    NAME = "KIS"
    POLL_EVENT = "poll"
    # 전략이 통화를 따로 정하지 않으면 원화(KRW)로 계산한다 (Lumibot 기본값은 USD)
    DEFAULT_QUOTE_SYMBOL = "KRW"

    def __init__(self, config=None, *, client: KisClient | None = None, data_source=None, paper=None,
                 allow_real=None, polling_interval: float = 5.0, connect_stream: bool = True, max_workers: int = 1):
        config = dict(config or {})
        # [연결 준비] config 가 있으면 그 값을, 없으면 .env(KIS_*) 값을 쓴다
        self.client = client or KisClient(
            app_key=config.get("APP_KEY"),
            app_secret=config.get("APP_SECRET"),
            account_no=config.get("ACCOUNT_NO"),
            paper=config.get("IS_PAPER", paper),
        )
        # [실전 이중 잠금] 실전 서버(진짜 돈)는 KIS_IS_PAPER=false 하나만으로는 열리지 않는다.
        # KIS_ALLOW_REAL=true 를 한 번 더 적어야 한다 (팀원 패치 kis_adapter_3 의 ALLOW_REAL 아이디어).
        if not self.client.paper and not _truthy(_first_set(allow_real, config.get("ALLOW_REAL"),
                                                            os.environ.get("KIS_ALLOW_REAL"))):
            raise ValueError(
                "KIS 실전 서버는 진짜 돈이 움직입니다. 실전으로 쓰려면 KIS_IS_PAPER=false 와 함께 "
                "KIS_ALLOW_REAL=true 도 설정하세요. (모의투자는 KIS_IS_PAPER=true)"
            )
        # 체결 조회 간격. 모의투자는 초당 호출 한도가 낮아서 너무 짧게 하지 않는다.
        self.polling_interval = polling_interval
        self._kis_orgno: dict[str, str] = {}     # 주문번호 → 주문 조직번호 (취소할 때 필요)
        self._kis_ord_dvsn: dict[str, str] = {}  # 주문번호 → 지정가/시장가 구분
        self._poll_count = 0

        if data_source is None:
            from lumibot.data_sources.kis_data import KisData
            data_source = KisData(client=self.client)

        config.setdefault("MARKET", "XKRX")      # 한국거래소 장 시간·휴장일 달력
        super().__init__(name=self.NAME, data_source=data_source, config=config,
                         max_workers=max_workers, connect_stream=connect_stream)
        self.market = "XKRX"

    # ================================================================== 계좌
    def _balance(self) -> tuple[list[dict], dict]:
        """잔고 조회 → (종목별 줄 목록, 계좌 합계 한 줄)"""
        params = {
            **self.client.account_params(),
            "AFHR_FLPR_YN": "N", "OFL_YN": "", "INQR_DVSN": "02", "UNPR_DVSN": "01",
            "FUND_STTL_ICLD_YN": "N", "FNCG_AMT_AUTO_RDPT_YN": "N", "PRCS_DVSN": "00",
            "CTX_AREA_FK100": "", "CTX_AREA_NK100": "",
        }
        pages = self.client.request_all("GET", PATH_BALANCE, TR_BALANCE, params)
        rows: list[dict] = []
        summary: dict = {}
        for page in pages:
            rows.extend(page.body.get("output1") or [])
            out2 = page.body.get("output2")
            if isinstance(out2, list) and out2:
                summary = out2[0]
            elif isinstance(out2, dict):
                summary = out2
        return rows, summary

    def _get_balances_at_broker(self, quote_asset: Asset, strategy):
        """(현금, 주식 평가금액, 총 평가금액). 현금은 결제(D+2) 기준 예수금."""
        try:
            _, s = self._balance()
        except KisError as e:
            logger.error(f"KIS 잔고 조회 실패: {e}")
            return None
        cash = _num(s.get("prvs_rcdl_excc_amt")) or _num(s.get("dnca_tot_amt"))
        positions_value = _num(s.get("scts_evlu_amt"))
        total = _num(s.get("tot_evlu_amt")) or cash + positions_value
        return cash, positions_value, total

    def _pull_positions(self, strategy) -> list[Position]:
        try:
            rows, _ = self._balance()
        except KisError as e:
            logger.error(f"KIS 보유 종목 조회 실패: {e}")
            return []
        # 주의: Broker 본체에 self._strategy_name(문자열) 변수가 있어서 같은 이름의 함수를 만들면 충돌한다
        name = self._strategy_name_from_input(strategy) or "Unknown"
        positions = []
        for row in rows:
            qty = int(_num(row.get("hldg_qty")))
            if qty <= 0:
                continue
            positions.append(Position(
                strategy=name,
                asset=Asset(normalize_code(row.get("pdno")), Asset.AssetType.STOCK),
                quantity=qty,
                available=int(_num(row.get("ord_psbl_qty"))),
                avg_fill_price=_num(row.get("pchs_avg_pric")) or None,
            ))
        return positions

    def _pull_position(self, strategy, asset: Asset):
        code = normalize_code(asset.symbol)
        for position in self._pull_positions(strategy):
            if position.asset.symbol == code:
                return position
        return None

    def get_historical_account_value(self) -> dict:
        # KIS 에는 계좌 가치 이력 API 가 없어서 비워 둔다
        return {"hourly": None, "daily": None}

    # ================================================================== 주문
    def _is_etf(self, asset) -> bool:
        """ETF·ETN 이면 호가 단위가 다르다. 시세 부품이 KisData 가 아니면(직접 넣은 경우) 주식으로 본다."""
        checker = getattr(self.data_source, "is_etf", None)
        return bool(checker(asset)) if callable(checker) else False

    def _submit_order(self, order: Order) -> Order:
        # 1) 지원하는 주문인지 확인
        problem = None
        if order.asset is None or order.asset.asset_type != Asset.AssetType.STOCK:
            problem = "KIS 어댑터는 국내 주식만 지원합니다."
        elif order.order_type not in (Order.OrderType.MARKET, Order.OrderType.LIMIT):
            problem = f"KIS 어댑터는 시장가·지정가만 지원합니다 (요청: {order.order_type})."
        elif order.order_class not in (None, Order.OrderClass.SIMPLE):
            problem = "KIS 어댑터는 손절·bracket 같은 복합 주문을 지원하지 않습니다."
        elif order.side not in (Order.OrderSide.BUY, Order.OrderSide.SELL):
            problem = f"KIS 어댑터는 매수·매도만 지원합니다 (요청: {order.side})."
        if problem is None:
            try:
                qty = to_order_qty(order.quantity)
            except ValueError as e:
                problem = str(e)
        if problem:
            self._dispatch(self.ERROR_ORDER, order=order, error_msg=problem)
            return order

        # 2) 한국 규칙에 맞춰 주문 값 만들기
        is_buy = order.side == Order.OrderSide.BUY
        if order.order_type == Order.OrderType.LIMIT:
            ord_dvsn = ORD_LIMIT
            price = round_to_tick(float(order.limit_price), "down" if is_buy else "up",
                                  is_etf=self._is_etf(order.asset))
        else:
            ord_dvsn, price = ORD_MARKET, 0
        params = {
            **self.client.account_params(),
            "PDNO": normalize_code(order.asset.symbol),
            "ORD_DVSN": ord_dvsn,
            "ORD_QTY": str(qty),
            "ORD_UNPR": str(price),
            "EXCG_ID_DVSN_CD": "KRX",
            "SLL_TYPE": "" if is_buy else "01",   # 01 = 일반매도
            "CNDT_PRIC": "",
        }

        # 3) KIS 로 전송 → 주문번호 기억
        try:
            res = self.client.request("POST", PATH_ORDER, TR_BUY if is_buy else TR_SELL, params)
        except KisError as e:
            self._dispatch(self.ERROR_ORDER, order=order, error_msg=f"KIS 주문 실패: {e}")
            return order
        out = res.body.get("output") or {}
        order.identifier = str(out.get("ODNO", ""))
        self._kis_orgno[order.identifier] = str(out.get("KRX_FWDG_ORD_ORGNO", ""))
        self._kis_ord_dvsn[order.identifier] = ord_dvsn
        order.status = Order.OrderStatus.SUBMITTED
        order.update_raw(out)
        self._unprocessed_orders.append(order)
        self._dispatch(self.NEW_ORDER, order=order)
        return order

    def cancel_order(self, order: Order) -> None:
        if not order.identifier:
            raise ValueError("주문번호가 없어 취소할 수 없습니다. 먼저 주문을 제출했는지 확인하세요.")
        orgno = self._kis_orgno.get(order.identifier, "")
        params = {
            **self.client.account_params(),
            "KRX_FWDG_ORD_ORGNO": orgno,
            "ORGN_ODNO": order.identifier,
            "ORD_DVSN": self._kis_ord_dvsn.get(order.identifier, ORD_LIMIT),
            "RVSE_CNCL_DVSN_CD": "02",     # 02 = 취소
            "ORD_QTY": "0",
            "ORD_UNPR": "0",
            "QTY_ALL_ORD_YN": "Y",         # 남은 수량 전부
            "EXCG_ID_DVSN_CD": "KRX",
        }
        try:
            self.client.request("POST", PATH_CANCEL, TR_CANCEL, params)
        except KisError as e:
            raise LumibotBrokerAPIError(f"KIS 주문 취소 실패: {e}") from e
        # 취소 확정은 다음 체결 조회(do_polling)에서 처리한다

    def _modify_order(self, order: Order, limit_price=None, stop_price=None):
        """지정가 주문의 가격 정정. KIS 는 정정하면 새 주문번호를 준다.

        Schwab 어댑터처럼 order.identifier 를 새 번호로 바꾸고 예전 번호는 previous_identifiers 에 남긴다.
        (엔진 입장에서는 같은 주문이므로 따로 이벤트를 보내지 않는다)
        부분 체결된 주문은 정정하지 않는다: 새 주문번호의 체결 수량이 0부터 다시 세어져서 누적 체결 계산이 틀어진다.
        """
        if not order.identifier or not order.was_transmitted():
            raise ValueError("주문번호가 없어 정정할 수 없습니다. 먼저 주문을 제출했는지 확인하세요.")
        if stop_price is not None:
            raise LumibotBrokerAPIError("KIS 어댑터는 손절가(stop_price) 정정을 지원하지 않습니다.")
        if limit_price is None:
            raise ValueError("정정할 지정가(limit_price)를 넣어 주세요.")
        if order.order_type != Order.OrderType.LIMIT:
            raise LumibotBrokerAPIError("KIS 어댑터는 지정가 주문만 정정할 수 있습니다.")
        if not order.is_active():
            raise LumibotBrokerAPIError(f"이미 끝난 주문은 정정할 수 없습니다 (상태: {order.status}).")
        if order.status == Order.OrderStatus.PARTIALLY_FILLED or getattr(order, "transactions", None):
            raise LumibotBrokerAPIError("부분 체결된 주문은 정정할 수 없습니다. 취소 후 다시 주문하세요.")

        is_buy = order.side == Order.OrderSide.BUY
        price = round_to_tick(float(limit_price), "down" if is_buy else "up", is_etf=self._is_etf(order.asset))
        params = {
            **self.client.account_params(),
            "KRX_FWDG_ORD_ORGNO": self._kis_orgno.get(order.identifier, ""),
            "ORGN_ODNO": order.identifier,
            "ORD_DVSN": ORD_LIMIT,
            "RVSE_CNCL_DVSN_CD": "01",     # 01 = 정정
            "ORD_QTY": "0",
            "ORD_UNPR": str(price),
            "QTY_ALL_ORD_YN": "Y",         # 남은 수량 전부
            "EXCG_ID_DVSN_CD": "KRX",
        }
        try:
            res = self.client.request("POST", PATH_CANCEL, TR_CANCEL, params)
        except KisError as e:
            raise LumibotBrokerAPIError(f"KIS 주문 정정 실패: {e}") from e

        out = res.body.get("output") or {}
        new_id = str(out.get("ODNO") or out.get("odno") or "")
        if not new_id:
            raise LumibotBrokerAPIError("KIS 가 정정 주문번호를 돌려주지 않았습니다.")
        old_id = order.identifier
        order.previous_identifiers = (getattr(order, "previous_identifiers", None) or []) + [old_id]
        order.identifier = new_id
        order.limit_price = price
        self._kis_orgno[new_id] = str(out.get("KRX_FWDG_ORD_ORGNO") or self._kis_orgno.get(old_id, ""))
        self._kis_ord_dvsn[new_id] = ORD_LIMIT
        logger.info(f"KIS 주문 정정: {old_id} → {new_id}, 지정가 {price:,}원")
        return order

    # ================================================================== 주문 조회
    def _ccld_rows(self, odno: str = "") -> list[dict]:
        """오늘 주문·체결 목록 (다음 페이지까지)"""
        today = datetime.now(KST).strftime("%Y%m%d")
        params = {
            **self.client.account_params(),
            "INQR_STRT_DT": today, "INQR_END_DT": today,
            "SLL_BUY_DVSN_CD": "00", "PDNO": "", "CCLD_DVSN": "00", "INQR_DVSN": "00",
            "INQR_DVSN_3": "00", "ORD_GNO_BRNO": "", "ODNO": odno, "INQR_DVSN_1": "",
            "CTX_AREA_FK100": "", "CTX_AREA_NK100": "",
        }
        rows: list[dict] = []
        for page in self.client.request_all("GET", PATH_CCLD, TR_CCLD, params):
            out = page.body.get("output1") or []
            rows.extend(out if isinstance(out, list) else [out])
        return rows

    def _pull_broker_all_orders(self) -> list[dict]:
        return self._ccld_rows()

    def _pull_broker_order(self, identifier: str):
        rows = self._ccld_rows(odno=identifier)
        return next((r for r in rows if str(r.get("odno")) == str(identifier)), None)

    @staticmethod
    def _row_status(row: dict) -> str:
        """체결 조회 한 줄 → Lumibot 주문 상태"""
        qty = _dec(row.get("ord_qty"))
        filled = _dec(row.get("tot_ccld_qty"))
        canceled = _dec(row.get("cnc_cfrm_qty"))
        rejected = _dec(row.get("rjct_qty"))
        if rejected > 0 and filled == 0:
            return Order.OrderStatus.ERROR
        if qty > 0 and filled >= qty:
            return Order.OrderStatus.FILLED
        if str(row.get("cncl_yn", "")).upper() == "Y" or (canceled > 0 and filled + canceled >= qty):
            return Order.OrderStatus.CANCELED
        if filled > 0:
            return Order.OrderStatus.PARTIALLY_FILLED
        return Order.OrderStatus.OPEN

    def _parse_broker_order(self, response: dict, strategy_name: str, strategy_object=None) -> Order:
        """KIS 체결 조회 한 줄 → Lumibot Order"""
        name = strategy_name or (strategy_object.name if strategy_object else None)
        is_market = str(response.get("ord_dvsn_cd", "")) == ORD_MARKET
        filled = _num(response.get("tot_ccld_qty"))
        order = Order(
            strategy=name,
            asset=Asset(normalize_code(response.get("pdno")), Asset.AssetType.STOCK),
            quantity=int(_num(response.get("ord_qty"))),
            side=Order.OrderSide.BUY if str(response.get("sll_buy_dvsn_cd")) == SIDE_BUY else Order.OrderSide.SELL,
            order_type=Order.OrderType.MARKET if is_market else Order.OrderType.LIMIT,
            limit_price=None if is_market else (_num(response.get("ord_unpr")) or None),
            identifier=str(response.get("odno", "")),
            status=self._row_status(response),
            avg_fill_price=_num(response.get("avg_prvs")) if filled > 0 else None,
        )
        order.filled_quantity = filled
        order.broker_status = order.status
        return order

    # ================================================================== 체결 확인 (주기 조회)
    def do_polling(self):
        """몇 초마다 실행. 오늘 주문을 조회해서 우리가 낸 주문의 체결·취소·거부를 엔진에 알린다."""
        self._poll_count += 1
        if self._poll_count % 6 == 1:      # 보유 종목은 30초에 한 번 정도만 맞춘다 (호출 수 절약)
            try:
                self.sync_positions(None)
            except Exception as e:  # noqa: BLE001 - 동기화 실패로 체결 확인까지 멈추지 않게
                logger.warning(f"KIS 보유 종목 동기화 실패: {e}")
        try:
            rows = self._ccld_rows()
        except KisError as e:
            logger.warning(f"KIS 체결 조회 실패 (다음 주기에 다시 시도): {e}")
            return

        tracked = {o.identifier: o for o in self.get_tracked_orders() if o.identifier}
        # 취소는 원주문 줄에 표시되거나, 원주문번호(orgn_odno)를 가리키는 취소 줄로 올 수 있다
        canceled_by_row = {str(r.get("orgn_odno")) for r in rows
                           if str(r.get("cncl_yn", "")).upper() == "Y" and r.get("orgn_odno")}

        for row in rows:
            odno = str(row.get("odno", ""))
            order = tracked.get(odno)
            if order is None:
                continue   # 우리가 낸 주문이 아니면 무시 (취소 주문 줄, 다른 프로그램 주문 등)
            if not self._kis_orgno.get(odno) and row.get("ord_gno_brno"):
                self._kis_orgno[odno] = str(row["ord_gno_brno"])
            self._apply_row(order, row, canceled=odno in canceled_by_row)

    def _apply_row(self, order: Order, row: dict, *, canceled: bool = False) -> None:
        if order.is_filled():
            return
        qty = _dec(row.get("ord_qty")) or _dec(order.quantity)
        filled = _dec(row.get("tot_ccld_qty"))
        avg = _dec(row.get("avg_prvs"))
        # 체결: 지난번보다 늘어난 수량만 엔진에 알린다 (Lumibot 기본 기능 사용)
        if filled > 0 and avg > 0:
            self._reconcile_cumulative_execution(order, filled, avg, terminal=filled >= qty, raw=row)
        if order.is_filled() or not order.is_active():
            return
        status = self._row_status(row)
        if canceled or status == Order.OrderStatus.CANCELED:
            self._dispatch(self.CANCELED_ORDER, order=order)
        elif status == Order.OrderStatus.ERROR:
            self._dispatch(self.ERROR_ORDER, order=order,
                           error_msg=f"KIS 주문 거부: {row.get('prdt_name', '')} {row.get('rjct_qty', '')}주")

    # ================================================================== 이벤트 연결
    def _get_stream_object(self):
        from lumibot.trading_builtins import PollingStream
        return PollingStream(self.polling_interval)

    # 이벤트 처리 함수들. 스트림이 있으면 스트림이, 없으면(테스트 등) _dispatch 가 바로 부른다.
    def _on_new(self, order):
        self._process_trade_event(order, self.NEW_ORDER)

    def _on_fill(self, order, price, filled_quantity):
        self._process_trade_event(order, self.FILLED_ORDER, price=price,
                                  filled_quantity=filled_quantity, multiplier=order.asset.multiplier)

    def _on_cancel(self, order):
        self._process_trade_event(order, self.CANCELED_ORDER)

    def _on_error(self, order, error_msg):
        logger.error(error_msg)
        order.set_error(error_msg)
        # error 를 넘기지 않으면 엔진이 "Unknown order error" 로 덮어써서 KIS 거부 사유가 사라진다
        self._process_trade_event(order, self.ERROR_ORDER, error=LumibotBrokerAPIError(error_msg))

    def _register_stream_events(self):
        broker = self
        handlers = {
            self.NEW_ORDER: self._on_new,
            self.FILLED_ORDER: self._on_fill,
            self.CANCELED_ORDER: self._on_cancel,
            self.ERROR_ORDER: self._on_error,
        }

        @broker.stream.add_action(broker.POLL_EVENT)
        def on_poll():
            broker.do_polling()

        for event, handler in handlers.items():
            broker.stream.add_action(event)(self._safe(handler))

    @staticmethod
    def _safe(handler):
        def run(*args, **kwargs):
            try:
                handler(*args, **kwargs)
                return True
            except Exception:  # noqa: BLE001 - 이벤트 하나 실패로 스트림이 멈추지 않게
                logger.exception("KIS 이벤트 처리 중 오류")
                return False
        return run

    def _dispatch(self, event, **kwargs):
        stream = getattr(self, "stream", None)
        if stream is not None:
            stream.dispatch(event, **kwargs)
            return
        handler = {self.NEW_ORDER: self._on_new, self.FILLED_ORDER: self._on_fill,
                   self.CANCELED_ORDER: self._on_cancel, self.ERROR_ORDER: self._on_error}.get(event)
        if handler:
            self._safe(handler)(**kwargs)

    def _run_stream(self):
        self._stream_established()
        self.stream._run()
