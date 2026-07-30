# 투자원칙.md의 "5대 핵심 펀더멘털 지표"를 실제 데이터로 계산하는 모듈입니다.
#
# 5개 축 각각 0~100점으로 채점하되, 절대로 임의의(지어낸) 값을 채워넣지 않습니다.
# 데이터를 못 가져온 세부 항목은 점수 계산에서 제외하고 "N개 항목 누락"으로 표시합니다.
# (5개 축을 합산한 "종합점수"는 만들지 않습니다 - 축별로만 봅니다)
#
# 반환 형식은 5개 축 모두 아래처럼 통일합니다.
#   {
#       "score": float | None,           # 0~100점 (채점 가능한 항목이 하나도 없으면 None)
#       "missing_count": int,            # 데이터를 못 가져온 세부 항목 수
#       "items": [                       # 세부 항목별 근거 (점수 반영 여부와 무관하게 전부 표시)
#           {"label": str, "value_text": str, "score": float|None, "detail": str},
#           ...
#       ],
#       "citation": str,                 # 투자원칙.md에서 이 축에 해당하는 문장 인용
#       "note": str | None,              # 경고/안내 문구 (예: 시클리컬 업종 경고)
#   }

import json
import os
import re
import zipfile
import io
from datetime import datetime, timedelta
from typing import Optional

import FinanceDataReader as fdr
import numpy as np
import pandas as pd
import requests
import streamlit as st
import yfinance as yf

from config import DART_API_KEY
import financials
from financials import DartUnavailableError
from stock_data import get_korea_stock_name, get_krx_listing

DART_BASE_URL = "https://opendart.fss.or.kr/api"


# ------------------------------
# 투자원칙.md 인용문 (하드코딩 - 문서 원문을 그대로 옮겨둔 것)
# ------------------------------

CITATIONS = {
    "관리": "기업 리더가 주주 가치를 존중하는지가 장기 성과와 직결된다. 도덕적 해이가 없는 경영진은 투명한 지배구조를 유지하며, 이는 합리적인 배당 정책으로 이어져 주주에게 보상이 환원되는 토대가 된다.",
    "해자": "매출의 지속적 성장이 가능한 구조인지, 경쟁사가 침범할 수 없는 경제적 해자(Moat)를 구축했는지 평가한다. 불황기에도 기업을 생존하게 하는 강력한 방어기제다.",
    "밸류": "현재 이익 대비 주가가 합리적인 수준인지 분석하되, 업종별 특성을 반드시 고려한다. 예를 들어 반도체 같은 시클리컬(경기 순환) 업종은 현재의 낮은 PER이 오히려 정점 신호일 수 있고, 업황 회복기에는 지표 해석이 달라진다.",
    "환원": "이익을 주주와 공유하는 기업은 장기 복리 효과를 극대화하는 촉매 역할을 한다. 배당은 단순한 현금 흐름이 아니라 기업의 재무적 건전성을 증명하는 가장 강력한 지표다.",
    "비전": "AI 시대와 같은 거대 패러다임 변화 속에서 기업이 어떻게 비즈니스를 재정의하는지 관찰한다. 창의성이 결여된 기업은 자본 효율성을 유지할 수 없다.",
}


# ------------------------------
# 업종별 대표 peer 종목 (완전한 "업종 평균"이 아니라, 가벼운 참고용 근사치입니다)
# ------------------------------

KR_SECTOR_PEERS = {
    # 업종명: {"peers": [대표 종목코드 3~5개], "cyclical": 시클리컬 여부}
    #
    # 처음에는 DART의 업종코드(induty_code)로 "같은 업종"을 자동 판별하려 했지만,
    # 삼성전자(264)와 SK하이닉스(2612)처럼 직관적으로는 같은 업종(반도체)이어도
    # DART의 세부 업종코드는 서로 다른 경우가 많아서 신뢰할 수 없었습니다.
    # 그래서 "이 종목이 아래 peers 목록에 직접 들어있는가"로만 판별합니다.
    # (peers 목록에 없는 종목은 비교를 생략합니다 - 완전한 업종 분류가 아닌, 소수 대표주 비교용입니다)
    # peer가 1~2개뿐이면 "중앙값"이 사실상 그 1~2개 값 그대로라 비교의 의미가 약해서,
    # 주요 업종은 모두 최소 3개씩으로 채웠습니다. (추가한 종목은 모두 DART/KRX 상장 여부를
    # 실제로 조회해서 확인한 것들입니다. 동국제강은 2023년 지주회사 전환으로 종목코드가
    # 001230(동국홀딩스, 지주사)에서 460860(동국제강, 사업회사)으로 바뀌어서 후자를 썼습니다)
    "반도체": {"peers": ["005930", "000660", "042700"], "cyclical": True},  # +한미반도체
    "자동차": {"peers": ["005380", "000270", "012330"], "cyclical": True},  # +현대모비스
    "화학": {"peers": ["051910", "011170", "011780"], "cyclical": True},  # +금호석유
    "철강": {"peers": ["005490", "004020", "460860"], "cyclical": True},  # +동국제강
    "조선": {"peers": ["009540", "010140", "042660"], "cyclical": True},  # +한화오션
    "해운": {"peers": ["011200", "028670", "005880"], "cyclical": True},  # +팬오션, +대한해운
    "정유": {"peers": ["096770", "010950", "078930"], "cyclical": True},  # +S-Oil, +GS
    "인터넷/플랫폼": {"peers": ["035420", "035720", "259960"], "cyclical": False},  # +크래프톤
    "은행/금융지주": {"peers": ["105560", "055550", "086790"], "cyclical": False},
    "통신": {"peers": ["030200", "017670", "032640"], "cyclical": False},  # +LG유플러스
}

US_SECTOR_PEERS = {
    # yfinance의 industry 문자열: {"peers": [...], "cyclical": ...}
    # (peer들의 yfinance industry 태그가 정확히 같을 필요는 없습니다 - 이 표는 대상 종목의
    #  industry로 어느 그룹을 쓸지만 찾고, peer 목록 자체는 사람이 고른 비교군입니다)
    "Semiconductors": {"peers": ["NVDA", "INTC", "TXN"], "cyclical": True},
    "Consumer Electronics": {"peers": ["AAPL", "SONY", "GRMN"], "cyclical": False},  # +Garmin
    "Software - Infrastructure": {"peers": ["MSFT", "ORCL", "CRM"], "cyclical": False},
    "Internet Content & Information": {"peers": ["GOOGL", "META", "NFLX"], "cyclical": False},  # +Netflix
    "Auto Manufacturers": {"peers": ["TSLA", "GM", "F"], "cyclical": True},
    "Steel": {"peers": ["NUE", "X", "CLF"], "cyclical": True},  # +Cleveland-Cliffs
    "Oil & Gas Integrated": {"peers": ["XOM", "CVX", "COP"], "cyclical": True},  # +ConocoPhillips
    "Chemicals": {"peers": ["DOW", "LYB", "LIN"], "cyclical": True},  # +Linde
    "Banks - Diversified": {"peers": ["JPM", "BAC", "WFC"], "cyclical": False},
}


# ------------------------------
# peer 재무 데이터 캐시 (build_peer_cache.py로 미리 만들어둔 파일을 읽습니다)
# ------------------------------
#
# peer는 종목 하나를 볼 때마다 여러 개(최대 5개)를 매번 실시간으로 조회해야 해서,
# DART 호출 횟수를 크게 늘리는 원인이었습니다. peer 값은 분기 실적 발표 전까지는
# 자주 바뀌지 않으므로, build_peer_cache.py를 필요할 때 한 번 실행해서
# peer_cache.json에 미리 계산해두고, 앱은 그 파일을 읽기만 하도록 바꿨습니다.
# 캐시에 없는 peer(파일이 없거나 새로 추가된 종목)는 예전처럼 실시간으로 조회합니다.

PEER_CACHE_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), "peer_cache.json")


@st.cache_data(ttl=60 * 60)  # 파일 내용 자체는 자주 안 바뀌므로 1시간이면 충분합니다.
def _load_peer_cache() -> dict:
    """미리 계산해둔 peer_cache.json을 읽습니다.

    파일이 없거나 손상됐으면 빈 딕셔너리를 반환합니다 - 이 경우 peer 조회는
    (느리지만) 예전처럼 매번 실시간으로 이뤄지니 기능이 완전히 끊기지는 않습니다.
    """
    if not os.path.exists(PEER_CACHE_PATH):
        return {}
    try:
        with open(PEER_CACHE_PATH, "r", encoding="utf-8") as f:
            return json.load(f)
    except (json.JSONDecodeError, OSError):
        return {}


def _get_peer_metrics(market: str, ticker: str) -> dict:
    """peer 종목의 PER/PBR/영업이익률/매출총이익률을 가져옵니다.

    peer_cache.json에 값이 있으면 그대로 쓰고(DART/yfinance 호출 없음),
    없으면 그 peer 하나에 한해서만 실시간으로 조회합니다(실패해도 죽지 않게 감쌉니다).
    """
    cache = _load_peer_cache()
    cached = cache.get(market, {}).get(ticker)
    if cached is not None:
        return cached

    if market == "KR":
        fin, _ = _safe_call(financials.get_korea_financials, ticker, years=1)
        gross_margin, _ = _safe_call(get_gross_margin, ticker)
    else:
        fin, _ = _safe_call(financials.get_us_financials, ticker, years=1)
        gross_margin, _ = _safe_call(_get_us_gross_margin, ticker)
    fin = fin or dict(financials.EMPTY_RESULT)

    operating_margin = None
    if not fin["income"].empty:
        row = fin["income"].iloc[-1]
        # .get()을 씁니다 - 미국 은행주(JPM 등)처럼 "영업이익"이라는 표준 계정 자체가
        # 없는 업종은 row["영업이익"]처럼 대괄호로 접근하면 KeyError가 납니다.
        revenue = row.get("매출액")
        operating_income = row.get("영업이익")
        if revenue and operating_income is not None:
            operating_margin = operating_income / revenue * 100

    return {"per": fin["per"], "pbr": fin["pbr"], "gross_margin": gross_margin, "operating_margin": operating_margin}


# ------------------------------
# 공용 유틸리티
# ------------------------------

def _safe_call(fn, *args, **kwargs) -> tuple:
    """DART/yfinance 등 외부 API를 호출하는 함수를 감싸서, 실패해도 예외가 앱까지
    올라가지 않게 합니다. (Streamlit Cloud처럼 DART 접속이 느리거나 끊기는 환경 대비)

    반환값: (결과 또는 None, 실패 여부)
    실패 여부가 True인 항목은 "데이터 없음 (조회 실패)"처럼 표시해서, 진짜로 데이터가
    없는 경우와 구분합니다. DartUnavailableError뿐 아니라 예상 못한 다른 오류(라이브러리
    버전 차이, 파싱 실패 등)까지 넓게 잡아서, 이 축 하나가 죽어도 나머지 축은 정상 표시되게 합니다.
    """
    try:
        return fn(*args, **kwargs), False
    except Exception:
        return None, True


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


def _score_from_bands(value: Optional[float], bands: list) -> Optional[float]:
    """value를 (임계값, 점수) 목록과 비교해서 점수를 매깁니다.

    bands는 임계값 내림차순으로 정렬돼 있어야 합니다.
    예: [(20, 100), (10, 70), (5, 40), (0, 20)] 이면
        value>=20 -> 100, value>=10 -> 70, value>=5 -> 40, 그 외(0 이상) -> 20
    """
    if value is None:
        return None
    for threshold, score in bands:
        if value >= threshold:
            return float(score)
    return float(bands[-1][1])


def _is_missing(value) -> bool:
    """None은 물론, pandas가 결측치를 표현할 때 쓰는 NaN도 함께 판별합니다.

    DataFrame에 숫자와 None이 섞여 있으면 pandas가 None을 NaN으로 바꿔버리는데,
    "value is None"만으로는 NaN을 걸러내지 못해서 계산 결과가 nan으로 새는 버그가 있었습니다.
    """
    return value is None or (isinstance(value, float) and pd.isna(value))


def _avg(values: list) -> Optional[float]:
    valid = [v for v in values if not _is_missing(v)]
    return float(np.mean(valid)) if valid else None


def _median(values: list) -> Optional[float]:
    valid = [v for v in values if not _is_missing(v)]
    return float(np.median(valid)) if valid else None


def _std(values: list) -> Optional[float]:
    valid = [v for v in values if not _is_missing(v)]
    return float(np.std(valid)) if len(valid) >= 2 else None


def _cagr(first: Optional[float], last: Optional[float], periods: int) -> Optional[float]:
    """first -> last 로 변할 때 연평균 성장률(%)을 계산합니다. (periods = 구간 수, 예: 3년치면 2)"""
    if first is None or last is None or first <= 0 or periods <= 0:
        return None
    return ((last / first) ** (1 / periods) - 1) * 100


# 채점 가능한 세부 항목이 이 개수 이하면, 그 축 점수는 "신뢰도 낮음"으로 표시합니다.
# 항목 1개만으로 0~100점을 매기면 극단값(0점/100점)이 나오기 쉬워서 오해를 줄 수 있기 때문입니다.
MIN_RELIABLE_ITEM_COUNT = 2


