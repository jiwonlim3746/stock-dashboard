# 기업 실적(매출액/영업이익/당기순이익)과 주요 투자지표(PER/PBR/ROE)를 가져오는 모듈입니다.
# - 한국 주식: OpenDART API 사용 (config.py에서 .env의 DART_API_KEY를 불러옵니다)
# - 미국 주식: yfinance 사용
#
# 두 함수(get_korea_financials, get_us_financials) 모두 아래와 같은
# 형태의 딕셔너리를 반환하도록 통일해서, app.py에서는 시장 구분 없이
# 같은 방식으로 화면에 그릴 수 있게 했습니다.
#   {
#       "income": DataFrame(연도, 매출액, 영업이익, 당기순이익),
#       "per": float | None,
#       "pbr": float | None,
#       "roe": float | None,   # 퍼센트(%) 단위
#   }

import io
import zipfile
import xml.etree.ElementTree as ET
from datetime import datetime
from typing import Optional

import pandas as pd
import requests
import streamlit as st
import yfinance as yf

from config import DART_API_KEY
from stock_data import get_krx_listing

DART_BASE_URL = "https://opendart.fss.or.kr/api"

EMPTY_RESULT = {"income": pd.DataFrame(), "per": None, "pbr": None, "roe": None}


def _to_number(value) -> Optional[float]:
    """DART API가 '1,234' 또는 '-' 형태로 주는 값을 숫자로 변환합니다."""
    if value is None:
        return None
    text = str(value).replace(",", "").strip()
    if text in ("", "-"):
        return None
    try:
        return float(text)
    except ValueError:
        return None


# ------------------------------
# 한국 주식 (OpenDART)
# ------------------------------

@st.cache_data(ttl=60 * 60 * 24)  # 용량이 큰 파일이라 하루 동안만 캐시합니다.
def _get_dart_corp_code_map() -> dict:
    """DART의 '종목코드 -> 고유번호(corp_code)' 매핑을 가져옵니다."""
    if not DART_API_KEY:
        return {}

    res = requests.get(
        f"{DART_BASE_URL}/corpCode.xml",
        params={"crtfc_key": DART_API_KEY},
        timeout=10,
    )
    res.raise_for_status()

    # corpCode.xml은 zip 파일 형태로 내려옵니다.
    with zipfile.ZipFile(io.BytesIO(res.content)) as zf:
        xml_bytes = zf.read("CORPCODE.xml")

    root = ET.fromstring(xml_bytes)
    code_map = {}
    for item in root.iter("list"):
        stock_code = (item.findtext("stock_code") or "").strip()
        corp_code = (item.findtext("corp_code") or "").strip()
        if stock_code:  # 비상장사는 stock_code가 비어있으므로 제외합니다.
            code_map[stock_code] = corp_code
    return code_map


@st.cache_data(ttl=3600)
def _get_dart_accounts(corp_code: str, year: int) -> pd.DataFrame:
    """특정 사업연도의 주요 계정(매출액/영업이익 등) 원본 데이터를 가져옵니다."""
    res = requests.get(
        f"{DART_BASE_URL}/fnlttSinglAcnt.json",
        params={
            "crtfc_key": DART_API_KEY,
            "corp_code": corp_code,
            "bsns_year": str(year),
            "reprt_code": "11011",  # 사업보고서(연간)
        },
        timeout=10,
    )
    data = res.json()

    if data.get("status") != "000":  # "000"이 아니면 정상 조회 실패
        return pd.DataFrame()

    df = pd.DataFrame(data["list"])

    # 연결재무제표(CFS)가 있으면 우선 사용하고, 없으면 별도재무제표(OFS)를 사용합니다.
    if "fs_div" in df.columns and "CFS" in df["fs_div"].values:
        df = df[df["fs_div"] == "CFS"]

    return df


@st.cache_data(ttl=3600)
def get_korea_financials(ticker_code: str) -> dict:
    """한국 주식의 최근 3년 실적과 PER/PBR/ROE를 가져옵니다."""
    if not DART_API_KEY:
        return EMPTY_RESULT

    corp_map = _get_dart_corp_code_map()
    corp_code = corp_map.get(ticker_code)
    if corp_code is None:
        return EMPTY_RESULT

    # 사업보고서는 다음 해 3~4월에 공시되므로, 최신 연도부터 순서대로
    # 조회해서 실제로 데이터가 있는 연도만 모읍니다. (최대 최근 3개년)
    current_year = datetime.today().year
    records = []
    for year in [current_year - 1, current_year - 2, current_year - 3, current_year - 4]:
        df = _get_dart_accounts(corp_code, year)
        if df.empty:
            continue

        # DART는 적자 기업의 경우 "영업이익(손실)", "당기순이익(손실)"처럼
        # 계정명 뒤에 괄호가 붙기도 해서, 정확히 일치하는 대신 접두어로 찾습니다.
        row = {"연도": year}
        for account in ["매출액", "영업이익", "당기순이익", "자본총계"]:
            match = df[df["account_nm"].str.startswith(account)]
            row[account] = _to_number(match.iloc[0]["thstrm_amount"]) if not match.empty else None
        records.append(row)

        if len(records) == 3:  # 3개년치를 모았으면 그만 조회합니다.
            break

    if not records:
        return EMPTY_RESULT

    income_df = pd.DataFrame(records).sort_values("연도").reset_index(drop=True)

    # PER/PBR/ROE 계산에 필요한 최신 연도의 순이익/자본총계
    latest = income_df.iloc[-1]
    net_income_latest = latest.get("당기순이익")
    equity_latest = latest.get("자본총계")

    income_df = income_df.drop(columns=["자본총계"])  # 표에는 손익 항목만 표시

    per = pbr = roe = None

    # 현재 시가총액은 FinanceDataReader의 KRX 종목 목록에서 가져옵니다.
    listing = get_krx_listing()
    matched = listing.loc[listing["Code"] == ticker_code]
    if not matched.empty:
        marcap = matched.iloc[0].get("Marcap")
        if marcap and net_income_latest:
            per = marcap / net_income_latest
        if marcap and equity_latest:
            pbr = marcap / equity_latest
    if net_income_latest and equity_latest:
        roe = (net_income_latest / equity_latest) * 100

    return {"income": income_df, "per": per, "pbr": pbr, "roe": roe}


# ------------------------------
# 미국 주식 (yfinance)
# ------------------------------

@st.cache_data(ttl=3600)
def get_us_financials(ticker: str) -> dict:
    """미국 주식의 최근 3년 실적과 PER/PBR/ROE를 가져옵니다."""
    yf_ticker = yf.Ticker(ticker)

    financials_df = yf_ticker.financials  # 연간 손익계산서 (최근 연도가 첫 컬럼)
    income_df = pd.DataFrame()
    if not financials_df.empty:
        rows = [r for r in ["Total Revenue", "Operating Income", "Net Income"] if r in financials_df.index]
        income_df = financials_df.loc[rows].iloc[:, :3]  # 최근 3개년만 사용
        income_df = income_df.rename(columns=lambda c: c.year)
        income_df = income_df.T.sort_index()
        income_df = income_df.rename(
            columns={
                "Total Revenue": "매출액",
                "Operating Income": "영업이익",
                "Net Income": "당기순이익",
            }
        )
        income_df.index.name = "연도"
        income_df = income_df.reset_index()

    info = yf_ticker.info
    per = info.get("trailingPE")
    pbr = info.get("priceToBook")
    roe = info.get("returnOnEquity")
    if roe is not None:
        roe = roe * 100  # 퍼센트로 변환

    return {"income": income_df, "per": per, "pbr": pbr, "roe": roe}
