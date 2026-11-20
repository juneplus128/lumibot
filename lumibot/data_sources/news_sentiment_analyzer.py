
"""
news_sentiment_analyzer.py

한국어 경제 뉴스 호재 / 악재 / 중립 분류기

입력:
    news_data 폴더의 네이버 뉴스 수집 JSON

출력:
    분류 결과 JSON 및 CSV

분류 방식:
    키워드 기반 규칙 점수
"""

import csv
import json
import logging
from pathlib import Path


BASE_DIR = Path(__file__).resolve().parent
NEWS_DIR = BASE_DIR / "news_data"

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s | %(levelname)s | %(message)s"
)

logger = logging.getLogger(__name__)


class NewsSentimentAnalyzer:

    # 호재 키워드와 가중치
    POSITIVE_KEYWORDS = {
        "흑자 전환": 4,
        "적자 탈출": 4,
        "사상 최대 실적": 5,
        "역대 최대 실적": 5,
        "영업이익 증가": 4,
        "순이익 증가": 4,
        "실적 개선": 3,
        "실적 호조": 4,
        "매출 증가": 2,
        "수익성 개선": 3,
        "대규모 공급 계약": 4,
        "공급 계약 체결": 3,
        "대규모 수주": 4,
        "수주 계약 체결": 3,
        "신규 계약 체결": 2,
        "목표주가 상향": 3,
        "투자의견 상향": 3,
        "배당 확대": 3,
        "자사주 매입": 2,
        "신사업 진출": 2,
        "수출 증가": 2,
        "시장 점유율 확대": 3,
        "규제 완화": 2,
        "부채 감소": 2,
        "비용 절감": 2,
        "주가 급등": 2,
    }

    # 악재 키워드와 가중치
    NEGATIVE_KEYWORDS = {
        "적자 전환": 5,
        "영업손실 확대": 5,
        "순손실 확대": 5,
        "사상 최대 손실": 5,
        "실적 악화": 4,
        "실적 부진": 3,
        "매출 감소": 2,
        "영업이익 감소": 3,
        "순이익 감소": 3,
        "수익성 악화": 3,
        "계약 해지": 4,
        "수주 취소": 4,
        "공급 계약 취소": 4,
        "목표주가 하향": 3,
        "투자의견 하향": 3,
        "배당 삭감": 3,
        "배당 축소": 3,
        "유상증자 결정": 2,
        "횡령": 5,
        "배임": 5,
        "회계 부정": 5,
        "상장폐지": 5,
        "거래 정지": 4,
        "부도": 5,
        "파산": 5,
        "대규모 리콜": 4,
        "소송 패소": 3,
        "과징금 부과": 3,
        "규제 강화": 2,
        "주가 급락": 3,
        "영업정지": 4,
        "공장 가동 중단": 3,
    }

    def classify_text(self, text):

        text = (text or "").replace("\n", " ").strip()

        positive_score = 0
        negative_score = 0

        positive_evidence = []
        negative_evidence = []

        # 호재 키워드 확인
        for keyword, weight in self.POSITIVE_KEYWORDS.items():

            if keyword in text:
                positive_score += weight
                positive_evidence.append(keyword)

        # 악재 키워드 확인
        for keyword, weight in self.NEGATIVE_KEYWORDS.items():

            if keyword in text:
                negative_score += weight
                negative_evidence.append(keyword)

        total_score = positive_score - negative_score

        # 호재와 악재가 동시에 강하게 나타나는 경우
        # 어느 한쪽으로 단정하지 않음
        if positive_score >= 3 and negative_score >= 3:
            sentiment = "NEUTRAL"

        elif total_score >= 3:
            sentiment = "POSITIVE"

        elif total_score <= -3:
            sentiment = "NEGATIVE"

        else:
            sentiment = "NEUTRAL"

        # 키워드 점수에 기반한 참고용 신뢰도
        confidence = min(
            0.95,
            0.5 + abs(total_score) / 20
        )

        if sentiment == "NEUTRAL":
            confidence = 0.5

        return {
            "sentiment": sentiment,
            "sentiment_ko": {
                "POSITIVE": "호재",
                "NEGATIVE": "악재",
                "NEUTRAL": "중립"
            }[sentiment],
            "score": total_score,
            "confidence": round(confidence, 2),
            "positive_evidence": positive_evidence,
            "negative_evidence": negative_evidence
        }

    def classify_news(self, news):

        title = news.get("title", "")
        description = news.get("description", "")

        # 제목은 요약보다 투자 판단에 더 큰 비중을 둠
        title_result = self.classify_text(title)
        description_result = self.classify_text(description)

        positive_score = (
            title_result["score"] * 2
            if title_result["score"] > 0 else 0
        ) + (
            description_result["score"]
            if description_result["score"] > 0 else 0
        )

        negative_score = (
            abs(title_result["score"]) * 2
            if title_result["score"] < 0 else 0
        ) + (
            abs(description_result["score"])
            if description_result["score"] < 0 else 0
        )

        total_score = positive_score - negative_score

        if positive_score >= 3 and negative_score >= 3:
            sentiment = "NEUTRAL"
        elif total_score >= 3:
            sentiment = "POSITIVE"
        elif total_score <= -3:
            sentiment = "NEGATIVE"
        else:
            sentiment = "NEUTRAL"

        confidence = (
            0.5 if sentiment == "NEUTRAL"
            else min(0.95, 0.5 + abs(total_score) / 20)
        )

        result = dict(news)

        result.update({
            "sentiment": sentiment,
            "sentiment_ko": {
                "POSITIVE": "호재",
                "NEGATIVE": "악재",
                "NEUTRAL": "중립"
            }[sentiment],
            "sentiment_score": total_score,
            "confidence": round(confidence, 2),
            "positive_evidence": list(dict.fromkeys(
                title_result["positive_evidence"]
                + description_result["positive_evidence"]
            )),
            "negative_evidence": list(dict.fromkeys(
                title_result["negative_evidence"]
                + description_result["negative_evidence"]
            ))
        })

        return result

    def analyze_news_list(self, news_list):

        analyzed = []

        for news in news_list:
            analyzed.append(self.classify_news(news))

        return analyzed

    @staticmethod
    def load_latest_news():

        files = sorted(
            NEWS_DIR.glob("news_*.json"),
            key=lambda path: path.stat().st_mtime,
            reverse=True
        )

        if not files:
            raise FileNotFoundError(
                "news_data 폴더에 수집된 뉴스 JSON 파일이 없습니다."
            )

        # 가장 최근 수집 파일 선택
        latest_file = files[0]

        with open(latest_file, "r", encoding="utf-8") as f:
            news_list = json.load(f)

        logger.info(
            "뉴스 파일 로드: %s (%d건)",
            latest_file.name,
            len(news_list)
        )

        return news_list

    @staticmethod
    def save_results(news_list):

        NEWS_DIR.mkdir(parents=True, exist_ok=True)

        json_path = NEWS_DIR / "classified_news.json"
        csv_path = NEWS_DIR / "classified_news.csv"

        with open(json_path, "w", encoding="utf-8") as f:
            json.dump(
                news_list,
                f,
                ensure_ascii=False,
                indent=4
            )

        if news_list:

            fieldnames = list(news_list[0].keys())

            with open(
                csv_path,
                "w",
                encoding="utf-8-sig",
                newline=""
            ) as f:

                writer = csv.DictWriter(
                    f,
                    fieldnames=fieldnames
                )

                writer.writeheader()

                for news in news_list:
                    row = dict(news)

                    # CSV에서 리스트는 문자열로 저장
                    for key in (
                        "positive_evidence",
                        "negative_evidence"
                    ):
                        row[key] = ", ".join(row.get(key, []))

                    writer.writerow(row)

        logger.info("분석 JSON 저장: %s", json_path)
        logger.info("분석 CSV 저장: %s", csv_path)


