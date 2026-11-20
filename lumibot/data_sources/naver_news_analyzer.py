
"""
naver_news_analyzer.py

네이버 뉴스 검색 API를 이용한 경제 뉴스 수집 모듈

기능
1. 검색어별 뉴스 수집
2. 뉴스 제목 및 요약 HTML 태그 제거
3. 발행일 변환
4. 중복 뉴스 제거
5. JSON 및 CSV 파일 저장

실행 전 환경변수 설정:
NAVER_API_CLIENT_ID
NAVER_API_CLIENT_SECRET
"""

import os
import re
import json
import csv
import html
import logging
from datetime import datetime
from email.utils import parsedate_to_datetime
from pathlib import Path

import requests


# --------------------------------------------------
# 기본 설정
# --------------------------------------------------

API_URL = "https://naverapihub.apigw.ntruss.com/search/v1/news"

BASE_DIR = Path(__file__).resolve().parent
OUTPUT_DIR = BASE_DIR / "news_data"

OUTPUT_DIR.mkdir(parents=True, exist_ok=True)

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s | %(levelname)s | %(message)s"
)

logger = logging.getLogger(__name__)


# --------------------------------------------------
# 예외 클래스
# --------------------------------------------------

class NaverNewsAPIError(Exception):
    """네이버 뉴스 API 요청 중 발생한 오류"""
    pass


# --------------------------------------------------
# 뉴스 수집 클래스
# --------------------------------------------------

