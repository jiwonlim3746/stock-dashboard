# 종목 관련 최신 뉴스를 가져오는 모듈입니다.
# - 한국 주식: 네이버 뉴스 검색 API 사용 (config.py에서 .env의 NAVER_CLIENT_ID/SECRET을 불러옵니다)
# - 미국 주식: yfinance의 news 기능 사용
#
# 두 함수(get_korea_news, get_us_news) 모두 아래와 같은 형태의
# 뉴스 항목 리스트(딕셔너리의 리스트)를 반환하도록 통일해서,
# app.py에서는 시장 구분 없이 같은 방식으로 화면에 그릴 수 있게 했습니다.
#   [{"title": str, "link": str, "date": datetime | None, "source": str}, ...]

import html
import re
from datetime import datetime
from email.utils import parsedate_to_datetime  # RFC 822 형식(예: "Thu, 30 Jul 2026 10:52:00 +0900") 날짜 파싱용
from typing import List
from urllib.parse import urlparse

import requests
import streamlit as st
import yfinance as yf

from config import NAVER_CLIENT_ID, NAVER_CLIENT_SECRET

NAVER_NEWS_URL = "https://openapi.naver.com/v1/search/news.json"
NEWS_COUNT = 10  # 화면에 표시할 뉴스 개수


def _strip_html(text: str) -> str:
    """네이버 API가 검색어를 <b>태그로 강조해서 주기 때문에, 태그와 HTML 특수문자(엔티티)를 제거합니다."""
    text = re.sub(r"<.*?>", "", text)  # <b>, </b> 같은 HTML 태그 제거
    return html.unescape(text)  # &quot; 같은 HTML 엔티티를 원래 글자(")로 변환


def _sort_by_date_desc(news_list: List[dict]) -> List[dict]:
    """날짜(date)가 최신인 뉴스가 앞에 오도록 정렬합니다.

    네이버는 시간대 정보가 포함된 날짜(timezone-aware)를, yfinance는 시간대 정보가
    없는 날짜(naive)를 주기 때문에 datetime끼리 직접 비교하면 오류가 날 수 있어,
    비교하기 쉬운 숫자(timestamp, 1970년 이후 경과 초)로 변환해서 정렬합니다.
    날짜를 알 수 없는 항목은 맨 뒤로 보냅니다.
    """
    def sort_key(item: dict) -> float:
        date = item.get("date")
        return date.timestamp() if date else float("-inf")

    return sorted(news_list, key=sort_key, reverse=True)


# ------------------------------
# 한국 주식 (네이버 뉴스 검색 API)
# ------------------------------

@st.cache_data(ttl=600)  # 뉴스는 자주 바뀌므로 10분만 캐시합니다.
def get_korea_news(stock_name: str) -> List[dict]:
    """종목명으로 네이버 뉴스를 검색해 최신 10건을 가져옵니다."""
    if not NAVER_CLIENT_ID or not NAVER_CLIENT_SECRET:
        return []

    headers = {
        "X-Naver-Client-Id": NAVER_CLIENT_ID,
        "X-Naver-Client-Secret": NAVER_CLIENT_SECRET,
    }
    params = {
        "query": stock_name,
        "display": NEWS_COUNT,
        "sort": "date",  # 최신순 정렬
    }

    res = requests.get(NAVER_NEWS_URL, headers=headers, params=params, timeout=10)
    if res.status_code != 200:
        return []

    items = res.json().get("items", [])

    news_list = []
    for item in items:
        try:
            pub_date = parsedate_to_datetime(item["pubDate"])
        except (TypeError, ValueError, KeyError):
            pub_date = None

        # 네이버 API는 언론사(출처)를 따로 주지 않아서, 원문 링크의 도메인으로 대신 표시합니다.
        source = urlparse(item.get("originallink", "")).netloc.replace("www.", "") or "네이버뉴스"

        news_list.append(
            {
                "title": _strip_html(item.get("title", "")),
                "link": item.get("originallink") or item.get("link"),
                "date": pub_date,
                "source": source,
            }
        )

    return _sort_by_date_desc(news_list)


# ------------------------------
# 미국 주식 (yfinance)
# ------------------------------

@st.cache_data(ttl=600)
def get_us_news(ticker: str) -> List[dict]:
    """yfinance로 티커의 최신 뉴스 10건을 가져옵니다."""
    yf_ticker = yf.Ticker(ticker)
    raw_items = yf_ticker.news or []

    news_list = []
    for item in raw_items[:NEWS_COUNT]:
        # yfinance 버전에 따라 실제 정보가 "content" 안에 중첩되어 오기도 해서 둘 다 대응합니다.
        content = item.get("content", item)

        pub_date = None
        pub_date_text = content.get("pubDate")  # 예: "2026-07-29T22:00:00Z" (ISO 8601, UTC 기준)
        if pub_date_text:
            try:
                pub_date = datetime.strptime(pub_date_text, "%Y-%m-%dT%H:%M:%SZ")
            except ValueError:
                pub_date = None

        link = (
            content.get("canonicalUrl", {}).get("url")
            or content.get("clickThroughUrl", {}).get("url")
            or content.get("link")
        )
        source = content.get("provider", {}).get("displayName", "Yahoo Finance")

        news_list.append(
            {
                "title": content.get("title", ""),
                "link": link,
                "date": pub_date,
                "source": source,
            }
        )

    return _sort_by_date_desc(news_list)