def _make_axis_result(score_items: list, citation: str, note: Optional[str] = None) -> dict:
    """세부 항목 리스트에서 점수를 평균 내 축 결과를 만듭니다. (가중치 없는 단순 평균용)"""
    scored = [it["score"] for it in score_items if it["score"] is not None]
    missing = sum(1 for it in score_items if it["score"] is None)
    axis_score = float(np.mean(scored)) if scored else None
    reliability = "low" if len(scored) < MIN_RELIABLE_ITEM_COUNT else "normal"
    return {
        "score": axis_score,
        "missing_count": missing,
        "scored_count": len(scored),
        "reliability": reliability,
        "items": score_items,
        "citation": citation,
        "note": note,
        "fetch_failed": any(it.get("fetch_failed") for it in score_items),
    }


def _make_weighted_axis_result(score_items: list, weights: list, citation: str, note: Optional[str] = None) -> dict:
    """세부 항목을 가중평균으로 합산합니다. (② 해자 축처럼 특정 항목 비중을 높이고 싶을 때 사용)

    score_items와 weights는 순서가 1:1로 대응해야 합니다.
    점수가 없는(None) 항목은 제외하고, 남은 항목들의 가중치 비율로 다시 정규화합니다.
    """
    pairs = [(it["score"], w) for it, w in zip(score_items, weights) if it["score"] is not None]
    missing = sum(1 for it in score_items if it["score"] is None)
    if not pairs:
        axis_score = None
    else:
        total_weight = sum(w for _, w in pairs)
        axis_score = sum(s * w for s, w in pairs) / total_weight
    reliability = "low" if len(pairs) < MIN_RELIABLE_ITEM_COUNT else "normal"
    return {
        "score": axis_score,
        "missing_count": missing,
        "scored_count": len(pairs),
        "reliability": reliability,
        "items": score_items,
        "citation": citation,
        "note": note,
        "fetch_failed": any(it.get("fetch_failed") for it in score_items),
    }


# ------------------------------
# DART 공용 호출 헬퍼 (financials.py의 것과 별도로, 이 모듈 전용 엔드포인트들을 다룹니다)
# ------------------------------

@st.cache_data(ttl=3600)
def _dart_get(endpoint: str, corp_code: str, year: int, reprt_code: str = "11011") -> pd.DataFrame:
    """정기보고서 주요정보류 API 공통 호출 (list 형태 응답을 DataFrame으로).

    financials.py의 재시도(2회)+30초 타임아웃 헬퍼를 그대로 씁니다. 접속 자체가
    실패하면(DartUnavailableError) 여기서 잡지 않고 그대로 던져서, [email protected]_data가
    그 실패를 캐시하지 않게 합니다 (성공했을 때만 캐시되어, 다음 요청 때 재시도됨).
    이 예외는 이 함수를 부르는 축(axis) 함수 쪽에서 항목 단위로 잡아서
    "데이터 없음(조회 실패)"로 표시합니다.
    """
    res = financials._dart_request(
        f"{DART_BASE_URL}/{endpoint}",
        {
            "crtfc_key": DART_API_KEY,
            "corp_code": corp_code,
            "bsns_year": str(year),
            "reprt_code": reprt_code,
        },
    )
    data = res.json()
    if data.get("status") != "000":
        return pd.DataFrame()
    return pd.DataFrame(data["list"])


def _get_corp_code(ticker_code: str) -> Optional[str]:
    corp_map = financials.get_dart_corp_code_map()
    return corp_map.get(ticker_code)


def _find_kr_sector(ticker_code: str) -> Optional[str]:
    """이 종목이 KR_SECTOR_PEERS의 어느 peer 목록에 직접 포함되어 있는지 찾습니다."""
    for sector, info in KR_SECTOR_PEERS.items():
        if ticker_code in info["peers"]:
            return sector
    return None


# ==============================================================
# ① 경영진의 역량과 도덕성
# ==============================================================

@st.cache_data(ttl=60 * 60 * 24)
def get_major_shareholder_pct(ticker_code: str) -> Optional[float]:
    """최대주주+특수관계인 지분율(%)을 가져옵니다. (점수화하지 않고 정보로만 표시)

    DART가 "계"(합계) 행을 이미 계산해서 주기 때문에, 그 행을 그대로 사용합니다.
    """
    if not DART_API_KEY:
        return None
    corp_code = _get_corp_code(ticker_code)
    if corp_code is None:
        return None

    df = _dart_get("hyslrSttus.json", corp_code, datetime.today().year - 1)
    if df.empty or "nm" not in df.columns:
        return None
    total_rows = df[df["nm"] == "계"]
    if total_rows.empty:
        return None
    return _to_number(total_rows.iloc[0]["trmend_posesn_stock_qota_rt"])


@st.cache_data(ttl=60 * 60 * 24)
def get_dividend_streak_years(ticker_code: str, max_lookback: int = 15) -> Optional[int]:
    """배당을 몇 년 연속으로 지급했는지 계산합니다. (최신 연도부터 역순으로 확인)"""
    if not DART_API_KEY:
        return None
    corp_code = _get_corp_code(ticker_code)
    if corp_code is None:
        return None

    current_year = datetime.today().year
    streak = 0
    checked_any = False
    for year in range(current_year - 1, current_year - 1 - max_lookback, -1):
        df = _dart_get("alotMatter.json", corp_code, year)
        if df.empty or "se" not in df.columns:
            break  # 그 이전 연도는 전자공시가 없을 가능성이 높아 조회를 멈춥니다.
        checked_any = True

        row = df[df["se"] == "현금배당금총액(백만원)"]
        amount = _to_number(row.iloc[0]["thstrm"]) if not row.empty else None
        if amount and amount > 0:
            streak += 1
        else:
            break  # 배당 없는 연도를 만나면 연속 기록이 끊깁니다.

    return streak if checked_any else None


@st.cache_data(ttl=3600)
def get_paid_capital_increase_count(ticker_code: str, years: int = 5) -> Optional[int]:
    """최근 N년간 유상증자 횟수를 셉니다."""
    if not DART_API_KEY:
        return None
    corp_code = _get_corp_code(ticker_code)
    if corp_code is None:
        return None

    current_year = datetime.today().year
    count = 0
    checked_any = False
    for year in range(current_year - 1, current_year - 1 - years, -1):
        df = _dart_get("irdsSttus.json", corp_code, year)
        if df.empty or "isu_dcrs_stle" not in df.columns:
            continue
        checked_any = True
        count += df["isu_dcrs_stle"].astype(str).str.contains("유상증자").sum()

    return int(count) if checked_any else None


@st.cache_data(ttl=60 * 60 * 24)
def get_audit_opinion(ticker_code: str) -> Optional[dict]:
    """가장 최근 사업연도의 감사의견(적정의견/한정의견/부적정의견/의견거절)을 가져옵니다.

    DART는 회사에 따라 최근 1~2개년치 감사의견 필드(adt_opinion)가 비어있는 경우가 있어서
    (현대차로 실측 확인: 2024·2025년은 필드 자체가 없고, 2023년부터는 정상 제공),
    최대 5개년까지 거슬러 올라가며 실제로 값이 있는 가장 최근 연도를 찾습니다.
    반환값에 어느 연도 기준인지(year)도 함께 담아서, 오래된 연도 값을 최신인 것처럼
    보여주는 일이 없도록 합니다.
    """
    if not DART_API_KEY:
        return None
    corp_code = _get_corp_code(ticker_code)
    if corp_code is None:
        return None

    current_year = datetime.today().year
    for year in range(current_year - 1, current_year - 6, -1):
        df = _dart_get("accnutAdtorNmNdAdtOpinion.json", corp_code, year)
        if not df.empty and "adt_opinion" in df.columns:
            return {"opinion": df.iloc[0]["adt_opinion"], "year": year}
    return None


def _score_dividend_streak(years: Optional[int]) -> Optional[float]:
    return _score_from_bands(years, [(10, 100), (6, 80), (3, 60), (1, 30), (0, 0)])


def _score_capital_increase_count(count: Optional[int]) -> Optional[float]:
    if count is None:
        return None
    return _score_from_bands(-count, [(0, 100), (-1, 70), (-2, 40), (-3, 10)])


def _score_audit_opinion(opinion: Optional[str]) -> Optional[float]:
    if opinion is None:
        return None
    if "적정" in opinion and "부적정" not in opinion:
        return 100.0
    if "한정" in opinion:
        return 40.0
    if "부적정" in opinion or "거절" in opinion:
        return 0.0
    return None


def _score_roe_std(std: Optional[float]) -> Optional[float]:
    if std is None:
        return None
    return _score_from_bands(-std, [(-3, 100), (-6, 70), (-10, 40), (-999, 10)])


def get_korea_management_axis(ticker_code: str) -> dict:
    """① 경영진의 역량과 도덕성 축을 계산합니다. (한국 주식)

    각 데이터 조회를 _safe_call로 감싸서, DART 접속이 실패한 항목만 "조회 실패"로
    표시하고 나머지 항목은 정상적으로 채점되게 합니다.
    """
    major_pct, major_pct_failed = _safe_call(get_major_shareholder_pct, ticker_code)
    streak, streak_failed = _safe_call(get_dividend_streak_years, ticker_code)
    capital_count, capital_failed = _safe_call(get_paid_capital_increase_count, ticker_code)
    audit, audit_failed = _safe_call(get_audit_opinion, ticker_code)
    opinion = audit["opinion"] if audit else None

    fin5, fin5_failed = _safe_call(financials.get_korea_financials, ticker_code, years=5)
    roe_values = fin5["trend"]["ROE"].tolist() if fin5 and not fin5["trend"].empty else []
    roe_std = _std(roe_values)
    roe_std_failed = fin5_failed or (fin5.get("fetch_failed") if fin5 else False)

    def _dv(text_if_failed: str, value_text: str, failed: bool) -> str:
        return text_if_failed if failed else value_text

    items = [
        {
            "label": "배당 연속 지급 연수",
            "value_text": _dv("데이터 없음 (조회 실패)", f"{streak}년 연속" if streak is not None else "데이터 없음", streak_failed),
            "score": _score_dividend_streak(streak),
            "detail": "최근 연도부터 역순으로 현금배당금총액이 있는 연도를 셉니다. (DART 배당에 관한 사항)",
            "fetch_failed": streak_failed,
        },
        {
            "label": "최근 5년 유상증자 횟수",
            "value_text": _dv("데이터 없음 (조회 실패)", f"{capital_count}회" if capital_count is not None else "데이터 없음", capital_failed),
            "score": _score_capital_increase_count(capital_count),
            "detail": "최근 5개 사업연도의 증자(감자) 현황에서 '유상증자'로 표시된 건수를 셉니다.",
            "fetch_failed": capital_failed,
        },
        {
            "label": "감사의견",
            "value_text": _dv("데이터 없음 (조회 실패)", (f"{opinion} ({audit['year']}년 기준)" if audit else "데이터 없음"), audit_failed),
            "score": _score_audit_opinion(opinion),
            "detail": "회계감사인 감사의견입니다. DART가 최근 1~2개년치는 이 항목을 비워두는 경우가 있어, 실제로 값이 있는 가장 최근 연도(최대 5년 전까지)를 찾아서 보여줍니다.",
            "fetch_failed": audit_failed,
        },
        {
            "label": "ROE 5년 표준편차",
            "value_text": _dv("데이터 없음 (조회 실패)", f"{roe_std:.2f}%p" if roe_std is not None else "데이터 없음", roe_std_failed),
            "score": _score_roe_std(roe_std),
            "detail": "최근 5개년 ROE의 표준편차입니다. 낮을수록 자본배분이 안정적이라고 봅니다.",
            "fetch_failed": roe_std_failed,
        },
    ]

    result = _make_axis_result(items, CITATIONS["관리"])
    result["info"] = {
        "label": "최대주주+특수관계인 지분율 (참고용, 점수 미반영)",
        "value_text": _dv("데이터 없음 (조회 실패)", f"{major_pct:.2f}%" if major_pct is not None else "데이터 없음", major_pct_failed),
        "detail": "지분율이 높다고 항상 좋은 것도, 낮다고 항상 나쁜 것도 아니라서(경영권 안정 vs 소액주주 이익 침해 우려는 반대 방향) 점수화하지 않고 참고 정보로만 표시합니다.",
    }
    return result


# ==============================================================
# ② 비즈니스 모델의 확장성 및 진입 장벽 (해자)
# ==============================================================

@st.cache_data(ttl=3600)
def get_gross_margin(ticker_code: str) -> Optional[float]:
    """최신 연도 매출총이익률(%)을 가져옵니다. (전체 재무제표 API 필요 - 주요계정에는 없음)"""
    if not DART_API_KEY:
        return None
    corp_code = _get_corp_code(ticker_code)
    if corp_code is None:
        return None

    current_year = datetime.today().year
    for year in [current_year - 1, current_year - 2, current_year - 3]:
        try:
            res = financials._dart_request(
                f"{DART_BASE_URL}/fnlttSinglAcntAll.json",
                {
                    "crtfc_key": DART_API_KEY, "corp_code": corp_code,
                    "bsns_year": str(year), "reprt_code": "11011", "fs_div": "CFS",
                },
            )
        except DartUnavailableError:
            continue  # 이 연도 조회만 실패한 것으로 보고 다른 연도를 시도합니다.
        data = res.json()
        if data.get("status") != "000":
            continue
        df = pd.DataFrame(data["list"])
        gross = df[df["account_nm"] == "매출총이익"]
        revenue = df[df["account_nm"] == "매출액"]
        if gross.empty or revenue.empty:
            continue
        gross_amt = _to_number(gross.iloc[0]["thstrm_amount"])
        revenue_amt = _to_number(revenue.iloc[0]["thstrm_amount"])
        if gross_amt is not None and revenue_amt:
            return gross_amt / revenue_amt * 100
    return None