if __name__ == "__main__":

    analyzer = NewsSentimentAnalyzer()

    # 실제 수집 파일이 없어도 분류 기능을 확인할 수 있는 예시
    sample_news = [
        {
            "title": "삼성전자 영업이익 증가, 실적 개선 기대",
            "description": "반도체 사업의 수익성이 개선되고 있다."
        },
        {
            "title": "기업 횡령 및 배임 혐의 발생",
            "description": "회계 부정 의혹으로 수사에 착수했다."
        },
        {
            "title": "국내 증시 거래량 증가",
            "description": "투자자들의 거래가 활발해졌다."
        }
    ]

    sample_results = analyzer.analyze_news_list(sample_news)

    print("\n===== 분류 기능 자체 테스트 =====")

    for result in sample_results:
        print(
            f"{result['sentiment_ko']} | "
            f"{result['title']} | "
            f"점수: {result['sentiment_score']}"
        )

    # 실제 수집 뉴스가 있다면 분석 및 저장
    try:
        news_list = analyzer.load_latest_news()
        analyzed_news = analyzer.analyze_news_list(news_list)

        analyzer.save_results(analyzed_news)

        print("\n===== 뉴스 분석 결과 =====")
        print(f"전체 뉴스: {len(analyzed_news)}건")

        for category in ("POSITIVE", "NEGATIVE", "NEUTRAL"):
            count = sum(
                1 for item in analyzed_news
                if item["sentiment"] == category
            )
            print(f"{category}: {count}건")

    except FileNotFoundError as e:
        logger.warning("%s", e)
