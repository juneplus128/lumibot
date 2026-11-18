# KakaoTalk 매매 알림 (Kakao Notifications)

주문이 체결·부분 체결·취소될 때와 장 마감 후에 **내 카카오톡(나와의 채팅)**으로 알림을 보낸다.
전략 코드는 고칠 필요 없이 `.env` 설정만 켜면 동작한다.

## 구성

| 파일 | 역할 |
|---|---|
| `lumibot/components/notifications/kakao.py` | `KakaoNotificationProvider`(메시지 전송, 토큰 자동 갱신), `KakaoTradeAlerts`(자동 매매 알림) |
| `lumibot/components/notifications/kakao_setup.py` | `lumibot kakao guide/login/test` 명령 (설명서 출력, 로그인 자동화) |
| `lumibot/components/notifications/KAKAO_SETUP.md` | 사용자 설정 안내서 (설치 묶음 포함) |
| `lumibot/cli.py` | `kakao` 하위 명령 등록 |
| `lumibot/components/notifications/base.py` | `NotificationManager.configure_kakao()` 추가 |
| `lumibot/strategies/strategy_executor.py` | 체결·부분 체결·취소·장 마감 이벤트에서 `KakaoTradeAlerts` 호출 (기존 Discord 자동 알림과 같은 위치) |
| `tests/test_kakao_notification.py` | 가짜 응답(Mock) 단위 테스트 |

## 준비

사용자 설정 안내서는 설치 묶음에 포함된 `lumibot/components/notifications/KAKAO_SETUP.md` 이다.
터미널에서 바로 볼 수 있다.

```bash
lumibot kakao guide   # 1~2단계: 카카오 콘솔 설정(직접), .env 에 REST API 키 넣기
lumibot kakao login   # 3단계: 브라우저 동의만 하면 토큰 발급·저장·권한 확인·테스트 메시지 자동
lumibot kakao test    # 테스트 메시지만 다시 보내기
```

`login` 은 `KAKAO_REDIRECT_URI`(기본 `http://localhost:18765/kakao/callback`)로 작은 로컬 서버를 잠깐 열어
인가 코드를 자동으로 받는다. 서버를 열 수 없거나 https 주소를 쓰면 `--manual` 처럼 주소를 붙여 넣는 방식으로 바뀐다.

알림을 켰는데 키나 토큰이 없으면 전략 시작 시 위 명령을 안내하는 경고가 한 번 나온다. 매매는 계속된다.

## 설정 (`.env`)

| 변수 | 기본값 | 설명 |
|---|---|---|
| `KAKAO_REST_API_KEY` | (필수) | 앱의 REST API 키 |
| `KAKAO_CLIENT_SECRET` | | "카카오 로그인" 클라이언트 시크릿을 켠 경우 (비즈니스 인증용 아님) |
| `KAKAO_TOKEN_FILE` | `kakao_token.json` | 토큰 저장 파일 |
| `KAKAO_TRADE_ALERTS` | `false` | `true`면 자동 매매 알림 사용 |
| `KAKAO_DAILY_SUMMARY` | `true` | 장 마감 후 하루 요약 |
| `KAKAO_ALERT_IN_BACKTEST` | `false` | 백테스트 중에도 보낼지 |
| `KAKAO_BACKTEST_MAX_ALERTS` | `20` | 백테스트 1회당 최대 메시지 수 |

## 동작 규칙

- 액세스 토큰이 만료돼 401이 오면 `refresh_token`으로 새 토큰을 받아 파일에 저장하고 한 번 다시 보낸다.
- 카카오 텍스트 메시지 길이 제한(200자)에 맞춰 자른다.
- 알림이 실패해도 예외를 밖으로 던지지 않는다. 경고 로그만 남기고 매매는 계속된다.
- 백테스트에서는 기본으로 보내지 않는다. 켜더라도 최대 개수를 넘으면 더 보내지 않는다(일일 쿼터 보호).
- `hide_trades` 설정이 켜져 있으면 종목·수량·가격을 숨긴다.

## 메시지 예시

```
[체결] 005930 10주 매수
가격: 70,000원
금액: 700,000원
보유: 10주
시각: 2026-11-02 09:30
```

## 테스트

```bash
pytest tests/test_kakao_notification.py -q
```

## 알려진 제한

- "나에게 보내기"는 나와의 채팅에 쌓이며 휴대폰 푸시 알림이 울리지 않을 수 있다.
- `refresh_token`도 만료되면(약 2개월) 다시 로그인해 토큰을 받아야 한다.