def _get_kr_sector_for_ticker(ticker_code: str) -> Optional[str]:
    return _find_kr_sector(ticker_code)


def _kr_peer_operating_margins(sector: str, exclude_ticker: str) -> list:
    """같은 업종 대표 종목들의 최신 연도 영업이익률(%) 목록을 가져옵니다. (peer_cache.json 우선 사용)"""
    peers = [p for p in KR_SECTOR_PEERS[sector]["peers"] if p != exclude_ticker]
    margins = [_get_peer_metrics("KR", p)["operating_margin"] for p in peers]
    return [m for m in margins if m is not None]


def _kr_peer_gross_margins(sector: str, exclude_ticker: str) -> list:
    peers = [p for p in KR_SECTOR_PEERS[sector]["peers"] if p != exclude_ticker]
    margins = [_get_peer_metrics("KR", p)["gross_margin"] for p in peers]
    return [m for m in margins if m is not None]


def _score_relative_margin(delta: Optional[float]) -> Optional[float]:
    """이 회사 마진 - peer 평균 마진 (%p). 업종 평균 대비 상대적 위치로 채점합니다."""
    return _score_from_bands(delta, [(10, 100), (0, 70), (-5, 50), (-999, 20)])


def _score_margin_std(std: Optional[float]) -> Optional[float]:
    if std is None:
        return None
    return _score_from_bands(-std, [(-3, 100), (-6, 70), (-10, 40), (-999, 10)])


def _score_cagr(cagr: Optional[float]) -> Optional[float]:
    return _score_from_bands(cagr, [(10, 100), (5, 70), (0, 40), (-999, 10)])


def _score_roe_avg(avg: Optional[float]) -> Optional[float]:
    return _score_from_bands(avg, [(15, 100), (10, 80), (5, 50), (-999, 20)])


def get_korea_moat_axis(ticker_code: str) -> dict:
    """② 비즈니스 모델의 확장성 및 진입 장벽(해자) 축을 계산합니다. (한국 주식)

    사용자 요청에 따라 "이익률 수준"은 대표 peer 대비 상대값(가중치 낮음)으로,
    변동성(표준편차)과 추세(매출 CAGR)에 가중치를 더 크게 뒀습니다.
    (예: 유통업처럼 원래 영업이익률이 낮은 업종이 절대 기준 때문에 불리해지는 것을 피하기 위함)
    """
    fin5, fin5_failed = _safe_call(financials.get_korea_financials, ticker_code, years=5)
    fin5 = fin5 or dict(financials.EMPTY_RESULT)
    fin5_failed = fin5_failed or fin5.get("fetch_failed", False)
    trend = fin5["trend"]

    op_margins = []
    if not trend.empty:
        # .get()을 씁니다 - 미국 은행주 등은 "영업이익" 계정 자체가 없을 수 있어서
        # row["영업이익"]로 바로 접근하면 KeyError가 납니다. (한국은 항상 값이 있어서
        # .get()을 써도 동작은 동일합니다)
        op_margins = [
            (row.get("영업이익") / row.get("매출액") * 100)
            if row.get("매출액") and row.get("영업이익") is not None
            else None
            for _, row in trend.iterrows()
        ]
    op_margin_latest = next((m for m in reversed(op_margins) if m is not None), None)
    op_margin_std = _std(op_margins)
    roe_avg = _avg(trend["ROE"].tolist()) if not trend.empty else None

    fin3, fin3_failed = _safe_call(financials.get_korea_financials, ticker_code, years=3)
    fin3 = fin3 or dict(financials.EMPTY_RESULT)
    fin3_failed = fin3_failed or fin3.get("fetch_failed", False)
    cagr = None
    if not fin3["income"].empty and len(fin3["income"]) >= 2:
        rev_first = fin3["income"].iloc[0]["매출액"]
        rev_last = fin3["income"].iloc[-1]["매출액"]
        cagr = _cagr(rev_first, rev_last, len(fin3["income"]) - 1)

    gross_margin, gross_margin_failed = _safe_call(get_gross_margin, ticker_code)

    sector = _get_kr_sector_for_ticker(ticker_code)
    op_margin_delta = gross_margin_delta = None
    peer_note = None
    if sector:
        peer_op_margins = _kr_peer_operating_margins(sector, ticker_code)
        if peer_op_margins and op_margin_latest is not None:
            op_margin_delta = op_margin_latest - _avg(peer_op_margins)
        peer_gross_margins = _kr_peer_gross_margins(sector, ticker_code)
        if peer_gross_margins and gross_margin is not None:
            gross_margin_delta = gross_margin - _avg(peer_gross_margins)
        peer_note = f"비교 대상(업종: {sector}): {', '.join(KR_SECTOR_PEERS[sector]['peers'])} (자기 자신 제외)"
    else:
        peer_note = "이 종목의 업종을 대표 peer 목록에서 찾지 못해 상대 비교를 생략했습니다."

    items = [
        {
            "label": "영업이익률 (peer 대비 상대수준)",
            "value_text": (
                "데이터 없음 (조회 실패)" if fin5_failed else (
                    f"{op_margin_latest:.1f}% (peer 대비 {op_margin_delta:+.1f}%p)"
                    if op_margin_latest is not None and op_margin_delta is not None
                    else (f"{op_margin_latest:.1f}% (peer 비교 불가)" if op_margin_latest is not None else "데이터 없음")
                )
            ),
            "score": _score_relative_margin(op_margin_delta),
            "detail": "최신 연도 영업이익률을 같은 업종 대표 종목 평균과 비교합니다. " + (peer_note or ""),
            "fetch_failed": fin5_failed,
        },
        {
            "label": "영업이익률 5년 변동성",
            "value_text": "데이터 없음 (조회 실패)" if fin5_failed else (f"표준편차 {op_margin_std:.2f}%p" if op_margin_std is not None else "데이터 없음"),
            "score": _score_margin_std(op_margin_std),
            "detail": "최근 5개년 영업이익률의 표준편차입니다. 낮을수록 가격결정력(해자)이 안정적이라고 봅니다.",
            "fetch_failed": fin5_failed,
        },
        {
            "label": "매출총이익률 (peer 대비 상대수준)",
            "value_text": (
                "데이터 없음 (조회 실패)" if gross_margin_failed else (
                    f"{gross_margin:.1f}% (peer 대비 {gross_margin_delta:+.1f}%p)"
                    if gross_margin is not None and gross_margin_delta is not None
                    else (f"{gross_margin:.1f}% (peer 비교 불가)" if gross_margin is not None else "데이터 없음")
                )
            ),
            "score": _score_relative_margin(gross_margin_delta),
            "detail": "매출총이익률(=매출총이익/매출액)을 같은 업종 대표 종목 평균과 비교합니다.",
            "fetch_failed": gross_margin_failed,
        },
        {
            "label": "매출 3년 CAGR",
            "value_text": "데이터 없음 (조회 실패)" if fin3_failed else (f"연평균 {cagr:.1f}%" if cagr is not None else "데이터 없음"),
            "score": _score_cagr(cagr),
            "detail": "최근 3개년 매출액의 연평균 성장률입니다.",
            "fetch_failed": fin3_failed,
        },
        {
            "label": "ROE 5년 평균",
            "value_text": "데이터 없음 (조회 실패)" if fin5_failed else (f"{roe_avg:.2f}%" if roe_avg is not None else "데이터 없음"),
            "score": _score_roe_avg(roe_avg),
            "detail": "최근 5개년 ROE의 평균입니다.",
            "fetch_failed": fin5_failed,
        },
    ]
    weights = [0.15, 0.30, 0.15, 0.20, 0.20]

    return _make_weighted_axis_result(items, weights, CITATIONS["해자"])


# ==============================================================
# ③ 수익성 및 밸류에이션 (PER 등)
# ==============================================================

def _score_per(per: Optional[float], is_loss: bool) -> Optional[float]:
    """PER 점수. 적자 기업은 PER 값과 무관하게 낮은 점수(0~20)로 처리합니다."""
    if is_loss:
        return 10.0  # "데이터 없음"으로 빼면 적자 기업이 오히려 유리해지는 문제를 막기 위함
    if per is None or per <= 0:
        return None
    return _score_from_bands(-per, [(-10, 100), (-15, 80), (-20, 60), (-30, 40), (-99999, 20)])


def _score_pbr(pbr: Optional[float]) -> Optional[float]:
    if pbr is None:
        return None
    if pbr <= 0:
        return 10.0  # 자본잠식 등으로 PBR이 음수인 경우 - 심각한 상황이므로 낮은 점수
    return _score_from_bands(-pbr, [(-1, 100), (-2, 80), (-3, 60), (-5, 40), (-99999, 20)])


def _score_relative_valuation(pct_cheaper: Optional[float]) -> Optional[float]:
    """peer 평균보다 몇 % 더 싼지(양수=더 쌈)를 점수로 바꿉니다."""
    return _score_from_bands(pct_cheaper, [(30, 100), (10, 80), (-10, 60), (-30, 40), (-99999, 20)])


def _format_peer_comparison(
    value: Optional[float], peer_median: Optional[float], peer_names: Optional[list] = None, unit: str = "배"
) -> str:
    """실제 값을 peer 중앙값과 나란히 보여주는 문구를 만듭니다.

    예: "6.93배 (peer[기아] 중앙값 6.24배보다 11% 높음 → 상대적 고평가)"
    peer 값은 평균이 아니라 중앙값(median)을 씁니다. peer가 2~3개뿐인 표본에서는
    극단값 하나가 평균을 크게 왜곡할 수 있어서(예: 삼성전자 반도체 peer가 SK하이닉스
    1개뿐이라 그 값이 그대로 "평균"이 되어버리는 경우), 중앙값이 더 안정적입니다.

    peer_names를 주면 "어떤 종목과 비교했는지"를 펼쳐보지 않아도 바로 보이도록
    괄호 안에 종목명을 함께 적습니다. (peer가 4개 넘으면 개수만 표시)

    배수 차이가 크면(200%, 즉 3배 넘게 차이나면) %보다 "peer의 N배"가 더 이해하기
    쉬워서 표현 방식을 바꿉니다. (예: "peer 중앙값 2.73배의 16.7배 수준")
    """
    if value is None or not peer_median:
        if peer_names:
            names = "·".join(peer_names) if len(peer_names) <= 3 else f"{len(peer_names)}종목"
            return f"peer 비교 불가 (비교 대상: {names})"
        return "peer 비교 불가"

    if peer_names and len(peer_names) <= 3:
        peer_label = f"peer[{'·'.join(peer_names)}]"
    elif peer_names:
        peer_label = f"peer {len(peer_names)}종목"
    else:
        peer_label = "peer"

    diff_pct = abs(value - peer_median) / peer_median * 100

    if value > peer_median:
        direction, judgement = "높음", "상대적 고평가"
    elif value < peer_median:
        direction, judgement = "낮음", "상대적 저평가"
    else:
        direction, judgement = "동일", "peer와 비슷한 수준"

    if diff_pct > 200:
        ratio = value / peer_median
        return f"{value:.2f}{unit} ({peer_label} 중앙값 {peer_median:.2f}{unit}의 {ratio:.1f}배 수준 → {judgement})"

    return f"{value:.2f}{unit} ({peer_label} 중앙값 {peer_median:.2f}{unit}보다 {diff_pct:.0f}% {direction} → {judgement})"


def _format_peer_breakdown(peer_details: list) -> str:
    """peer 각 종목의 PER/PBR 개별 값을 나열합니다. (평균/중앙값이 어떻게 나왔는지 검증할 수 있도록)"""
    parts = []
    for d in peer_details:
        per_text = f"PER {d['per']:.2f}배" if d.get("per") is not None else "PER 데이터없음"
        pbr_text = f"PBR {d['pbr']:.2f}배" if d.get("pbr") is not None else "PBR 데이터없음"
        label = d["name"] if d["name"] == d["code"] else f"{d['name']}({d['code']})"
        parts.append(f"{label} {per_text}·{pbr_text}")
    return ", ".join(parts) if parts else "peer 없음"


def _band_position_percentile(current: float, historical: list) -> tuple:
    """current 값이 historical(과거 관측치들)의 [최소~최대] 구간에서 어디쯤 있는지를 0~100 사이 연속값으로 계산합니다.

    처음에는 "과거 값들 중 current보다 큰 값의 비율"(카운트 기반)로 계산했는데,
    관측치가 3개뿐이다 보니 범위를 살짝만 벗어나도 곧바로 0%나 100%로 튀는 문제가 있었습니다.
    대신 [최소, 최대] 구간 안에서 현재 값의 상대적 위치를 매끄럽게(연속적으로) 계산합니다.
    범위를 벗어나면(과거 어떤 값보다도 높거나 낮으면) 0%/100%로 고정하되, 그 사실을 out_of_range로 알려줍니다.

    반환값: (백분위, 범위를 벗어났는지 여부)
    """
    lo, hi = min(historical), max(historical)
    if hi == lo:
        return 50.0, False
    raw = (current - lo) / (hi - lo) * 100
    # 백분위 정의상 "낮을수록 저평가"이므로, 위치를 뒤집어서 반환합니다.
    # (current가 lo에 가까우면 "쌈" -> 백분위 낮게, current가 hi에 가까우면 "비쌈" -> 백분위 높게)
    out_of_range = raw < 0 or raw > 100
    return max(0.0, min(100.0, raw)), out_of_range


