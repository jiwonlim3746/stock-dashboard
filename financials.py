# 기업 실적(매출액/영업이익/당기순이익)과 주요 투자지표(PER/PBR/ROE)를 가져오는 모듈입니다.
# - 한국 주식: OpenDART API 사용 (config.py에서 .env의 DART_API_KEY를 불러옵니다)
# - 미국 주식: yfinance 사용
#
# 두 함수(get_korea_financials, get_us_financials) 모두 아래와 같은
# 형태의 딕셔너리를 반환하도록 통일해서, app.py에서는 시장 구분 없이
# 같은 방식으로 화면에 그릴 수 있게 했습니다.
#   {
#       "income": DataFrame(연도, 매출액, 영업이익, 당기순이익),          # 실적 표용
#       "trend": DataFrame(연도, 매출액, 영업이익, 당기순이익, ROE),      # 추이 그래프용 (ROE 포함)
#       "per": float | None,
#       "pbr": float | None,
#       "roe": float | None,   # 퍼센트(%) 단위, 최신 연도 기준
#   }
#
# 추이 그래프를 "연간"이 아니라 "분기"로 보고 싶을 때는 아래 두 함수를 대신 씁니다.
# 둘 다 DataFrame(분기, 매출액, 영업이익, 당기순이익, ROE) 형태를 반환합니다. (분기 예: "2024Q1")
#   - get_korea_quarterly_trend(ticker_code)
#   - get_us_quarterly_trend(ticker)

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

