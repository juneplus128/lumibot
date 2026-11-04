# 카카오톡 매매 알림 설정 안내

주문이 체결·부분 체결·취소될 때와 장 마감 후에 내 카카오톡 "나와의 채팅"으로 알림을 받는다.
처음 한 번만 아래 순서대로 설정하면 된다.

이 안내는 터미널에서 언제든 다시 볼 수 있다.

    lumibot kakao guide


## 1단계. 카카오 앱 만들기 (직접, 처음 한 번)

카카오 개발자 콘솔은 프로그램이 대신 설정할 수 없어서 직접 해야 한다.

1. https://developers.kakao.com/console/app 에 로그인하고 [앱 추가]로 앱을 만든다.
2. [카카오 로그인] → [사용 설정] → 상태를 ON 으로 바꾼다.
3. [앱] → [플랫폼 키] → [REST API 키] 로 들어가서
   "카카오 로그인 리다이렉트 URI" 에 아래 주소를 추가하고 저장한다.
   (같은 화면의 "비즈니스 인증 리다이렉트 URI" 가 아니라 "카카오 로그인" 쪽에 넣는다)

       http://localhost:18765/kakao/callback

4. [카카오 로그인] → [동의항목] 에서 "카카오톡 메시지 전송(talk_message)" 의
   [설정] 을 눌러 "이용 중 동의" 로 바꾸고 저장한다.
5. 같은 [REST API 키] 화면에서 REST API 키를 복사한다.
   클라이언트 시크릿은 "카카오 로그인" 용과 "비즈니스 인증" 용이 따로 있다.
   "카카오 로그인" 클라이언트 시크릿이 "활성화 ON" 이면 그 코드를 복사한다.


## 2단계. .env 에 키 넣기 (직접, 처음 한 번)

.env 는 GitHub 에 올리지 않는다.

1. 카카오 설정만 들어 있는 견본(.env.kakao.example)을 복사해서 .env 를 만든다.
   견본은 lumibot 저장소 맨 위 폴더에 있으니 먼저 그 폴더로 이동한다.

       # git clone 을 한 바로 그 자리라면 (모든 운영체제 공통)
       cd lumibot

       # 바탕화면에 clone 했다면
       cd ~/Desktop/lumibot                     # macOS, Linux
       cd $HOME\Desktop\lumibot                 # Windows PowerShell
       cd %USERPROFILE%\Desktop\lumibot         # Windows 명령 프롬프트(cmd)

       # VS Code 에서 lumibot 폴더를 열고 터미널을 열면 이미 그 폴더라서 cd 가 필요 없다

   .env 가 아직 없으면 (처음)

       cp .env.kakao.example .env               # macOS, Linux
       copy .env.kakao.example .env             # Windows

   .env 가 이미 있으면 (다른 설정이 들어 있을 때) 맨 아래에 이어 붙인다

       cat .env.kakao.example >> .env           # macOS, Linux
       type .env.kakao.example >> .env          # Windows

   저장소 맨 위에 .env 를 두면, 저장소 안 어느 폴더에서 전략을 실행해도 이 .env 를 찾는다
   (Lumibot 이 지금 폴더부터 위쪽 폴더로 올라가며 .env 를 찾기 때문).

2. .env 를 열고 = 뒤에 콘솔에서 복사한 값을 붙여넣고 저장한다. (따옴표, 띄어쓰기 없이)

       KAKAO_REST_API_KEY=복사한_REST_API_키
       KAKAO_CLIENT_SECRET=카카오_로그인_클라이언트_시크릿   (시크릿을 안 켰으면 비워두기)
       KAKAO_TRADE_ALERTS=true                              (이미 들어 있음)


## 3단계. 로그인 (명령 한 번, 동의 클릭 한 번)

전략을 실행할 폴더(전략 파일이 있는 폴더)로 이동한 뒤 실행한다.

    cd 내_전략_폴더
    lumibot kakao login

브라우저가 열리면 카카오 로그인 후 "동의하고 계속하기" 만 누르면 된다.
나머지(토큰 발급, kakao_token.json 저장, 권한 확인, 테스트 메시지)는 자동으로 한다.
kakao_token.json 도 GitHub 에 올리지 않는다.

주의: kakao_token.json 은 .env 와 달리 위쪽 폴더를 찾지 않고 "터미널이 지금 있는 폴더"에 저장되고
그 폴더에서만 읽힌다. 그래서 login 과 전략 실행은 같은 폴더에서 한다.

    cd 내_전략_폴더
    python 내_전략.py                 # login 한 폴더와 같은 곳에서 실행

다른 폴더에서도 쓰고 싶으면 .env 에 토큰 파일의 전체 경로를 적어 둔다.

    KAKAO_TOKEN_FILE=/Users/내이름/Desktop/lumibot/kakao_token.json          # macOS 예
    KAKAO_TOKEN_FILE=C:/Users/내이름/Desktop/lumibot/kakao_token.json        # Windows 예

토큰은 자동으로 갱신된다. 약 2개월 동안 쓰지 않아 갱신이 끊기면 이 명령을 다시 실행한다.


## 4단계. 알림 켜기

.env 에 아래 한 줄이 있으면 된다 (.env.kakao.example 로 만들었다면 이미 들어 있다).
평소처럼 전략을 실행한다. 전략 코드는 고칠 필요가 없다.

    KAKAO_TRADE_ALERTS=true

선택 설정

    KAKAO_DAILY_SUMMARY=true        장 마감 후 하루 요약 (기본 true)
    KAKAO_ALERT_IN_BACKTEST=false   백테스트 중에도 보낼지 (기본 false)
    KAKAO_BACKTEST_MAX_ALERTS=20    백테스트 1회당 최대 메시지 수
    KAKAO_TOKEN_FILE=kakao_token.json
    KAKAO_REDIRECT_URI=http://localhost:18765/kakao/callback


## 자주 나오는 문제

| 화면/메시지 | 원인 | 해결 |
|---|---|---|
| 로그인 화면에 KOE004 | 카카오 로그인이 꺼져 있음 | 1단계 2번 |
| 로그인 화면에 KOE006 | 카카오 로그인 리다이렉트 URI 미등록 또는 불일치 (비즈니스 인증 칸에 넣은 경우 포함) | 1단계 3번, 주소를 글자 하나까지 똑같이 |
| KOE010 | REST API 키 또는 클라이언트 시크릿이 틀림 (비즈니스 인증용 시크릿을 넣은 경우 포함) | 2단계 다시 확인 |
| KOE101 / "예시 글자가 그대로" | .env 에 진짜 키 대신 예시 글자(한글)가 있음 | 콘솔에서 REST API 키를 다시 복사해 2단계대로 넣기 |
| KOE322 / "토큰을 새로 받지 못했습니다" | 로그인이 만료됨 (약 2달) | 3단계 `lumibot kakao login` 다시 |
| 권한(talk_message) 없음 | 동의항목 미설정 | 1단계 4번 후 3단계 다시 |
| 메시지는 성공인데 알림이 안 울림 | "나에게 보내기" 는 나와의 채팅에 조용히 쌓임 | 카카오톡 "나와의 채팅" 확인 |

테스트 메시지만 다시 보내 보려면

    lumibot kakao test