def _diverging_valuation_note(per_delta_pct: Optional[float], pbr_delta_pct: Optional[float]) -> Optional[str]:
    """PER 기준과 PBR 기준의 '싸다/비싸다' 방향이 서로 다를 때 이유를 설명합니다.

    PBR = PER × ROE 라는 관계식이 성립하기 때문에, peer보다 ROE가 낮은 회사는
    'PER은 peer보다 높은데(고평가) PBR은 peer보다 낮은(저렴)' 것처럼 보일 수 있습니다.
    계산 오류가 아니라 실제로 있을 수 있는 정상적인 상황이라, 헷갈리지 않도록 안내합니다.
    """
    if per_delta_pct is None or pbr_delta_pct is None:
        return None
    if (per_delta_pct >= 0) == (pbr_delta_pct >= 0):
        return None
    return (
        "PER과 PBR의 '저렴/고평가' 방향이 서로 다릅니다 — 계산 오류가 아니라, "
        "PBR은 'PER × ROE'와 같아서 peer보다 ROE가 낮으면 실제로 이렇게 갈릴 수 있습니다. "
        "두 지표가 각각 무엇을 보는지(수익 대비 가격 vs 자산 대비 가격) 구분해서 판단해주세요."
    )


def _score_per_band_percentile(percentile: Optional[float]) -> Optional[float]:
    """3년 자기 PER 밴드 내 백분위. 낮을수록(과거 대비 저평가) 고득점."""
    if percentile is None:
        return None
    return _score_from_bands(-percentile, [(-30, 100), (-60, 70), (-85, 40), (-999, 10)])


@st.cache_data(ttl=3600)
def get_korea_per_band_percentile(ticker_code: str) -> Optional[dict]:
    """최근 3개년 '연말 시점 PER' 대비, 현재 PER이 어느 위치(백분위)에 있는지 계산합니다.

    엄밀한 일별 PER 밴드 대신, 계산량을 줄이기 위해 "각 연도 말 종가 3개 vs 현재가"로
    단순화했습니다. (발행주식수는 최신 값을 그대로 사용하는 근사치입니다)

    "현재 PER"의 순이익은 최근 4개 분기 합계(TTM, 실적 발표 시점 최신)를 우선 쓰고,
    분기 데이터가 부족하면 최신 연간 실적으로 대신합니다. 처음에는 "오늘 주가 ÷ 작년
    연간 순이익"으로 계산했는데, 마지막 사업보고서(작년 말)로부터 몇 달이 지나 주가만
    올라도 "현재가 가장 비쌈(백분위 100%)"으로 나오는 착시가 있어서 고쳤습니다.
    (현대차 실측: TTM을 안 쓰면 PER 6.93배가 오히려 "3년 중 가장 비쌈"으로 나왔었습니다)
    """
    fin3 = financials.get_korea_financials(ticker_code, years=3)
    if fin3["income"].empty or len(fin3["income"]) < 2:
        return None

    listing = get_krx_listing()
    if listing.empty:  # KRX 목록 조회 자체가 실패한 경우 (빈 표에는 컬럼도 없어서 바로 걸러줍니다)
        return None
    matched = listing.loc[listing["Code"] == ticker_code]
    if matched.empty or not matched.iloc[0].get("Stocks"):
        return None
    shares = matched.iloc[0]["Stocks"]
    current_price = matched.iloc[0].get("Close")

    end = datetime.today()
    start = end - timedelta(days=365 * 4)
    try:
        price_df = fdr.DataReader(ticker_code, start, end)
    except Exception:
        return None
    if price_df.empty:
        return None

    historical_pers = []
    for _, row in fin3["income"].iterrows():
        year = int(row["연도"])
        net_income = row["당기순이익"]
        if not net_income or net_income <= 0 or not shares:
            continue
        eps = net_income / shares
        # 그 해 마지막 거래일 종가를 찾습니다.
        year_prices = price_df.loc[f"{year}-01-01":f"{year}-12-31"]
        if year_prices.empty:
            continue
        year_end_price = year_prices["Close"].iloc[-1]
        historical_pers.append(year_end_price / eps)

    if not historical_pers or not current_price or not shares:
        return None

    # "현재" 순이익은 최근 4개 분기 합계(TTM)를 우선 사용합니다. (위 설명 참고)
    ttm_net_income, eps_basis = None, None
    quarterly = financials.get_korea_quarterly_trend(ticker_code)
    if not quarterly.empty:
        recent4 = quarterly["당기순이익"].dropna().tail(4)
        if len(recent4) == 4:
            ttm_net_income = recent4.sum()
            eps_basis = "최근 4개 분기 합산(TTM)"

    if ttm_net_income and ttm_net_income > 0:
        current_net_income = ttm_net_income
    else:
        current_net_income = fin3["income"].iloc[-1]["당기순이익"]
        eps_basis = f"최신 연간({int(fin3['income'].iloc[-1]['연도'])}년) 실적"

    if not current_net_income or current_net_income <= 0:
        return None
    current_eps = current_net_income / shares
    current_per = current_price / current_eps

    percentile, out_of_range = _band_position_percentile(current_per, historical_pers)

    return {
        "current_per": current_per,
        "historical_pers": historical_pers,
        "percentile": percentile,
        "out_of_range": out_of_range,
        "eps_basis": eps_basis,
    }


def get_korea_valuation_axis(ticker_code: str) -> dict:
    """③ 수익성 및 밸류에이션 축을 계산합니다. (한국 주식)"""
    fin, fin_failed = _safe_call(financials.get_korea_financials, ticker_code)
    fin = fin or dict(financials.EMPTY_RESULT)
    fin_failed = fin_failed or fin.get("fetch_failed", False)
    per, pbr = fin["per"], fin["pbr"]
    net_income_latest = fin["income"].iloc[-1]["당기순이익"] if not fin["income"].empty else None
    is_loss = net_income_latest is not None and net_income_latest <= 0

    sector = _get_kr_sector_for_ticker(ticker_code)
    peer_median_per = peer_median_pbr = None
    per_delta_pct = pbr_delta_pct = None
    peer_note = None
    peer_names = []
    if sector:
        peers = [p for p in KR_SECTOR_PEERS[sector]["peers"] if p != ticker_code]
        peer_pers, peer_pbrs, peer_details = [], [], []
        for peer in peers:
            peer_metrics = _get_peer_metrics("KR", peer)  # peer_cache.json 우선, 없으면 실시간 조회
            peer_details.append(
                {"code": peer, "name": get_korea_stock_name(peer), "per": peer_metrics["per"], "pbr": peer_metrics["pbr"]}
            )
            if peer_metrics["per"] and peer_metrics["per"] > 0:
                peer_pers.append(peer_metrics["per"])
            if peer_metrics["pbr"] and peer_metrics["pbr"] > 0:
                peer_pbrs.append(peer_metrics["pbr"])
        # peer가 2~3개뿐인 표본에서는 평균 대신 중앙값을 써야 극단값 하나에 덜 흔들립니다.
        if peer_pers and per and per > 0:
            peer_median_per = _median(peer_pers)
            per_delta_pct = (peer_median_per - per) / peer_median_per * 100
        if peer_pbrs and pbr and pbr > 0:
            peer_median_pbr = _median(peer_pbrs)
            pbr_delta_pct = (peer_median_pbr - pbr) / peer_median_pbr * 100
        peer_names = [d["name"] for d in peer_details]
        peer_note = f"비교 대상(업종: {sector}) 개별 값 — {_format_peer_breakdown(peer_details)}"
    else:
        peer_note = "이 종목의 업종을 대표 peer 목록에서 찾지 못해 상대 비교를 생략했습니다."

    diverging_note = _diverging_valuation_note(per_delta_pct, pbr_delta_pct)

    band, band_failed = _safe_call(get_korea_per_band_percentile, ticker_code)
    percentile = band["percentile"] if band else None
    out_of_range = band.get("out_of_range", False) if band else False

    items = [
        {
            "label": "PER",
            "value_text": "데이터 없음 (조회 실패)" if fin_failed else ("적자 (PER 의미 없음)" if is_loss else (f"{per:.2f}배" if per is not None else "데이터 없음")),
            "score": _score_per(per, is_loss),
            "detail": "주가를 주당순이익으로 나눈 값입니다. 적자 기업은 PER이 의미가 없어 낮은 점수로 처리합니다.",
            "fetch_failed": fin_failed,
        },
        {
            "label": "PBR",
            "value_text": "데이터 없음 (조회 실패)" if fin_failed else (f"{pbr:.2f}배" if pbr is not None else "데이터 없음"),
            "score": _score_pbr(pbr),
            "detail": "주가를 주당순자산으로 나눈 값입니다.",
            "fetch_failed": fin_failed,
        },
        {
            "label": "PER peer 대비 (참고용)",
            "value_text": "PER " + _format_peer_comparison(per, peer_median_per, peer_names) + (" ⚠️" if diverging_note else ""),
            "score": _score_relative_valuation(per_delta_pct),
            "detail": (
                "같은 업종 대표 종목 중앙값 PER과 비교합니다. " + (peer_note or "")
                + (f" {diverging_note}" if diverging_note else "")
                + " ⚠️ peer를 누구로 고르느냐에 따라 결론이 바뀔 수 있어서(peer가 2~3개뿐이라 표본이 작음) "
                "참고용으로만 보고, 아래 '3년 자기 PER 밴드'에 더 큰 비중을 뒀습니다."
            ),
            "fetch_failed": fin_failed,
        },
        {
            "label": "PBR peer 대비 (참고용)",
            "value_text": "PBR " + _format_peer_comparison(pbr, peer_median_pbr, peer_names) + (" ⚠️" if diverging_note else ""),
            "score": _score_relative_valuation(pbr_delta_pct),
            "detail": (
                "같은 업종 대표 종목 중앙값 PBR과 비교합니다." + (f" {diverging_note}" if diverging_note else "")
                + " ⚠️ peer를 누구로 고르느냐에 따라 결론이 바뀔 수 있어서 참고용으로만 봅니다."
            ),
            "fetch_failed": fin_failed,
        },
        {
            "label": "3년 자기 PER 밴드 백분위",
            "value_text": (
                "데이터 없음 (조회 실패)" if band_failed else (
                    f"백분위 {percentile:.0f}% (낮을수록 저평가)" + (" — 3년 밴드 범위를 벗어남" if out_of_range else "")
                    if percentile is not None else "데이터 없음"
                )
            ),
            "score": _score_per_band_percentile(percentile),
            "detail": (
                "최근 3개 연말 시점 PER과 비교해, 현재 PER이 상대적으로 높은지 낮은지 봅니다. "
                "peer 선택에 좌우되지 않고 이 종목 자신의 과거와만 비교하기 때문에, 이 축에서 가장 큰 비중(50%)을 둡니다. "
                "(일별 정밀 계산이 아닌 연말 시점 근사치이며, 현재 순이익 기준: " + (band.get("eps_basis", "-") if band else "-") + ") "
                + ("⚠️ 현재 PER이 최근 3년치보다 높거나 낮아 범위를 벗어났습니다 - 최근 추세(실적 증감 등)가 과거 3년과 달라졌다는 뜻일 수 있습니다." if out_of_range else "")
            ),
            "fetch_failed": band_failed,
        },
    ]

    note = None
    if sector and KR_SECTOR_PEERS[sector]["cyclical"]:
        note = f"⚠️ 이 종목은 시클리컬(경기 순환) 업종({sector})으로 분류됩니다. " + CITATIONS["밸류"]

    # 가중치: PER/PBR 절대 수준 30%(15%+15%), peer 비교 20%(10%+10%, 참고용),
    # 3년 자기 PER 밴드 백분위 50% (peer 선택에 흔들리지 않는 지표라 가장 신뢰도가 높다고 판단)
    weights = [0.15, 0.15, 0.10, 0.10, 0.50]
    return _make_weighted_axis_result(items, weights, CITATIONS["밸류"], note=note)


# ==============================================================
# ④ 주주 환원 및 배당 성향
# ==============================================================

@st.cache_data(ttl=3600)
def get_dividend_detail(ticker_code: str, year: int) -> dict:
    """특정 연도의 배당수익률/배당성향/주당배당금을 가져옵니다."""
    corp_code = _get_corp_code(ticker_code)
    if corp_code is None:
        return {}
    df = _dart_get("alotMatter.json", corp_code, year)
    if df.empty or "se" not in df.columns:
        return {}

    def first_value(label):
        row = df[df["se"] == label]
        return _to_number(row.iloc[0]["thstrm"]) if not row.empty else None

    return {
        "dividend_yield": first_value("현금배당수익률(%)"),
        "payout_ratio": first_value("(연결)현금배당성향(%)") or first_value("현금배당성향(%)"),
        "dps": first_value("주당 현금배당금(원)"),
    }


@st.cache_data(ttl=3600)
def get_buyback_years(ticker_code: str, years: int = 5) -> Optional[int]:
    """최근 N년 중 자기주식을 실제로 취득한 연도 수를 셉니다."""
    if not DART_API_KEY:
        return None
    corp_code = _get_corp_code(ticker_code)
    if corp_code is None:
        return None

    current_year = datetime.today().year
    count = 0
    checked_any = False
    for year in range(current_year - 1, current_year - 1 - years, -1):
        try:
            df = _dart_get("tesstkAcqsDspsSttus.json", corp_code, year)
        except DartUnavailableError:
            continue  # 이 연도 조회만 실패한 것으로 보고 다른 연도는 계속 확인합니다.
        if df.empty or "change_qy_acqs" not in df.columns:
            continue
        checked_any = True
        acquired = df["change_qy_acqs"].apply(_to_number)
        if any((v or 0) > 0 for v in acquired):
            count += 1

    return count if checked_any else None