EMPTY_RESULT = {
    "income": pd.DataFrame(),
    "trend": pd.DataFrame(),
    "per": None,
    "pbr": None,
    "roe": None,
}


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
def _get_dart_accounts(corp_code: str, year: int, reprt_code: str = "11011") -> pd.DataFrame:
    """특정 사업연도/보고서의 주요 계정(매출액/영업이익 등) 원본 데이터를 가져옵니다.

    reprt_code(보고서 종류): 11013=1분기보고서, 11012=반기보고서, 11014=3분기보고서, 11011=사업보고서(연간)
    """
    res = requests.get(
        f"{DART_BASE_URL}/fnlttSinglAcnt.json",
        params={
            "crtfc_key": DART_API_KEY,
            "corp_code": corp_code,
            "bsns_year": str(year),
            "reprt_code": reprt_code,
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


def _extract_accounts(df: pd.DataFrame) -> dict:
    """DART 계정 목록에서 매출액/영업이익/당기순이익/자본총계를 뽑아 딕셔너리로 만듭니다.

    DART는 적자 기업의 경우 "영업이익(손실)", "당기순이익(손실)"처럼
    계정명 뒤에 괄호가 붙기도 해서, 정확히 일치하는 대신 접두어로 찾습니다.
    """
    result = {}
    for account in ["매출액", "영업이익", "당기순이익", "자본총계"]:
        match = df[df["account_nm"].str.startswith(account)]
        result[account] = _to_number(match.iloc[0]["thstrm_amount"]) if not match.empty else None
    return result


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
        df = _get_dart_accounts(corp_code, year)  # 기본값(reprt_code="11011")은 사업보고서(연간)
        if df.empty:
            continue

        row = {"연도": year, **_extract_accounts(df)}
        records.append(row)

        if len(records) == 3:  # 3개년치를 모았으면 그만 조회합니다.
            break

    if not records:
        return EMPTY_RESULT

    full_df = pd.DataFrame(records).sort_values("연도").reset_index(drop=True)

    # 연도별 ROE(자기자본이익률) = 그 해 당기순이익 / 그 해 자본총계 * 100
    # 이미 3개년치 자본총계를 위에서 받아왔으므로, 별도 API 호출 없이 계산만 하면 됩니다.
    full_df["ROE"] = full_df.apply(
        lambda row: (row["당기순이익"] / row["자본총계"] * 100)
        if row["당기순이익"] is not None and row["자본총계"]
        else None,
        axis=1,
    )

    # PER/PBR 계산에 필요한 최신 연도의 순이익/자본총계, ROE는 방금 계산한 값을 재사용합니다.
    latest = full_df.iloc[-1]
    net_income_latest = latest.get("당기순이익")
    equity_latest = latest.get("자본총계")
    roe = latest.get("ROE")

    income_df = full_df.drop(columns=["자본총계", "ROE"])  # 실적 표에는 손익 항목만 표시
    trend_df = full_df.drop(columns=["자본총계"])  # 추이 그래프에는 ROE까지 포함

    per = pbr = None

    # 현재 시가총액은 FinanceDataReader의 KRX 종목 목록에서 가져옵니다.
    listing = get_krx_listing()
    matched = listing.loc[listing["Code"] == ticker_code]
    if not matched.empty:
        marcap = matched.iloc[0].get("Marcap")
        if marcap and net_income_latest:
            per = marcap / net_income_latest
        if marcap and equity_latest:
            pbr = marcap / equity_latest

    return {"income": income_df, "trend": trend_df, "per": per, "pbr": pbr, "roe": roe}


# 분기 번호(1~4) -> DART 보고서 코드
# DART는 "4분기보고서"를 따로 내지 않기 때문에, 4분기 실적은 연간(사업보고서)에서
# 1~3분기 실적을 뺀 값으로 직접 계산해야 합니다.
_QUARTER_REPRT_CODES = {
    1: "11013",  # 1분기보고서
    2: "11012",  # 반기보고서 (thstrm_amount가 2분기 단독 3개월 실적으로 내려옵니다)
    3: "11014",  # 3분기보고서 (역시 3분기 단독 3개월 실적)
    4: "11011",  # 사업보고서 (1~4분기 누적/연간 실적)
}


@st.cache_data(ttl=3600)
def get_korea_quarterly_trend(ticker_code: str) -> pd.DataFrame:
    """한국 주식의 최근 3년(최대 12개) 분기별 매출액/영업이익/당기순이익/ROE를 가져옵니다.

    데이터가 부족한(최근 상장/비상장 등) 종목은 빈 DataFrame을 반환하니,
    호출하는 쪽(app.py)에서 연간 데이터로 대신 보여주면 됩니다.
    """
    if not DART_API_KEY:
        return pd.DataFrame()

    corp_map = _get_dart_corp_code_map()
    corp_code = corp_map.get(ticker_code)
    if corp_code is None:
        return pd.DataFrame()

    current_year = datetime.today().year
    rows = []
    # 최근 4개년을 넉넉히 훑어서 데이터를 모으고, 마지막에 최신 12개 분기만 남깁니다.
    # (올해는 아직 일부 분기만 공시됐을 수 있어서, 그만큼을 다른 연도로 채우기 위함입니다)
    for year in [current_year, current_year - 1, current_year - 2, current_year - 3]:
        quarter_accounts = {}
        for q in (1, 2, 3):
            df = _get_dart_accounts(corp_code, year, _QUARTER_REPRT_CODES[q])
            if not df.empty:
                quarter_accounts[q] = _extract_accounts(df)

        annual_df = _get_dart_accounts(corp_code, year, _QUARTER_REPRT_CODES[4])
        annual_accounts = _extract_accounts(annual_df) if not annual_df.empty else None

        # 1~3분기는 보고서에서 받은 값을 그대로 사용합니다 (이미 해당 분기 단독 실적).
        for q in (1, 2, 3):
            if q in quarter_accounts:
                acc = quarter_accounts[q]
                rows.append({"연도": year, "분기번호": q, **acc})

        # 4분기 = 연간 실적 - (1분기 + 2분기 + 3분기). 셋 중 하나라도 없으면 계산할 수 없습니다.
        if annual_accounts and all(q in quarter_accounts for q in (1, 2, 3)):
            q4 = {}
            calculable = True
            for item in ["매출액", "영업이익", "당기순이익"]:
                parts = [quarter_accounts[q][item] for q in (1, 2, 3)]
                if annual_accounts[item] is None or any(p is None for p in parts):
                    calculable = False
                    break
                q4[item] = annual_accounts[item] - sum(parts)
            if calculable:
                q4["자본총계"] = annual_accounts["자본총계"]  # 자본총계는 특정 시점 값이라 그대로 사용
                rows.append({"연도": year, "분기번호": 4, **q4})

    if not rows:
        return pd.DataFrame()

    df = pd.DataFrame(rows).sort_values(["연도", "분기번호"]).reset_index(drop=True)

    df["ROE"] = df.apply(
        lambda row: (row["당기순이익"] / row["자본총계"] * 100)
        if row["당기순이익"] is not None and row["자본총계"]
        else None,
        axis=1,
    )
    df["분기"] = df.apply(lambda row: f"{int(row['연도'])}Q{int(row['분기번호'])}", axis=1)

    df = df[["분기", "매출액", "영업이익", "당기순이익", "ROE"]]
    return df.tail(12).reset_index(drop=True)  # 오래된 것부터 정렬돼 있으니, 뒤에서 12개 = 최신 12개


# ------------------------------
# 미국 주식 (yfinance)
# ------------------------------

_EQUITY_ROW_CANDIDATES = ["Stockholders Equity", "Common Stock Equity", "Total Equity Gross Minority Interest"]


def _find_equity_row(balance_df: pd.DataFrame) -> Optional[pd.Series]:
    """대차대조표(balance_sheet)에서 자기자본에 해당하는 행을 찾습니다.

    yfinance는 기업/버전에 따라 행 이름이 조금씩 달라서, 자주 쓰이는 이름들을 순서대로 찾아봅니다.
    """
    for candidate in _EQUITY_ROW_CANDIDATES:
        if candidate in balance_df.index:
            return balance_df.loc[candidate]
    return None


@st.cache_data(ttl=3600)
def get_us_financials(ticker: str) -> dict:
    """미국 주식의 최근 3년 실적과 PER/PBR/ROE를 가져옵니다."""
    yf_ticker = yf.Ticker(ticker)

    financials_df = yf_ticker.financials  # 연간 손익계산서 (최근 연도가 첫 컬럼)
    income_df = pd.DataFrame()
    trend_df = pd.DataFrame()
    if not financials_df.empty:
        rows = [r for r in ["Total Revenue", "Operating Income", "Net Income"] if r in financials_df.index]
        full_df = financials_df.loc[rows].iloc[:, :3]  # 최근 3개년만 사용
        full_df = full_df.rename(columns=lambda c: c.year)
        full_df = full_df.T.sort_index()
        full_df = full_df.rename(
            columns={
                "Total Revenue": "매출액",
                "Operating Income": "영업이익",
                "Net Income": "당기순이익",
            }
        )
        full_df.index.name = "연도"

        # 연도별 ROE 계산을 위해 대차대조표에서 같은 연도의 자기자본을 가져옵니다.
        equity_row = _find_equity_row(yf_ticker.balance_sheet)
        if equity_row is not None:
            equity_by_year = equity_row.rename(index=lambda c: c.year)
            full_df["ROE"] = full_df.apply(
                lambda row: (row["당기순이익"] / equity_by_year[row.name] * 100)
                if row.name in equity_by_year.index and equity_by_year[row.name]
                else None,
                axis=1,
            )
        else:
            full_df["ROE"] = None

        full_df = full_df.reset_index()
        income_df = full_df.drop(columns=["ROE"])  # 실적 표에는 손익 항목만 표시
        trend_df = full_df  # 추이 그래프에는 ROE까지 포함

    info = yf_ticker.info
    per = info.get("trailingPE")
    pbr = info.get("priceToBook")
    roe = info.get("returnOnEquity")
    if roe is not None:
        roe = roe * 100  # 퍼센트로 변환

    return {"income": income_df, "trend": trend_df, "per": per, "pbr": pbr, "roe": roe}


@st.cache_data(ttl=3600)
def get_us_quarterly_trend(ticker: str) -> pd.DataFrame:
    """미국 주식의 분기별 매출액/영업이익/당기순이익/ROE를 가져옵니다.

    주의: yfinance(Yahoo Finance)는 무료로는 최근 4~5개 분기 정도까지만 제공해서,
    한국 주식(DART)처럼 3년(12개) 전체를 채우기는 어려울 수 있습니다.
    받아올 수 있는 만큼만 반환하고, 부족한 부분은 app.py에서 안내 문구로 알려줍니다.
    """
    yf_ticker = yf.Ticker(ticker)
    quarterly_df = yf_ticker.quarterly_financials
    if quarterly_df.empty:
        return pd.DataFrame()

    rows = [r for r in ["Total Revenue", "Operating Income", "Net Income"] if r in quarterly_df.index]
    df = quarterly_df.loc[rows].T.sort_index()  # 오래된 분기가 위로 오도록 정렬
    df = df.rename(
        columns={
            "Total Revenue": "매출액",
            "Operating Income": "영업이익",
            "Net Income": "당기순이익",
        }
    )

    # 분기별 ROE 계산을 위해 분기별 대차대조표에서 같은 분기말 자기자본을 가져옵니다.
    equity_row = _find_equity_row(yf_ticker.quarterly_balance_sheet)
    if equity_row is not None:
        df["ROE"] = df.apply(
            lambda row: (row["당기순이익"] / equity_row[row.name] * 100)
            if row.name in equity_row.index and equity_row[row.name]
            else None,
            axis=1,
        )
    else:
        df["ROE"] = None

    # 분기 라벨(예: 2024Q1)은 보고 기간 종료일이 속한 달력 분기 기준으로 만듭니다.
    df["분기"] = [f"{d.year}Q{(d.month - 1) // 3 + 1}" for d in df.index]

    df = df.reset_index(drop=True)[["분기", "매출액", "영업이익", "당기순이익", "ROE"]]
    return df.tail(12).reset_index(drop=True)