class NaverNewsCollector:

    def __init__(self, client_id=None, client_secret=None):

        self.client_id = (
            client_id or os.getenv("NAVER_API_CLIENT_ID")
        )

        self.client_secret = (
            client_secret or os.getenv("NAVER_API_CLIENT_SECRET")
        )

        if not self.client_id or not self.client_secret:
            raise NaverNewsAPIError(
                "네이버 API 인증정보가 없습니다. "
                "환경변수 NAVER_API_CLIENT_ID와 "
                "NAVER_API_CLIENT_SECRET을 설정하세요."
            )

        self.session = requests.Session()

        self.session.headers.update({
            "X-NCP-APIGW-API-KEY-ID": self.client_id,
            "X-NCP-APIGW-API-KEY": self.client_secret,
            "Accept": "application/json"
        })

    # ----------------------------------------------
    # HTML 태그 제거
    # ----------------------------------------------

    @staticmethod
    def clean_text(text):

        if not text:
            return ""

        # 네이버 검색어 강조 태그 제거
        text = re.sub(r"<[^>]+>", "", text)

        # HTML 특수문자 변환
        text = html.unescape(text)

        # 불필요한 공백 정리
        text = re.sub(r"\s+", " ", text)

        return text.strip()

    # ----------------------------------------------
    # 날짜 변환
    # ----------------------------------------------

    @staticmethod
    def parse_date(pub_date):

        if not pub_date:
            return None

        try:
            return parsedate_to_datetime(pub_date).isoformat()

        except (TypeError, ValueError, OverflowError):
            return None

    # ----------------------------------------------
    # 뉴스 한 건 정리
    # ----------------------------------------------

    def normalize_news(self, item, query):

        return {
            "query": query,
            "title": self.clean_text(item.get("title", "")),
            "description": self.clean_text(
                item.get("description", "")
            ),
            "original_link": item.get("originallink", ""),
            "naver_link": item.get("link", ""),
            "published_at": self.parse_date(
                item.get("pubDate", "")
            ),
            "collected_at": datetime.now().astimezone().isoformat()
        }

    # ----------------------------------------------
    # 뉴스 검색 API 호출
    # ----------------------------------------------

    def search_news(
        self,
        query,
        display=30,
        start=1,
        sort="date"
    ):

        if not query or not query.strip():
            raise ValueError("검색어를 입력해야 합니다.")

        if not 1 <= display <= 100:
            raise ValueError("display는 1~100 사이여야 합니다.")

        if not 1 <= start <= 1000:
            raise ValueError("start는 1~1000 사이여야 합니다.")

        if sort not in ("date", "sim"):
            raise ValueError("sort는 date 또는 sim이어야 합니다.")

        params = {
            "query": query,
            "display": display,
            "start": start,
            "sort": sort,
            "format": "json"
        }

        try:
            response = self.session.get(
                API_URL,
                params=params,
                timeout=15
            )

            if response.status_code != 200:
                raise NaverNewsAPIError(
                    f"API 오류: HTTP {response.status_code} - "
                    f"{response.text[:500]}"
                )

            data = response.json()

        except requests.RequestException as e:
            raise NaverNewsAPIError(
                f"네트워크 요청 실패: {e}"
            ) from e

        except ValueError as e:
            raise NaverNewsAPIError(
                "API 응답을 JSON으로 변환하지 못했습니다."
            ) from e

        items = data.get("items", [])

        news_list = [
            self.normalize_news(item, query)
            for item in items
        ]

        logger.info(
            "검색어 '%s': %d건 수집",
            query,
            len(news_list)
        )

        return news_list

    # ----------------------------------------------
    # 여러 검색어로 뉴스 수집
    # ----------------------------------------------

    def collect_news(
        self,
        queries,
        per_query=30
    ):

        all_news = []

        for query in queries:

            try:
                news = self.search_news(
                    query=query,
                    display=per_query,
                    sort="date"
                )

                all_news.extend(news)

            except NaverNewsAPIError as e:
                logger.error(
                    "'%s' 검색 실패: %s",
                    query,
                    e
                )

        # 중복 제거
        unique_news = {}
        
        for news in all_news:

            # 원문 링크가 있으면 우선 사용
            unique_key = (
                news["original_link"]
                or news["naver_link"]
                or (
                    news["title"],
                    news["published_at"]
                )
            )

            if unique_key not in unique_news:
                unique_news[unique_key] = news

        result = list(unique_news.values())

        # 최신 뉴스 순으로 정렬
        result.sort(
            key=lambda x: x["published_at"] or "",
            reverse=True
        )

        logger.info(
            "전체 수집: %d건 / 중복 제거 후: %d건",
            len(all_news),
            len(result)
        )

        return result

    # ----------------------------------------------
    # JSON 저장
    # ----------------------------------------------

    @staticmethod
    def save_json(news_list, file_path):

        with open(
            file_path,
            "w",
            encoding="utf-8"
        ) as f:

            json.dump(
                news_list,
                f,
                ensure_ascii=False,
                indent=4
            )

        logger.info("JSON 저장 완료: %s", file_path)

    # ----------------------------------------------
    # CSV 저장
    # ----------------------------------------------

    @staticmethod
    def save_csv(news_list, file_path):

        if not news_list:
            logger.warning("저장할 뉴스가 없습니다.")
            return

        fieldnames = [
            "query",
            "title",
            "description",
            "original_link",
            "naver_link",
            "published_at",
            "collected_at"
        ]

        with open(
            file_path,
            "w",
            encoding="utf-8-sig",
            newline=""
        ) as f:

            writer = csv.DictWriter(
                f,
                fieldnames=fieldnames
            )

            writer.writeheader()
            writer.writerows(news_list)

        logger.info("CSV 저장 완료: %s", file_path)


# --------------------------------------------------
# 실행부
# --------------------------------------------------

if __name__ == "__main__":

    # 경제 및 주식 관련 기본 검색어
    search_queries = [
        "국내 증시",
        "코스피",
        "코스닥",
        "경제 전망",
        "기업 실적",
        "금리 정책",
        "주식 시장"
    ]

    try:

        collector = NaverNewsCollector()

        news_list = collector.collect_news(
            queries=search_queries,
            per_query=30
        )

        # 실행 시각을 파일명에 사용
        timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")

        json_path = OUTPUT_DIR / f"news_{timestamp}.json"
        csv_path = OUTPUT_DIR / f"news_{timestamp}.csv"

        collector.save_json(
            news_list,
            json_path
        )

        collector.save_csv(
            news_list,
            csv_path
        )

        print("\n===== 뉴스 수집 결과 =====")
        print(f"수집 뉴스: {len(news_list)}건")
        print(f"JSON 파일: {json_path}")
        print(f"CSV 파일: {csv_path}")

        # 상위 5건 출력
        for index, news in enumerate(news_list[:5], start=1):

            print(f"\n[{index}] {news['title']}")
            print(f"요약: {news['description']}")
            print(f"발행일: {news['published_at']}")
            print(f"링크: {news['naver_link']}")

    except NaverNewsAPIError as e:

        logger.error("뉴스 수집 실패: %s", e)