def _score_dividend_yield(yield_pct: Optional[float]) -> Optional[float]:
    if yield_pct is None:
        return None
    return _score_from_bands(yield_pct, [(4, 100), (2, 80), (1, 60), (0, 40), (-999, 20)])


def _score_payout_ratio(ratio: Optional[float]) -> Optional[float]:
    if ratio is None:
        return None
    if ratio > 80:
        return 60.0  # 과도한 배당성향은 지속가능성 우려로 소폭 감점
    return _score_from_bands(ratio, [(50, 100), (20, 90), (0, 50)])


def _score_dividend_trend(dps_values: list) -> Optional[float]:
    valid = [(y, v) for y, v in dps_values if v is not None]
    if len(valid) < 2:
        return None
    diffs = [valid[i + 1][1] - valid[i][1] for i in range(len(valid) - 1)]
    increases = sum(1 for d in diffs if d > 0)
    decreases = sum(1 for d in diffs if d < 0)
    if increases >= 3 and decreases == 0:
        return 100.0
    if increases > decreases:
        return 70.0
    if increases == decreases:
        return 50.0
    return 20.0


def _score_buyback_years(count: Optional[int]) -> Optional[float]:
    return _score_from_bands(count, [(3, 100), (1, 60), (0, 30)])


def get_korea_shareholder_return_axis(ticker_code: str) -> dict:
    """④ 주주 환원 및 배당 성향 축을 계산합니다. (한국 주식)"""
    current_year = datetime.today().year
    years_to_check = [current_year - 1, current_year - 2, current_year - 3, current_year - 4, current_year - 5]

    yearly = {}
    dividend_detail_failed = False
    for y in years_to_check:
        result, failed = _safe_call(get_dividend_detail, ticker_code, y)
        yearly[y] = result or {}
        dividend_detail_failed = dividend_detail_failed or failed
    latest = yearly[years_to_check[0]]

    dps_series = [(y, yearly[y].get("dps")) for y in sorted(years_to_check)]
    buyback_count, buyback_failed = _safe_call(get_buyback_years, ticker_code)

    items = [
        {
            "label": "배당수익률",
            "value_text": "데이터 없음 (조회 실패)" if dividend_detail_failed else (f"{latest.get('dividend_yield'):.2f}%" if latest.get("dividend_yield") is not None else "데이터 없음"),
            "score": _score_dividend_yield(latest.get("dividend_yield")),
            "detail": "최근 사업연도 기준 현금배당수익률입니다.",
            "fetch_failed": dividend_detail_failed,
        },
        {
            "label": "배당성향",
            "value_text": "데이터 없음 (조회 실패)" if dividend_detail_failed else (f"{latest.get('payout_ratio'):.1f}%" if latest.get("payout_ratio") is not None else "데이터 없음"),
            "score": _score_payout_ratio(latest.get("payout_ratio")),
            "detail": "당기순이익 중 배당으로 지급한 비율입니다. 너무 낮으면 주주환원이 부족하고, 너무 높으면(80% 초과) 지속가능성이 우려됩니다.",
            "fetch_failed": dividend_detail_failed,
        },
        {
            "label": "5년 배당 증감 추이",
            "value_text": (
                "데이터 없음 (조회 실패)" if dividend_detail_failed and not any(v is not None for _, v in dps_series) else (
                    ", ".join(f"{y}:{v:,.0f}원" for y, v in dps_series if v is not None)
                    if any(v is not None for _, v in dps_series) else "데이터 없음"
                )
            ),
            "score": _score_dividend_trend(dps_series),
            "detail": "최근 5개년 주당 현금배당금의 증감 추이입니다.",
            "fetch_failed": dividend_detail_failed and not any(v is not None for _, v in dps_series),
        },
        {
            "label": "자기주식 취득 이력",
            "value_text": "데이터 없음 (조회 실패)" if buyback_failed else (f"최근 5년 중 {buyback_count}개 연도에서 취득" if buyback_count is not None else "데이터 없음"),
            "score": _score_buyback_years(buyback_count),
            "detail": "자기주식 취득 및 처분 현황에서, 실제로 순취득이 있었던 연도 수를 셉니다.",
            "fetch_failed": buyback_failed,
        },
    ]

    return _make_axis_result(items, CITATIONS["환원"])


# ==============================================================
# ⑤ 미래 비전 및 창의적 적응력
# ==============================================================

# 신규사업/전환 언급 추출에 쓸 키워드. "전환" 처럼 흔한 단어는 전환사채 등과 섞여 오탐이
# 많아서 빼고, 사업 방향 전환을 가리킬 가능성이 높은 복합어 위주로 골랐습니다.
_NEW_BUSINESS_KEYWORDS = ["신규사업", "신규 사업", "사업다각화", "사업 다각화", "미래 성장동력", "신성장동력", "사업구조 개편", "사업재편"]


@st.cache_data(ttl=60 * 60 * 24)
def _get_business_report_text(ticker_code: str) -> Optional[str]:
    """가장 최근 사업보고서 원문(전체 텍스트, 태그 미제거)을 가져옵니다.

    document.xml은 최대 수 MB짜리 zip이라 원래도 넉넉한 타임아웃(30초)을 썼는데,
    이제 financials._dart_request로 재시도(2회)까지 함께 적용합니다. 접속이 완전히
    실패하면(DartUnavailableError) 여기서 잡지 않고 그대로 던져서, 실패를 캐시하지 않고
    호출한 쪽(get_rnd_detail, get_new_business_mentions)에서 항목 단위로 처리하게 합니다.
    """
    if not DART_API_KEY:
        return None
    corp_code = _get_corp_code(ticker_code)
    if corp_code is None:
        return None

    end = datetime.today()
    start = end - timedelta(days=500)  # 사업보고서가 다음해 3~4월에 공시되는 것까지 여유있게 포함
    res = financials._dart_request(
        f"{DART_BASE_URL}/list.json",
        {
            "crtfc_key": DART_API_KEY, "corp_code": corp_code,
            "bgn_de": start.strftime("%Y%m%d"), "end_de": end.strftime("%Y%m%d"),
            "pblntf_ty": "A", "page_count": 20,
        },
    )
    data = res.json()
    rcept_no = None
    for item in data.get("list", []):
        if "사업보고서" in item.get("report_nm", ""):
            rcept_no = item.get("rcept_no")
            break
    if not rcept_no:
        return None

    doc_res = financials._dart_request(
        f"{DART_BASE_URL}/document.xml",
        {"crtfc_key": DART_API_KEY, "rcept_no": rcept_no},
    )
    try:
        with zipfile.ZipFile(io.BytesIO(doc_res.content)) as zf:
            names = zf.namelist()
            biggest = max(names, key=lambda n: len(zf.read(n)))
            content = zf.read(biggest)
        return content.decode("utf-8")
    except Exception:
        return None


@st.cache_data(ttl=60 * 60 * 24)
def get_rnd_detail(ticker_code: str) -> dict:
    """연구개발비 관련 정보를 사업보고서 원문에서 최대한 뽑아옵니다.

    삼성전자·현대차·카카오 등으로 실측 검증한 결과, 우리가 계산한 비율이 사업보고서에
    원문 그대로 적힌 비율과 0.1%p 이내로 일치했습니다. 그 "원문 명시 비율"도 함께 뽑아서
    검증용으로 반환합니다 (report_ratio).

    전년도 R&D 금액(prior_amount)은 "연결/별도"를 나란히 적은 다중 열 표(예: 카카오)에서는
    바로 다음 숫자가 전년도가 아니라 다른 기준(별도)의 같은 연도 값일 수 있어서, 그런 표가
    감지되면(주변에 "별도"라는 글자가 있으면) 안전하게 None으로 둡니다.

    반환:
      {
        "ratio": float|None,          # 우리가 계산한 최신 연도 R&D/매출 비율(%)
        "report_ratio": float|None,   # 사업보고서 원문에 직접 적힌 비율(%) - 검증용
        "amount": float|None,         # 최신 연도 R&D 금액(백만원)
        "prior_amount": float|None,   # 전년도 R&D 금액(백만원). 표 형식상 못 뽑으면 None
      }
    """
    empty = {"ratio": None, "report_ratio": None, "amount": None, "prior_amount": None}

    text = _get_business_report_text(ticker_code)
    if not text:
        return empty

    clean = re.sub(r"<[^>]+>", " ", text)
    clean = re.sub(r"\s+", " ", clean)

    m = re.search(r"연구개발비용\s*(?:총계|계)\s*([\d,]+)", clean)
    if not m:
        return empty
    amount = int(m.group(1).replace(",", ""))  # 단위: 백만원

    # 전년도 금액 추정: 라벨 앞쪽 200자 안에 "별도"가 있으면 연결/별도가 나란히 있는
    # 다중 열 표라는 뜻이라, 바로 다음 숫자를 전년도로 오인하지 않도록 건너뜁니다.
    prior_amount = None
    preceding_text = clean[max(0, m.start() - 200): m.start()]
    if "별도" not in preceding_text:
        after = clean[m.end(): m.end() + 30]
        m2 = re.match(r"\s*([\d,]+)", after)
        if m2:
            candidate = int(m2.group(1).replace(",", ""))
            # 갑자기 5배 넘게 차이나면 R&D와 무관한 다른 숫자를 잘못 집었을 가능성이 커서 버립니다.
            if amount and candidate and 0.2 <= candidate / amount <= 5:
                prior_amount = candidate

    # 검증용: 사업보고서에 직접 적힌 "연구개발비/매출액 비율" 문구 뒤의 첫 퍼센트 값.
    # "매출액 비율"이라는 문구는 R&D 표 말고도(예: 제품별 매출 비중 표) 문서 다른 곳에도
    # 나올 수 있어서, 문서 전체가 아니라 방금 찾은 R&D 금액 위치 바로 뒤(400자 이내)에서만
    # 찾습니다. (KT&G에서 담배 브랜드별 매출 비중표의 "66.4%"를 잘못 집었던 문제를 방지)
    report_ratio = None
    nearby_text = clean[m.end(): m.end() + 400]
    ratio_match = re.search(r"매출액\s*비율[^%]{0,120}?(\d+\.\d+)\s*%", nearby_text)
    if ratio_match:
        report_ratio = float(ratio_match.group(1))

    fin = financials.get_korea_financials(ticker_code, years=1)
    ratio = None
    if not fin["income"].empty:
        revenue = fin["income"].iloc[-1]["매출액"]
        if revenue:
            ratio = amount / (revenue / 1_000_000) * 100

    return {"ratio": ratio, "report_ratio": report_ratio, "amount": amount, "prior_amount": prior_amount}


@st.cache_data(ttl=3600)
def get_capex_trend(ticker_code: str, years: int = 3) -> pd.DataFrame:
    """최근 N개년 CAPEX(유형자산+무형자산 취득)/매출액 비율(%)을 계산합니다.

    매출액은 fnlttSinglAcntAll(전체 재무제표) 응답에서 직접 뽑지 않고, 이미 안정적으로
    검증된 financials.py의 매출액을 재사용합니다. 실측해보니 삼성전자 2023년치처럼
    fnlttSinglAcntAll에 '매출액' 라인 자체가 빠져있는 연도가 있어서(매출총이익/매출원가는
    있는데 매출액만 없음), 그 API의 매출액에 의존하면 3개년을 다 못 채우는 문제가 있었습니다.
    """
    corp_code = _get_corp_code(ticker_code)
    if corp_code is None or not DART_API_KEY:
        return pd.DataFrame()

    fin = financials.get_korea_financials(ticker_code, years=years + 2)
    if fin["income"].empty:
        return pd.DataFrame()
    revenue_by_year = dict(zip(fin["income"]["연도"], fin["income"]["매출액"]))

    current_year = datetime.today().year
    rows = []
    for year in range(current_year - 1, current_year - 1 - years - 2, -1):
        revenue = revenue_by_year.get(year)
        if not revenue:
            continue

        try:
            res = financials._dart_request(
                f"{DART_BASE_URL}/fnlttSinglAcntAll.json",
                {
                    "crtfc_key": DART_API_KEY, "corp_code": corp_code,
                    "bsns_year": str(year), "reprt_code": "11011", "fs_div": "CFS",
                },
            )
        except DartUnavailableError:
            continue  # 이 연도 조회만 실패한 것으로 보고 다른 연도를 시도합니다.
        data = res.json()
        if data.get("status") != "000":
            continue
        df = pd.DataFrame(data["list"])

        def get_amt(name):
            match = df[df["account_nm"] == name]
            return _to_number(match.iloc[0]["thstrm_amount"]) if not match.empty else None

        tangible = get_amt("유형자산의 취득")
        intangible = get_amt("무형자산의 취득")
        if tangible is not None or intangible is not None:
            capex = (tangible or 0) + (intangible or 0)
            rows.append({"연도": year, "CAPEX비율": capex / revenue * 100})

        if len(rows) == years:
            break

    return pd.DataFrame(rows).sort_values("연도").reset_index(drop=True) if rows else pd.DataFrame()


