# 주가 데이터를 가져오는 모듈입니다.
# - 입력값이 한국 주식인지 미국 주식인지 판별하는 기능
# - 한국 주식: FinanceDataReader 라이브러리 사용 (로그인/API 키 불필요)
# - 미국 주식: yfinance 라이브러리 사용
#
# 나중에 실적/뉴스 기능을 추가할 때는 이 파일과 비슷한 형태로
# financials.py, news.py 같은 모듈을 새로 만들어서 붙이면 됩니다.

import re
from datetime import datetime, timedelta
from typing import Optional

import FinanceDataReader as fdr
import pandas as pd
import streamlit as st
import yfinance as yf


# ------------------------------
# 1. 입력값으로 시장(한국/미국) 판별하기
# ------------------------------

def _is_korean(text: str) -> bool:
    """문자열에 한글이 포함되어 있는지 확인합니다."""
    return bool(re.search(r"[가-힣]", text))


def _is_six_digit_code(text: str) -> bool:
    """문자열이 한국 종목코드 형식(숫자 6자리)인지 확인합니다."""
    return bool(re.fullmatch(r"\d{6}", text))


def detect_market(query: str) -> str:
    """검색어를 보고 한국 주식("KR")인지 미국 주식("US")인지 자동으로 판별합니다.

    규칙:
    - 한글이 포함되어 있으면 -> 한국 (종목명으로 검색)
    - 숫자 6자리면 -> 한국 (종목코드로 검색)
    - 그 외(영문 등)면 -> 미국 (티커로 검색)
    """
    query = query.strip()
    if _is_korean(query) or _is_six_digit_code(query):
        return "KR"
    return "US"


# ------------------------------
# 2. 한국 주식 (FinanceDataReader)
# ------------------------------

@st.cache_data(ttl=3600)  # 1시간 동안 결과를 캐시해서 반복 호출을 줄입니다.
def get_krx_listing() -> pd.DataFrame:
    """KRX 전체 종목 목록(코드, 종목명, 시가총액 등)을 가져옵니다.

    financials.py에서 PER/PBR 계산에 필요한 시가총액(Marcap)을 가져올 때도 사용합니다.
    """
    return fdr.StockListing("KRX")


def _get_krx_name_to_code_map() -> dict:
    """'종목명 -> 종목코드' 매핑 딕셔너리를 만듭니다. (전체 종목 대상)"""
    listing = get_krx_listing()
    return dict(zip(listing["Name"], listing["Code"]))


def resolve_korea_ticker(query: str) -> Optional[str]:
    """입력값(종목명 또는 코드)을 한국 종목코드로 변환합니다. 못 찾으면 None."""
    query = query.strip()
    if _is_six_digit_code(query):
        return query

    name_to_code = _get_krx_name_to_code_map()
    return name_to_code.get(query)


@st.cache_data(ttl=3600)
def get_korea_stock_name(ticker_code: str) -> str:
    """종목코드로 종목명을 조회합니다."""
    listing = get_krx_listing()
    matched = listing.loc[listing["Code"] == ticker_code, "Name"]
    return matched.iloc[0] if not matched.empty else ticker_code


@st.cache_data(ttl=3600)
def get_korea_price_history(ticker_code: str) -> pd.DataFrame:
    """최근 1년간 한국 주식의 일별 시세(OHLCV)를 가져옵니다."""
    end_date = datetime.today()
    start_date = end_date - timedelta(days=365)

    # FinanceDataReader는 컬럼명이 이미 영문(Open/High/Low/Close/Volume)이라
    # 별도의 컬럼명 변환이 필요 없습니다.
    df = fdr.DataReader(ticker_code, start_date, end_date)
    return df


# ------------------------------
# 3. 미국 주식 (yfinance)
# ------------------------------

@st.cache_data(ttl=3600)
def get_us_price_history(ticker: str) -> pd.DataFrame:
    """최근 1년간 미국 주식의 일별 시세(OHLCV)를 가져옵니다."""
    df = yf.download(ticker, period="1y", progress=False)

    # yfinance 최신 버전은 컬럼이 (필드, 티커) 형태의 멀티인덱스로 나올 수 있어서
    # 단순 컬럼으로 정리해줍니다.
    if isinstance(df.columns, pd.MultiIndex):
        df.columns = df.columns.get_level_values(0)

    return df