@st.cache_data(ttl=60 * 60 * 24)
def get_new_business_mentions(ticker_code: str, max_snippets: int = 3) -> list:
    """사업보고서 원문에서 신규사업/전환 관련 키워드 주변 문장을 인용용으로 추출합니다.

    점수에는 반영하지 않고, 사용자가 직접 읽고 판단할 수 있도록 원문 그대로 보여주기 위한 것입니다.
    """
    text = _get_business_report_text(ticker_code)
    if not text:
        return []

    clean = re.sub(r"<[^>]+>", " ", text)
    clean = re.sub(r"\s+", " ", clean)

    snippets = []
    seen = set()
    for keyword in _NEW_BUSINESS_KEYWORDS:
        for m in re.finditer(re.escape(keyword), clean):
            start = max(0, m.start() - 60)
            end = min(len(clean), m.end() + 100)
            snippet = clean[start:end].strip()
            if snippet not in seen:
                seen.add(snippet)
                snippets.append(snippet)
            if len(snippets) >= max_snippets:
                break
        if len(snippets) >= max_snippets:
            break
    return snippets


def _score_rnd_ratio(ratio: Optional[float]) -> Optional[float]:
    """R&D/매출 비율 채점 밴드.

    기존 밴드([2%->60점, ...])는 실측해보니 너무 관대했습니다 (현대차 2.97%가 60점,
    사실상 대부분 기업이 60~85점대에 몰림). 삼성전자(11.3%)·현대차(2.97%)·
    카카오(16.0%)·KT&G(1.15%) 4개 실측치를 기준으로 값을 더 넓게 벌렸습니다.
    """
    return _score_from_bands(ratio, [(15, 100), (8, 80), (4, 55), (1.5, 30), (-999, 10)])


def _score_capex_level(level: Optional[float]) -> Optional[float]:
    """CAPEX/매출 비율의 '수준'(최신 연도 절대값) 채점 밴드.

    삼성전자(15.6%)·현대차(5.9%)·포스코(9.0%) 실측치를 기준으로 잡았습니다.
    """
    return _score_from_bands(level, [(15, 100), (10, 80), (6, 55), (3, 30), (-999, 10)])


def _score_capex_trend_3y(values: list) -> Optional[float]:
    """CAPEX 비율의 3년 흐름을 선형회귀 기울기로 채점합니다.

    이전에는 '첫 해 대비 마지막 해' 두 점만 비교했는데, 반도체처럼 투자 사이클이 있는
    업종은 한 해만 봐도 증가/감소가 크게 흔들릴 수 있어서(예: 삼성전자 CAPEX가
    2023→2025 사이 23.4%→17.9%→15.6%로 매년 줄어드는 것처럼, 혹은 반대로 특정
    연도만 튀는 경우), 3개 연도 전체의 흐름(기울기)을 봅니다. 기울기를 평균 수준으로
    나눠서(%) 회사 규모와 무관하게 비교할 수 있게 정규화했습니다.
    """
    valid = [v for v in values if v is not None]
    if len(valid) < 2:
        return None
    x = list(range(len(valid)))
    slope = float(np.polyfit(x, valid, 1)[0])
    avg_level = float(np.mean(valid))
    if not avg_level:
        return None
    slope_pct = slope / avg_level * 100  # 연평균 몇 % 씩 늘거나 줄어드는지
    return _score_from_bands(slope_pct, [(10, 100), (2, 80), (-5, 55), (-15, 30), (-999, 10)])


def _score_capex(capex_df: pd.DataFrame) -> tuple:
    """CAPEX 항목 점수 = 수준(60%) + 3년 흐름(40%).

    이전에는 추세(증감 방향)만 봐서, 삼성전자(CAPEX 15.6%, 압도적으로 많이 투자)가
    현대차(5.9%)보다 낮은 점수를 받는 문제가 있었습니다. '얼마나 투자하는지(수준)'에
    더 큰 비중을 두고, '늘어나는 중인지(흐름)'는 보조 지표로만 반영합니다.
    반환값: (수준 점수, 흐름 점수, 합산 점수)
    """
    if capex_df.empty:
        return None, None, None
    values = capex_df["CAPEX비율"].tolist()
    level_score = _score_capex_level(values[-1])
    trend_score = _score_capex_trend_3y(values)
    if level_score is None:
        combined = None
    elif trend_score is None:
        combined = level_score  # 흐름 데이터가 부족하면 수준만으로 채점
    else:
        combined = level_score * 0.6 + trend_score * 0.4
    return level_score, trend_score, combined


def _guard_rnd_ratio_score(rnd: dict, revenue_prior: Optional[float], revenue_latest: Optional[float]) -> tuple:
    """R&D 비율은 분모가 매출액이라, 매출이 줄기만 해도 비율이 저절로 올라가는 착시가 있습니다.

    '매출 감소 + R&D 금액은 그대로거나 줄어듦'이 동시에 확인되면, 비율이 높아 보여도
    실제 R&D 투자가 늘어난 게 아니므로 가점하지 않고 점수를 낮게 캡(cap)합니다.
    반환값: (점수, 경고 문구 또는 None)
    """
    ratio = rnd.get("ratio")
    if ratio is None:
        return None, None

    base_score = _score_rnd_ratio(ratio)

    amount, prior_amount = rnd.get("amount"), rnd.get("prior_amount")
    revenue_declined = revenue_prior is not None and revenue_latest is not None and revenue_latest < revenue_prior
    rnd_not_increased = amount is not None and prior_amount is not None and amount <= prior_amount

    if revenue_declined and rnd_not_increased:
        capped_score = min(base_score, 40.0) if base_score is not None else None
        warning = "매출이 줄어든 상태에서 비율만 높아진 것으로 보입니다 (R&D 금액 자체는 늘지 않음) — 가점하지 않았습니다."
        return capped_score, warning

    return base_score, None


def _score_rnd_amount_change(amount: Optional[float], prior_amount: Optional[float]) -> Optional[float]:
    return _score_trend_change([prior_amount, amount]) if prior_amount is not None and amount is not None else None


def get_korea_future_vision_axis(ticker_code: str) -> dict:
    """⑤ 미래 비전 및 창의적 적응력 축을 계산합니다. (한국 주식)

    R&D/매출 비율(최신 연도), R&D 금액의 절대 증감, CAPEX/매출 추이를 점수에 반영합니다.
    신규사업 언급은 사업보고서 원문에서 뽑은 문장을 인용으로만 보여주고 점수에는 넣지 않습니다.
    """
    rnd, rnd_failed = _safe_call(get_rnd_detail, ticker_code)
    rnd = rnd or {"ratio": None, "report_ratio": None, "amount": None, "prior_amount": None}
    capex_df, capex_failed = _safe_call(get_capex_trend, ticker_code)
    capex_df = capex_df if capex_df is not None else pd.DataFrame()
    mentions, mentions_failed = _safe_call(get_new_business_mentions, ticker_code)
    mentions = mentions or []

    # R&D 비율 착시 방지를 위해 최근 2개년 매출액을 함께 봅니다.
    fin2, fin2_failed = _safe_call(financials.get_korea_financials, ticker_code, years=2)
    fin2 = fin2 or dict(financials.EMPTY_RESULT)
    revenue_prior = revenue_latest = None
    if len(fin2["income"]) >= 2:
        revenue_prior = fin2["income"].iloc[-2]["매출액"]
        revenue_latest = fin2["income"].iloc[-1]["매출액"]

    rnd_score, rnd_warning = _guard_rnd_ratio_score(rnd, revenue_prior, revenue_latest)

    # 우리 계산과 원문 명시 비율이 크게(1%p 넘게) 다르면, 그 회사가 R&D 비율을 계산할 때
    # 우리와 다른 매출액 기준(예: 전체 연결이 아니라 R&D를 수행하는 일부 계열사 매출만)을
    # 썼을 가능성이 큽니다. (KT&G로 실측: 각주에 "일부 계열사 매출 기준"이라고 명시돼 있었음)
    # 파싱이 틀린 게 아니라 방법론 차이라는 걸 알려주기 위한 안내입니다.
    rnd_discrepancy_note = None
    if rnd.get("ratio") is not None and rnd.get("report_ratio") is not None:
        if abs(rnd["ratio"] - rnd["report_ratio"]) > 1.0:
            rnd_discrepancy_note = (
                "원문 비율과 차이가 있습니다 - 회사가 전체 연결 매출이 아닌 다른 매출 기준(예: R&D를 "
                "수행하는 일부 계열사 매출만)으로 자체 계산했을 수 있습니다. 종목 간 비교 일관성을 위해 "
                "우리는 항상 전체 연결 매출액 기준으로 계산합니다."
            )

    rnd_value_text = "데이터 없음 (조회 실패)" if rnd_failed else "데이터 없음"
    if not rnd_failed and rnd.get("ratio") is not None:
        rnd_value_text = f"{rnd['ratio']:.2f}%"
        if rnd.get("report_ratio") is not None:
            rnd_value_text += f" (원문 명시: {rnd['report_ratio']}%)"
        if rnd_discrepancy_note:
            rnd_value_text += " ⚠️"
        if rnd_warning:
            rnd_value_text = "⚠️ " + rnd_value_text

    rnd_amount_text = "데이터 없음 (조회 실패)" if rnd_failed else "데이터 없음"
    if not rnd_failed and rnd.get("amount") is not None:
        if rnd.get("prior_amount") is not None:
            change_pct = (rnd["amount"] - rnd["prior_amount"]) / rnd["prior_amount"] * 100
            rnd_amount_text = f"{rnd['amount']:,}백만원 (전년대비 {change_pct:+.1f}%)"
        else:
            rnd_amount_text = f"{rnd['amount']:,}백만원 (전년도 값은 표 형식상 추출 불가)"

    capex_level_score, capex_trend_score, capex_score = _score_capex(capex_df)
    capex_text = "데이터 없음 (조회 실패)" if capex_failed else "데이터 없음"
    if not capex_failed and not capex_df.empty:
        series_text = ", ".join(f"{int(r['연도'])}:{r['CAPEX비율']:.1f}%" for _, r in capex_df.iterrows())
        level_text = f"{capex_level_score:.0f}점" if capex_level_score is not None else "N/A"
        trend_text = f"{capex_trend_score:.0f}점" if capex_trend_score is not None else "N/A"
        capex_text = f"{series_text} (수준 {level_text} · 3년 흐름 {trend_text})"

    items = [
        {
            "label": "R&D/매출 비율 (최신 연도, 수준)",
            "value_text": rnd_value_text,
            "score": rnd_score,
            "detail": (
                "사업보고서 원문의 '연구개발비용' 표에서 최신 연도 값을 추출해 매출액과 비교했습니다. "
                "'원문 명시' 값은 사업보고서에 직접 적힌 비율로, 우리 계산과 대조하는 검증용입니다. "
                + (f"⚠️ {rnd_warning} " if rnd_warning else "")
                + (f"⚠️ {rnd_discrepancy_note}" if rnd_discrepancy_note else "")
            ),
            "fetch_failed": rnd_failed,
        },
        {
            "label": "R&D 금액 전년대비",
            "value_text": rnd_amount_text,
            "score": _score_rnd_amount_change(rnd.get("amount"), rnd.get("prior_amount")),
            "detail": "R&D '비율'이 아니라 실제로 투입한 금액 자체가 전년보다 늘었는지 줄었는지를 봅니다. (연결/별도를 나란히 적은 표 형식에서는 전년도 값을 안전하게 추출할 수 없어 '데이터 없음'으로 표시될 수 있습니다)",
            "fetch_failed": rnd_failed,
        },
        {
            "label": "CAPEX/매출 (수준+추세)",
            "value_text": capex_text,
            "score": capex_score,
            "detail": (
                "설비투자(유형자산+무형자산 취득)를 매출액과 비교한 비율입니다. "
                "'수준'(최신 연도 절대값, 60%)과 '3년 흐름'(선형회귀 기울기, 40%)을 함께 봐서, "
                "투자를 많이 하는 기업이 단순 증감 방향만으로 낮은 점수를 받지 않도록 했습니다. "
                "흐름은 한 해 급감/급증이 아니라 3개년 전체 추세로 판단해서, 투자 사이클이 있는 "
                "업종의 정상적인 한 해 감소에 과민 반응하지 않습니다."
            ),
            "fetch_failed": capex_failed,
        },
    ]

    weights = [0.5, 0.25, 0.25]  # R&D 비율(수준) 50% / R&D 증감 25% / CAPEX 25%
    result = _make_weighted_axis_result(items, weights, CITATIONS["비전"])
    result["mentions"] = mentions
    result["fetch_failed"] = result.get("fetch_failed") or mentions_failed
    return result


# ==============================================================
# 미국 주식 (yfinance) - 5개 축
# ==============================================================

@st.cache_data(ttl=60 * 60 * 24)
def _get_us_sector(ticker: str) -> Optional[str]:
    """yfinance의 industry 문자열이 US_SECTOR_PEERS에 있는지 확인합니다."""
    try:
        industry = yf.Ticker(ticker).info.get("industry")
    except Exception:
        return None
    return industry if industry in US_SECTOR_PEERS else None


def _score_trend_change(values: list) -> Optional[float]:
    """값들의 첫 시점 대비 마지막 시점 변화율(%) 크기로 채점합니다. (CAPEX, R&D 추이 공용)

    원래는 "늘었으면 100점, 줄었으면 30점"처럼 방향만 봤는데, 그러다 보니 +7.8%와 +20.6%가
    똑같이 100점을 받고, -12.8%(꽤 큰 감소)와 +9.3%(미미한 증가)가 30점 vs 100점으로
    실제 차이보다 과도하게 벌어지는 문제가 있었습니다 (삼성전자·현대차 ⑤축 역전의 원인).
    변화율 크기 자체를 밴드로 나눠서 채점합니다.
    """
    valid = [v for v in values if v is not None]
    if len(valid) < 2 or not valid[0]:
        return None
    change_pct = (valid[-1] - valid[0]) / abs(valid[0]) * 100
    return _score_from_bands(change_pct, [(20, 100), (5, 80), (-5, 55), (-20, 30), (-999, 10)])


# ------------------------------
# ① 경영진 (미국)
# ------------------------------

@st.cache_data(ttl=60 * 60 * 24)
def get_us_dividend_streak_years(ticker: str) -> Optional[int]:
    """배당을 몇 년 연속 지급했는지 계산합니다. (연도별 배당 합계 기준)"""
    div = yf.Ticker(ticker).dividends
    if div.empty:
        return None
    yearly = div.groupby(div.index.year).sum()
    current_year = datetime.today().year
    streak = 0
    for year in range(current_year - 1, current_year - 30, -1):
        if year not in yearly.index:
            break
        if yearly[year] > 0:
            streak += 1
        else:
            break
    return streak


def get_us_management_axis(ticker: str) -> dict:
    """① 경영진의 역량과 도덕성 축을 계산합니다. (미국 주식)"""
    info, info_failed = _safe_call(lambda: yf.Ticker(ticker).info)
    info = info or {}
    streak, streak_failed = _safe_call(get_us_dividend_streak_years, ticker)

    fin5, fin5_failed = _safe_call(financials.get_us_financials, ticker, years=5)
    fin5 = fin5 or dict(financials.EMPTY_RESULT)
    fin5_failed = fin5_failed or fin5.get("fetch_failed", False)
    roe_values = fin5["trend"]["ROE"].tolist() if not fin5["trend"].empty else []
    roe_std = _std(roe_values)

    insider_pct = info.get("heldPercentInsiders")
    if insider_pct is not None:
        insider_pct *= 100

    items = [
        {
            "label": "배당 연속 지급 연수",
            "value_text": "데이터 없음 (조회 실패)" if streak_failed else (f"{streak}년 연속" if streak is not None else "데이터 없음"),
            "score": _score_dividend_streak(streak),
            "detail": "연도별 배당 지급 합계가 있었던 연속 연수입니다. (yfinance 배당 이력)",
            "fetch_failed": streak_failed,
        },
        {
            "label": "최근 5년 유상증자 횟수",
            "value_text": "데이터 없음 (미국은 대응 데이터 없음)",
            "score": None,
            "detail": "한국의 '유상증자'에 정확히 대응하는 미국 데이터가 없습니다. 현금흐름표의 주식 발행액은 스톡옵션 행사분과 섞여 있어 신뢰도가 낮아 사용하지 않았습니다.",
        },
        {
            "label": "감사의견",
            "value_text": "데이터 없음 (미국은 대응 데이터 없음)",
            "score": None,
            "detail": "yfinance에는 감사의견 데이터가 없습니다.",
        },
        {
            "label": "ROE 5년 표준편차",
            "value_text": "데이터 없음 (조회 실패)" if fin5_failed else (f"{roe_std:.2f}%p" if roe_std is not None else "데이터 없음"),
            "score": _score_roe_std(roe_std),
            "detail": "최근 5개년 ROE의 표준편차입니다.",
            "fetch_failed": fin5_failed,
        },
    ]

    result = _make_axis_result(items, CITATIONS["관리"])
    result["info"] = {
        "label": "내부자(경영진+이사회) 지분율 (참고용, 점수 미반영)",
        "value_text": "데이터 없음 (조회 실패)" if info_failed else (f"{insider_pct:.2f}%" if insider_pct is not None else "데이터 없음"),
        "detail": "한국의 '최대주주+특수관계인 지분율'과 완전히 같은 개념은 아닙니다 (지배주주 일가 지분이 아니라 경영진/이사회의 내부자 지분율에 가깝습니다). 참고용으로만 표시합니다.",
    }
    return result


# ------------------------------
# ② 해자·확장성 (미국)
# ------------------------------

def _get_us_gross_margin(ticker: str) -> Optional[float]:
    """최신 연도 매출총이익률(%)을 yfinance에서 계산합니다. (peer 계산에도 재사용)"""
    financials_df = yf.Ticker(ticker).financials
    if financials_df.empty or "Gross Profit" not in financials_df.index or "Total Revenue" not in financials_df.index:
        return None
    gross = financials_df.loc["Gross Profit"].iloc[0]
    revenue = financials_df.loc["Total Revenue"].iloc[0]
    return gross / revenue * 100 if revenue else None


def get_us_moat_axis(ticker: str) -> dict:
    """② 비즈니스 모델의 확장성 및 진입 장벽(해자) 축을 계산합니다. (미국 주식)"""
    fin5, fin5_failed = _safe_call(financials.get_us_financials, ticker, years=5)
    fin5 = fin5 or dict(financials.EMPTY_RESULT)
    fin5_failed = fin5_failed or fin5.get("fetch_failed", False)
    trend = fin5["trend"]

    op_margins = []
    if not trend.empty:
        # .get()을 씁니다 - 미국 은행주 등은 "영업이익" 계정 자체가 없을 수 있어서
        # row["영업이익"]로 바로 접근하면 KeyError가 납니다. (한국은 항상 값이 있어서
        # .get()을 써도 동작은 동일합니다)
        op_margins = [
            (row.get("영업이익") / row.get("매출액") * 100)
            if row.get("매출액") and row.get("영업이익") is not None
            else None
            for _, row in trend.iterrows()
        ]
    op_margin_latest = next((m for m in reversed(op_margins) if m is not None), None)
    op_margin_std = _std(op_margins)
    roe_avg = _avg(trend["ROE"].tolist()) if not trend.empty else None

    fin3, fin3_failed = _safe_call(financials.get_us_financials, ticker, years=3)
    fin3 = fin3 or dict(financials.EMPTY_RESULT)
    fin3_failed = fin3_failed or fin3.get("fetch_failed", False)
    cagr = None
    if not fin3["income"].empty and len(fin3["income"]) >= 2:
        rev_first = fin3["income"].iloc[0]["매출액"]
        rev_last = fin3["income"].iloc[-1]["매출액"]
        cagr = _cagr(rev_first, rev_last, len(fin3["income"]) - 1)

    gross_margin, gross_margin_failed = _safe_call(_get_us_gross_margin, ticker)

    sector = _get_us_sector(ticker)
    op_margin_delta = gross_margin_delta = None
    peer_note = None
    if sector:
        peers = [p for p in US_SECTOR_PEERS[sector]["peers"] if p != ticker]
        peer_op_margins, peer_gross_margins = [], []
        for peer in peers:
            peer_metrics = _get_peer_metrics("US", peer)  # peer_cache.json 우선, 없으면 실시간 조회
            if peer_metrics["operating_margin"] is not None:
                peer_op_margins.append(peer_metrics["operating_margin"])
            if peer_metrics["gross_margin"] is not None:
                peer_gross_margins.append(peer_metrics["gross_margin"])
        if peer_op_margins and op_margin_latest is not None:
            op_margin_delta = op_margin_latest - _avg(peer_op_margins)
        if peer_gross_margins and gross_margin is not None:
            gross_margin_delta = gross_margin - _avg(peer_gross_margins)
        peer_note = f"비교 대상(industry: {sector}): {', '.join(US_SECTOR_PEERS[sector]['peers'])} (자기 자신 제외)"
    else:
        peer_note = "이 종목의 industry를 대표 peer 목록에서 찾지 못해 상대 비교를 생략했습니다."

    items = [
        {
            "label": "영업이익률 (peer 대비 상대수준)",
            "value_text": (
                "데이터 없음 (조회 실패)" if fin5_failed else (
                    f"{op_margin_latest:.1f}% (peer 대비 {op_margin_delta:+.1f}%p)"
                    if op_margin_latest is not None and op_margin_delta is not None
                    else (f"{op_margin_latest:.1f}% (peer 비교 불가)" if op_margin_latest is not None else "데이터 없음")
                )
            ),
            "score": _score_relative_margin(op_margin_delta),
            "detail": "최신 연도 영업이익률을 같은 industry 대표 종목 평균과 비교합니다. " + (peer_note or ""),
            "fetch_failed": fin5_failed,
        },
        {
            "label": "영업이익률 5년 변동성",
            "value_text": "데이터 없음 (조회 실패)" if fin5_failed else (f"표준편차 {op_margin_std:.2f}%p" if op_margin_std is not None else "데이터 없음"),
            "score": _score_margin_std(op_margin_std),
            "detail": "최근 5개년 영업이익률의 표준편차입니다.",
            "fetch_failed": fin5_failed,
        },
        {
            "label": "매출총이익률 (peer 대비 상대수준)",
            "value_text": (
                "데이터 없음 (조회 실패)" if gross_margin_failed else (
                    f"{gross_margin:.1f}% (peer 대비 {gross_margin_delta:+.1f}%p)"
                    if gross_margin is not None and gross_margin_delta is not None
                    else (f"{gross_margin:.1f}% (peer 비교 불가)" if gross_margin is not None else "데이터 없음")
                )
            ),
            "score": _score_relative_margin(gross_margin_delta),
            "detail": "매출총이익률을 같은 industry 대표 종목 평균과 비교합니다.",
            "fetch_failed": gross_margin_failed,
        },
        {
            "label": "매출 3년 CAGR",
            "value_text": "데이터 없음 (조회 실패)" if fin3_failed else (f"연평균 {cagr:.1f}%" if cagr is not None else "데이터 없음"),
            "score": _score_cagr(cagr),
            "detail": "최근 3개년 매출액의 연평균 성장률입니다.",
            "fetch_failed": fin3_failed,
        },
        {
            "label": "ROE 5년 평균",
            "value_text": "데이터 없음 (조회 실패)" if fin5_failed else (f"{roe_avg:.2f}%" if roe_avg is not None else "데이터 없음"),
            "score": _score_roe_avg(roe_avg),
            "detail": "최근 5개년 ROE의 평균입니다.",
            "fetch_failed": fin5_failed,
        },
    ]
    weights = [0.15, 0.30, 0.15, 0.20, 0.20]
    return _make_weighted_axis_result(items, weights, CITATIONS["해자"])


# ------------------------------
# ③ 밸류에이션 (미국)
# ------------------------------

@st.cache_data(ttl=3600)
def get_us_per_band_percentile(ticker: str) -> Optional[dict]:
    """최근 3개년 '연말 시점 PER' 대비, 현재 PER의 백분위를 계산합니다. (한국과 동일한 단순화 방식)"""
    fin3 = financials.get_us_financials(ticker, years=3)
    if fin3["income"].empty or len(fin3["income"]) < 2:
        return None

    info = yf.Ticker(ticker).info
    shares = info.get("sharesOutstanding")
    current_price = info.get("currentPrice") or info.get("regularMarketPrice")
    if not shares or not current_price:
        return None

    end = datetime.today()
    start = end - timedelta(days=365 * 4)
    try:
        price_df = yf.Ticker(ticker).history(start=start, end=end)
    except Exception:
        return None
    if price_df.empty:
        return None
    price_df.index = price_df.index.tz_localize(None)

    historical_pers = []
    for _, row in fin3["income"].iterrows():
        year = int(row["연도"])
        net_income = row["당기순이익"]
        if not net_income or net_income <= 0:
            continue
        eps = net_income / shares
        year_prices = price_df.loc[f"{year}-01-01":f"{year}-12-31"]
        if year_prices.empty:
            continue
        year_end_price = year_prices["Close"].iloc[-1]
        historical_pers.append(year_end_price / eps)

    if not historical_pers:
        return None

    # 한국 쪽과 동일한 이유로, "현재" 순이익은 최근 4개 분기 합계(TTM)를 우선 사용합니다.
    ttm_net_income, eps_basis = None, None
    quarterly = financials.get_us_quarterly_trend(ticker)
    if not quarterly.empty:
        recent4 = quarterly["당기순이익"].dropna().tail(4)
        if len(recent4) == 4:
            ttm_net_income = recent4.sum()
            eps_basis = "최근 4개 분기 합산(TTM)"

    if ttm_net_income and ttm_net_income > 0:
        current_net_income = ttm_net_income
    else:
        current_net_income = fin3["income"].iloc[-1]["당기순이익"]
        eps_basis = f"최신 연간({int(fin3['income'].iloc[-1]['연도'])}년) 실적"

    if not current_net_income or current_net_income <= 0:
        return None
    current_eps = current_net_income / shares
    current_per = current_price / current_eps

    percentile, out_of_range = _band_position_percentile(current_per, historical_pers)
    return {
        "current_per": current_per,
        "historical_pers": historical_pers,
        "percentile": percentile,
        "out_of_range": out_of_range,
        "eps_basis": eps_basis,
    }


def get_us_valuation_axis(ticker: str) -> dict:
    """③ 수익성 및 밸류에이션 축을 계산합니다. (미국 주식)"""
    fin, fin_failed = _safe_call(financials.get_us_financials, ticker)
    fin = fin or dict(financials.EMPTY_RESULT)
    fin_failed = fin_failed or fin.get("fetch_failed", False)
    per, pbr = fin["per"], fin["pbr"]
    net_income_latest = fin["income"].iloc[-1]["당기순이익"] if not fin["income"].empty else None
    is_loss = net_income_latest is not None and net_income_latest <= 0

    sector = _get_us_sector(ticker)
    peer_median_per = peer_median_pbr = None
    per_delta_pct = pbr_delta_pct = None
    peer_note = None
    peer_names = []
    if sector:
        peers = [p for p in US_SECTOR_PEERS[sector]["peers"] if p != ticker]
        peer_pers, peer_pbrs, peer_details = [], [], []
        for peer in peers:
            peer_metrics = _get_peer_metrics("US", peer)  # peer_cache.json 우선, 없으면 실시간 조회
            peer_details.append({"code": peer, "name": peer, "per": peer_metrics["per"], "pbr": peer_metrics["pbr"]})
            if peer_metrics["per"] and peer_metrics["per"] > 0:
                peer_pers.append(peer_metrics["per"])
            if peer_metrics["pbr"] and peer_metrics["pbr"] > 0:
                peer_pbrs.append(peer_metrics["pbr"])
        # peer가 2~3개뿐인 표본에서는 평균 대신 중앙값을 써야 극단값 하나에 덜 흔들립니다.
        if peer_pers and per and per > 0:
            peer_median_per = _median(peer_pers)
            per_delta_pct = (peer_median_per - per) / peer_median_per * 100
        if peer_pbrs and pbr and pbr > 0:
            peer_median_pbr = _median(peer_pbrs)
            pbr_delta_pct = (peer_median_pbr - pbr) / peer_median_pbr * 100
        peer_names = [d["name"] for d in peer_details]
        peer_note = f"비교 대상(industry: {sector}) 개별 값 — {_format_peer_breakdown(peer_details)}"
    else:
        peer_note = "이 종목의 industry를 대표 peer 목록에서 찾지 못해 상대 비교를 생략했습니다."

    diverging_note = _diverging_valuation_note(per_delta_pct, pbr_delta_pct)

    band, band_failed = _safe_call(get_us_per_band_percentile, ticker)
    percentile = band["percentile"] if band else None
    out_of_range = band.get("out_of_range", False) if band else False

    items = [
        {
            "label": "PER",
            "value_text": "데이터 없음 (조회 실패)" if fin_failed else ("적자 (PER 의미 없음)" if is_loss else (f"{per:.2f}배" if per is not None else "데이터 없음")),
            "score": _score_per(per, is_loss),
            "detail": "적자 기업은 PER이 의미가 없어 낮은 점수로 처리합니다.",
            "fetch_failed": fin_failed,
        },
        {
            "label": "PBR",
            "value_text": "데이터 없음 (조회 실패)" if fin_failed else (f"{pbr:.2f}배" if pbr is not None else "데이터 없음"),
            "score": _score_pbr(pbr),
            "detail": "주가를 주당순자산으로 나눈 값입니다.",
            "fetch_failed": fin_failed,
        },
        {
            "label": "PER peer 대비 (참고용)",
            "value_text": "PER " + _format_peer_comparison(per, peer_median_per, peer_names) + (" ⚠️" if diverging_note else ""),
            "score": _score_relative_valuation(per_delta_pct),
            "detail": (
                "같은 industry 대표 종목 중앙값 PER과 비교합니다. " + (peer_note or "")
                + (f" {diverging_note}" if diverging_note else "")
                + " ⚠️ peer를 누구로 고르느냐에 따라 결론이 바뀔 수 있어서(peer가 2~3개뿐이라 표본이 작음) "
                "참고용으로만 보고, 아래 '3년 자기 PER 밴드'에 더 큰 비중을 뒀습니다."
            ),
            "fetch_failed": fin_failed,
        },
        {
            "label": "PBR peer 대비 (참고용)",
            "value_text": "PBR " + _format_peer_comparison(pbr, peer_median_pbr, peer_names) + (" ⚠️" if diverging_note else ""),
            "score": _score_relative_valuation(pbr_delta_pct),
            "detail": (
                "같은 industry 대표 종목 중앙값 PBR과 비교합니다." + (f" {diverging_note}" if diverging_note else "")
                + " ⚠️ peer를 누구로 고르느냐에 따라 결론이 바뀔 수 있어서 참고용으로만 봅니다."
            ),
            "fetch_failed": fin_failed,
        },
        {
            "label": "3년 자기 PER 밴드 백분위",
            "value_text": (
                "데이터 없음 (조회 실패)" if band_failed else (
                    f"백분위 {percentile:.0f}% (낮을수록 저평가)" + (" — 3년 밴드 범위를 벗어남" if out_of_range else "")
                    if percentile is not None else "데이터 없음"
                )
            ),
            "score": _score_per_band_percentile(percentile),
            "detail": (
                "최근 3개 연말 시점 PER과 비교한 근사치입니다. peer 선택에 좌우되지 않아서 이 축에서 가장 "
                "큰 비중(50%)을 둡니다. (현재 순이익 기준: "
                + (band.get("eps_basis", "-") if band else "-") + ") "
                + ("⚠️ 현재 PER이 최근 3년치보다 높거나 낮아 범위를 벗어났습니다." if out_of_range else "")
            ),
            "fetch_failed": band_failed,
        },
    ]

    note = None
    if sector and US_SECTOR_PEERS[sector]["cyclical"]:
        note = f"⚠️ 이 종목은 시클리컬(경기 순환) 업종({sector})으로 분류됩니다. " + CITATIONS["밸류"]

    weights = [0.15, 0.15, 0.10, 0.10, 0.50]
    return _make_weighted_axis_result(items, weights, CITATIONS["밸류"], note=note)


# ------------------------------
# ④ 주주환원 (미국)
# ------------------------------

def get_us_shareholder_return_axis(ticker: str) -> dict:
    """④ 주주 환원 및 배당 성향 축을 계산합니다. (미국 주식)"""
    info, info_failed = _safe_call(lambda: yf.Ticker(ticker).info)
    info = info or {}
    dividend_yield = info.get("dividendYield")  # 이 필드는 yfinance에서 이미 %(예: 0.32=0.32%) 단위로 옵니다.
    payout_ratio = info.get("payoutRatio")
    if payout_ratio is not None:
        payout_ratio *= 100  # 이 필드는 소수(0.1259=12.59%) 단위라 100을 곱해줘야 합니다.

    div, div_failed = _safe_call(lambda: yf.Ticker(ticker).dividends)
    div = div if div is not None else pd.Series(dtype=float)
    dps_series = []
    if not div.empty:
        yearly = div.groupby(div.index.year).sum()
        current_year = datetime.today().year
        for year in range(current_year - 4, current_year + 1):
            dps_series.append((year, float(yearly[year]) if year in yearly.index else None))

    cf, cf_failed = _safe_call(lambda: yf.Ticker(ticker).cashflow)
    cf = cf if cf is not None else pd.DataFrame()
    buyback_count = None
    if not cf.empty and "Repurchase Of Capital Stock" in cf.index:
        buyback_row = cf.loc["Repurchase Of Capital Stock"]
        buyback_count = int(sum(1 for v in buyback_row if v is not None and not pd.isna(v) and v < 0))

    items = [
        {
            "label": "배당수익률",
            "value_text": "데이터 없음 (조회 실패)" if info_failed else (f"{dividend_yield:.2f}%" if dividend_yield is not None else "데이터 없음"),
            "score": _score_dividend_yield(dividend_yield),
            "detail": "현재가 기준 배당수익률입니다.",
            "fetch_failed": info_failed,
        },
        {
            "label": "배당성향",
            "value_text": "데이터 없음 (조회 실패)" if info_failed else (f"{payout_ratio:.1f}%" if payout_ratio is not None else "데이터 없음"),
            "score": _score_payout_ratio(payout_ratio),
            "detail": "당기순이익 중 배당으로 지급한 비율입니다.",
            "fetch_failed": info_failed,
        },
        {
            "label": "5년 배당 증감 추이",
            "value_text": (
                "데이터 없음 (조회 실패)" if div_failed else (
                    ", ".join(f"{y}:${v:,.2f}" for y, v in dps_series if v is not None)
                    if any(v is not None for _, v in dps_series) else "데이터 없음"
                )
            ),
            "score": _score_dividend_trend(dps_series),
            "detail": "최근 5개년 연간 배당 합계(주당)의 증감 추이입니다.",
            "fetch_failed": div_failed,
        },
        {
            "label": "자기주식 매입 이력",
            "value_text": "데이터 없음 (조회 실패)" if cf_failed else (f"최근 {len(cf.columns) if not cf.empty else 0}개년 중 {buyback_count}개 연도에서 매입" if buyback_count is not None else "데이터 없음"),
            "score": _score_buyback_years(buyback_count),
            "detail": "현금흐름표의 자사주 매입 지출(Repurchase Of Capital Stock)이 있었던 연도 수입니다.",
            "fetch_failed": cf_failed,
        },
    ]
    return _make_axis_result(items, CITATIONS["환원"])


# ------------------------------
# ⑤ 미래비전 (미국)
# ------------------------------

def get_us_future_vision_axis(ticker: str) -> dict:
    """⑤ 미래 비전 및 창의적 적응력 축을 계산합니다. (미국 주식)

    yfinance는 R&D/CAPEX 모두 최근 5개년치를 깔끔하게 제공해서, 한국과 달리
    R&D도 "추이"까지 점수에 반영합니다. (한국은 사업보고서 표 형식이 회사마다 달라
    최신 연도 값만 안정적으로 뽑을 수 있었습니다)
    """
    yf_ticker = yf.Ticker(ticker)
    fin_df, fin_df_failed = _safe_call(lambda: yf_ticker.financials)
    fin_df = fin_df if fin_df is not None else pd.DataFrame()
    cf_df, cf_df_failed = _safe_call(lambda: yf_ticker.cashflow)
    cf_df = cf_df if cf_df is not None else pd.DataFrame()
    fetch_failed = fin_df_failed or cf_df_failed

    rnd_ratios, capex_ratios = [], []
    if not fin_df.empty and "Total Revenue" in fin_df.index:
        revenue_row = fin_df.loc["Total Revenue"]
        years_sorted = sorted(revenue_row.index)  # 오래된 연도부터
        if "Research And Development" in fin_df.index:
            rnd_row = fin_df.loc["Research And Development"]
            for y in years_sorted:
                if revenue_row[y] and not pd.isna(rnd_row.get(y, np.nan)):
                    rnd_ratios.append(rnd_row[y] / revenue_row[y] * 100)
        if not cf_df.empty and "Capital Expenditure" in cf_df.index:
            capex_row = cf_df.loc["Capital Expenditure"]
            for y in years_sorted:
                if revenue_row[y] and y in capex_row.index and not pd.isna(capex_row[y]):
                    capex_ratios.append(abs(capex_row[y]) / revenue_row[y] * 100)

    rnd_latest = rnd_ratios[-1] if rnd_ratios else None

    capex_level_score = _score_capex_level(capex_ratios[-1]) if capex_ratios else None
    capex_trend_score = _score_capex_trend_3y(capex_ratios) if capex_ratios else None
    if capex_level_score is None:
        capex_score = None
    elif capex_trend_score is None:
        capex_score = capex_level_score
    else:
        capex_score = capex_level_score * 0.6 + capex_trend_score * 0.4

    capex_text = "데이터 없음"
    if capex_ratios:
        series_text = ", ".join(f"{v:.1f}%" for v in capex_ratios)
        level_text = f"{capex_level_score:.0f}점" if capex_level_score is not None else "N/A"
        trend_text = f"{capex_trend_score:.0f}점" if capex_trend_score is not None else "N/A"
        capex_text = f"{series_text} (수준 {level_text} · 흐름 {trend_text})"

    items = [
        {
            "label": "R&D/매출 비율 (최신 연도, 수준)",
            "value_text": "데이터 없음 (조회 실패)" if fetch_failed else (f"{rnd_latest:.2f}%" if rnd_latest is not None else "데이터 없음"),
            "score": _score_rnd_ratio(rnd_latest),
            "detail": "최근 연간 손익계산서의 Research And Development 항목을 매출액과 비교했습니다.",
            "fetch_failed": fetch_failed,
        },
        {
            "label": "R&D 비율 추이",
            "value_text": "데이터 없음 (조회 실패)" if fetch_failed else (", ".join(f"{v:.1f}%" for v in rnd_ratios) if rnd_ratios else "데이터 없음"),
            "score": _score_trend_change(rnd_ratios),
            "detail": "최근 수년간 R&D/매출 비율의 증감 추이입니다. (오래된 연도 -> 최신 연도 순)",
            "fetch_failed": fetch_failed,
        },
        {
            "label": "CAPEX/매출 (수준+추세)",
            "value_text": "데이터 없음 (조회 실패)" if fetch_failed else capex_text,
            "score": capex_score,
            "detail": (
                "설비투자(Capital Expenditure)를 매출액과 비교한 비율입니다. "
                "'수준'(최신 연도 절대값, 60%)과 '전체 기간 흐름'(선형회귀 기울기, 40%)을 함께 봐서, "
                "투자를 많이 하는 기업이 단순 증감 방향만으로 낮은 점수를 받지 않도록 했습니다."
            ),
            "fetch_failed": fetch_failed,
        },
    ]

    weights = [0.5, 0.25, 0.25]  # R&D 비율(수준) 50% / R&D 추이 25% / CAPEX 25%
    result = _make_weighted_axis_result(items, weights, CITATIONS["비전"])
    result["mentions"] = []  # 미국은 사업보고서 원문 파싱을 구현하지 않아 인용은 비워둡니다.
    return result
