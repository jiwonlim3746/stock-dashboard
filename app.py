# 주식 정보 대시보드 (6단계: 투자원칙.md 5대 지표 레이더 차트 추가)
# - 종목을 검색하면 최근 1년 주가 차트 + 최근 3년 실적(매출액/영업이익/순이익) +
#   주요 투자지표(PER/PBR/ROE) + 지표 추이 그래프 + 투자원칙 5대 지표 + 최신 뉴스 10건을 보여줍니다.
# - 지표 이름 옆 물음표(?) 아이콘에 마우스를 올리면 쉬운 한국어 설명이 나옵니다.
# - 사이드바에서 추이 그래프를 "연간"(점 3개) / "분기"(점 최대 12개) 중 골라 볼 수 있습니다.

import pandas as pd
import plotly.graph_objects as go
import streamlit as st

import config  # .env 값을 미리 불러와 둡니다.
import financials
import glossary
import news
import principles
from stock_data import (
    detect_market,
    get_korea_price_history,
    get_korea_stock_name,
    get_us_price_history,
    resolve_korea_ticker,
)

# 웹페이지 기본 설정 (탭 제목, 레이아웃 등)
st.set_page_config(page_title="주식 정보 대시보드", page_icon="📈", layout="wide")

st.title("📈 주식 정보 대시보드")
st.caption("한국 주식은 종목명/코드, 미국 주식은 티커로 검색할 수 있습니다. (예: 삼성전자, 005930, AAPL)")

# 사이드바의 연간/분기 토글: 아래 "지표 추이" 그래프에만 적용됩니다.
# (실적 표, PER/PBR/ROE는 토글과 상관없이 항상 최근 3개년/최신 연도 기준입니다)
with st.sidebar:
    st.markdown("### ⚙️ 보기 설정")
    trend_view = st.radio(
        "지표 추이 그래프 기준",
        options=["연간", "분기"],
        index=1,  # 기본값을 "분기"로 (options 순서는 그대로, 초기 선택만 인덱스 1번인 분기로)
        help="추이 그래프의 점 개수를 연간(3개) 또는 분기(최대 12개) 중에서 고를 수 있어요.",
    )


def render_financials_section(fin: dict, unit_label: str, trend_df: pd.DataFrame, trend_note: str = None) -> None:
    """실적 표 + 막대그래프 + 지표 추이 그래프 + 주요 지표(PER/PBR/ROE)를 그리는 공통 함수.

    market(한국/미국) 구분 없이 같은 형태의 딕셔너리를 받아서 그리기 때문에,
    나중에 다른 시장을 추가하더라도 이 함수를 그대로 재사용할 수 있습니다.
    trend_df/trend_note는 사이드바의 연간/분기 토글에 따라 호출하는 쪽에서 미리 골라 넘겨줍니다.
    """
    st.divider()
    st.subheader("💰 기업 실적")

    income_df = fin.get("income")
    if income_df is None or income_df.empty:
        st.info("실적 데이터를 가져올 수 없습니다. (DART_API_KEY 설정 여부 또는 상장 여부를 확인해주세요)")
    else:
        table_df = income_df.set_index("연도")
        st.caption(f"최근 3개년 실적 (단위: {unit_label})")

        # column_config에 help를 넣으면 표의 컬럼 제목 옆에 물음표(?) 아이콘이 붙고,
        # 마우스를 올리면 glossary.py에 적어둔 설명이 풍선말(tooltip)로 나타납니다.
        # format="localized"는 큰 숫자를 지수 표기(예: 3.3e+14) 대신 천 단위 콤마로 보여줍니다.
        column_config = {
            column: st.column_config.NumberColumn(
                column,
                help=glossary.get_explanation(column),
                format="localized",
            )
            for column in table_df.columns
        }

        col_table, col_chart = st.columns(2)
        with col_table:
            st.dataframe(table_df, width="stretch", column_config=column_config)
        with col_chart:
            st.bar_chart(table_df)

        # 실적 표 바로 아래에 매출액/영업이익/당기순이익/ROE의 추이를 이어서 보여줍니다.
        render_trend_section(trend_df, trend_note)

    # 주요 투자지표
    # st.metric의 help 파라미터도 마찬가지로 물음표 아이콘 + 마우스오버 설명을 만들어줍니다.
    st.markdown("**주요 투자지표**")
    col1, col2, col3 = st.columns(3)
    col1.metric(
        "PER",
        f"{fin['per']:.2f}" if fin.get("per") is not None else "N/A",
        help=glossary.get_explanation("PER"),
    )
    col2.metric(
        "PBR",
        f"{fin['pbr']:.2f}" if fin.get("pbr") is not None else "N/A",
        help=glossary.get_explanation("PBR"),
    )
    col3.metric(
        "ROE",
        f"{fin['roe']:.2f}%" if fin.get("roe") is not None else "N/A",
        help=glossary.get_explanation("ROE"),
    )


def _summarize_pct_change(series: pd.Series) -> tuple:
    """지표(series)의 맨 처음 시점 대비 맨 마지막 시점 변화율(%)을 계산해 한 줄 요약을 만듭니다.

    연간이든 분기든 그래프에 실제로 표시된 구간을 그대로 요약하기 때문에,
    문구에 "3년"처럼 기간을 못 박지 않고 "조회 기간 동안"이라고 표현합니다.
    (미국 주식 분기 데이터는 Yahoo Finance 제한으로 3년치가 다 안 채워질 수 있기 때문)
    반환값: (요약 문자열, "up"(증가) | "down"(감소) | "flat"(변화 없음) | "unknown"(계산 불가))
    """
    valid = series.dropna()
    if len(valid) < 2:
        return "데이터가 부족해 변화율을 계산할 수 없어요.", "unknown"

    first, last = valid.iloc[0], valid.iloc[-1]
    if first == 0:
        return "조회 기간 첫 값이 0이라 변화율(%)을 계산할 수 없어요.", "unknown"

    pct = (last - first) / abs(first) * 100
    if pct > 0:
        return f"조회 기간 동안 {pct:.1f}% 증가했어요.", "up"
    if pct < 0:
        return f"조회 기간 동안 {abs(pct):.1f}% 감소했어요.", "down"
    return "조회 기간 동안 변화가 없어요.", "flat"


def render_trend_section(trend_df: pd.DataFrame, note: str = None) -> None:
    """매출액/영업이익/당기순이익/ROE의 추이를 꺾은선 그래프로 보여주는 함수.

    trend_df는 연간(연도가 3개) 또는 분기(예: "2024Q1"이 최대 12개) 형태 둘 다 받을 수 있고,
    "연도" 컬럼이 있으면 연간, "분기" 컬럼이 있으면 분기 데이터로 자동 인식해서 x축을 그립니다.
    각 그래프 아래에는 처음~마지막 시점 사이에 몇 % 증가/감소했는지 한 줄 요약을 함께 표시합니다.
    (증가: 초록색 상자, 감소: 빨간색 상자로 구분 - st.success/st.error가 기본으로 그렇게 색이 입혀집니다)
    """
    if trend_df is None or trend_df.empty:
        return

    period_col = "분기" if "분기" in trend_df.columns else "연도"
    label = "분기별 추이" if period_col == "분기" else "최근 3년 추이"
    st.markdown(f"**📈 {label}**")

    if note:
        st.info(note)

    metrics = [m for m in ["매출액", "영업이익", "당기순이익", "ROE"] if m in trend_df.columns]
    columns = st.columns(2)
    for i, metric in enumerate(metrics):
        with columns[i % 2]:
            st.caption(metric)
            st.line_chart(trend_df.set_index(period_col)[[metric]])

            summary, direction = _summarize_pct_change(trend_df[metric])
            if direction == "up":
                st.success(summary)
            elif direction == "down":
                st.error(summary)
            else:
                st.info(summary)


def render_principles_section(axes: dict) -> None:
    """투자원칙.md의 5대 지표를 레이더 차트 + 축별 근거로 보여주는 함수.

    axes: {"① 경영진 역량·도덕성": {axis 결과 딕셔너리}, ...} 형태.
    5개 축을 더한 "종합점수"는 절대 만들지 않고, 축별로만 보여줍니다 (사용자 요구사항).
    """
    st.divider()
    st.subheader("🎯 나의 투자 원칙 5대 지표")
    st.caption(
        "투자원칙.md에 정리해둔 5가지 기준을 실제 데이터로 계산했습니다. "
        "5개 축을 더한 종합점수는 만들지 않으니, 각 축을 하나씩 따로 보고 판단해주세요."
    )

    # 데이터 조회 서버(DART 등) 접속이 실패한 항목이 하나라도 있으면 화면 맨 위에서 한 번만 알려줍니다.
    # (항목 하나하나마다 원인을 밝히기보다, "일부가 비어있을 수 있다"는 것만 짧게 안내합니다)
    if any(axis.get("fetch_failed") for axis in axes.values()):
        st.warning(
            "⚠️ 일부 데이터를 불러오지 못했습니다. (서버 접속 지연 등으로 일부 항목이 비어있을 수 있어요 - "
            "그 항목에는 '조회 실패'라고 표시됩니다) 새로고침하면 다시 시도합니다."
        )

    # 레이더 차트는 점수가 실제로 계산됐고, 근거가 2개 이상(신뢰도 "normal")인 축만 그립니다.
    # 근거 1개짜리 축까지 그리면 극단값(예: 100점)이 다른 축과 똑같은 무게로 보여서 오해하기 쉽습니다.
    plot_labels = [label for label, axis in axes.items() if axis["score"] is not None and axis["reliability"] == "normal"]
    plot_scores = [axes[label]["score"] for label in plot_labels]
    skipped_labels = [
        label for label, axis in axes.items()
        if axis["score"] is None or axis["reliability"] == "low"
    ]

    if plot_labels:
        fig = go.Figure()
        fig.add_trace(
            go.Scatterpolar(
                r=plot_scores + [plot_scores[0]],  # 마지막에 첫 값을 다시 넣어야 도형이 닫힙니다.
                theta=plot_labels + [plot_labels[0]],
                fill="toself",
                name="점수",
            )
        )
        fig.update_layout(
            polar={"radialaxis": {"visible": True, "range": [0, 100]}},
            showlegend=False,
            margin={"l": 40, "r": 40, "t": 30, "b": 30},
            height=420,
        )
        st.plotly_chart(fig, width="stretch")

    if skipped_labels:
        st.caption(f"⚠️ 데이터가 부족하거나 근거가 적어 레이더 차트에 표시하지 못한 축: {', '.join(skipped_labels)}")

    for label, axis in axes.items():
        score = axis["score"]
        missing = axis["missing_count"]
        is_low_reliability = axis["reliability"] == "low"

        if score is None:
            score_text = "점수 계산 불가"
        elif is_low_reliability:
            score_text = f"{score:.0f}점 (참고용)"
        else:
            score_text = f"{score:.0f}점"
        missing_text = f" · {missing}개 항목 누락" if missing else ""

        st.markdown(f"#### {label} — {score_text}{missing_text}")

        # 요구사항: 점수 아래에 산출 근거를 반드시 표시
        basis_line = " · ".join(f"{it['label']} {it['value_text']}" for it in axis["items"])
        st.caption(basis_line)

        # 근거가 1개뿐이면 점수를 그대로 믿기 어려우니, 신뢰도가 낮다고 명확히 경고합니다.
        if is_low_reliability and score is not None:
            st.warning(f"⚠️ 근거 {axis['scored_count']}개 — 신뢰도 낮음. 이 축은 참고만 하고 다른 근거로 직접 판단해주세요.")

        if axis.get("note"):
            st.warning(axis["note"])

        # 요구사항: 점수를 펼치면 계산식과 원천 데이터를 볼 수 있게
        with st.expander("계산식 · 원천 데이터 보기"):
            for it in axis["items"]:
                item_score_text = f"{it['score']:.0f}점" if it["score"] is not None else "데이터 없음"
                st.markdown(f"**{it['label']}**: {it['value_text']} → {item_score_text}")
                st.caption(it["detail"])

            if axis.get("info"):
                st.markdown(f"**{axis['info']['label']}**: {axis['info']['value_text']}")
                st.caption(axis["info"]["detail"])

            if axis.get("mentions"):
                st.markdown("**사업보고서 원문 인용 (참고용, 점수에는 반영하지 않음)**")
                for snippet in axis["mentions"]:
                    st.markdown(f"> …{snippet}…")

            st.markdown("**투자원칙.md 인용**")
            st.caption(f"「{axis['citation']}」")


def render_news_section(news_list: list) -> None:
    """뉴스 목록(제목/날짜/출처)을 최신순으로 나열하는 공통 함수.

    제목은 마크다운 링크로 표시해서, 클릭하면 원문 뉴스로 바로 이동합니다.
    """
    st.divider()
    st.subheader("📰 최신 뉴스")

    if not news_list:
        st.info("관련 뉴스를 찾을 수 없습니다.")
        return

    for item in news_list:
        date_str = item["date"].strftime("%Y-%m-%d %H:%M") if item["date"] else "날짜 미상"
        st.markdown(f"**[{item['title']}]({item['link']})**")
        st.caption(f"{item['source']} · {date_str}")


def _select_trend_data(annual_trend_df: pd.DataFrame, quarterly_fetch_fn) -> tuple:
    """사이드바에서 고른 연간/분기(trend_view)에 맞는 추이 데이터를 골라줍니다.

    - "연간"을 골랐으면 그대로 연간 데이터를 씁니다.
    - "분기"를 골랐는데 분기 데이터가 하나도 없으면, 연간 데이터로 자동 대체하고 안내 문구를 붙입니다.
    - "분기"인데 일부만 있으면(주로 미국 주식) 있는 만큼 보여주되, 부족하다는 안내 문구를 붙입니다.
    반환값: (사용할 DataFrame, 화면에 보여줄 안내 문구 또는 None)
    """
    if trend_view == "연간":
        return annual_trend_df, None

    quarterly_df = quarterly_fetch_fn()
    if quarterly_df.empty:
        return annual_trend_df, "분기 데이터를 찾을 수 없어 연간 데이터로 대신 보여드려요."
    if len(quarterly_df) < 12:
        return (
            quarterly_df,
            f"최근 {len(quarterly_df)}개 분기 데이터만 제공돼요. "
            "(데이터 제공처 사정으로 3년(12개) 전체가 안 나올 수 있어요)",
        )
    return quarterly_df, None


# 종목 검색창
query = st.text_input("종목 검색", placeholder="예: 삼성전자, 005930, AAPL")

if query:
    market = detect_market(query)

    if market == "KR":
        # ------------------------------
        # 한국 주식 처리
        # ------------------------------
        ticker_code = resolve_korea_ticker(query)

        if ticker_code is None:
            st.error("종목을 찾을 수 없습니다. 정확한 종목명 또는 6자리 종목코드를 입력해주세요.")
        else:
            stock_name = get_korea_stock_name(ticker_code)
            df = get_korea_price_history(ticker_code)

            if df.empty:
                st.warning("주가 데이터가 없습니다.")
            else:
                st.subheader(f"{stock_name} ({ticker_code}) - 최근 1년 주가")
                st.line_chart(df["Close"])
                with st.expander("원본 데이터 보기"):
                    st.dataframe(df)

                # 한국 실적 정보는 DART API 키가 있어야 조회할 수 있습니다.
                if not config.DART_API_KEY:
                    st.divider()
                    st.subheader("💰 기업 실적")
                    st.warning(".env 파일에 DART_API_KEY를 입력하면 실적 정보를 볼 수 있습니다.")
                else:
                    fin = financials.get_korea_financials(ticker_code)
                    trend_df, trend_note = _select_trend_data(
                        fin.get("trend"),
                        lambda: financials.get_korea_quarterly_trend(ticker_code),
                    )
                    render_financials_section(fin, unit_label="원", trend_df=trend_df, trend_note=trend_note)

                # 투자원칙 5대 지표도 DART API 키가 있어야 조회할 수 있습니다.
                if not config.DART_API_KEY:
                    st.divider()
                    st.subheader("🎯 나의 투자 원칙 5대 지표")
                    st.warning(".env 파일에 DART_API_KEY를 입력하면 투자 원칙 지표를 볼 수 있습니다.")
                else:
                    with st.spinner("투자 원칙 5대 지표를 계산하는 중입니다 (peer 종목 비교 때문에 조금 걸릴 수 있어요)..."):
                        axes = {
                            "① 경영진 역량·도덕성": principles.get_korea_management_axis(ticker_code),
                            "② 해자·확장성": principles.get_korea_moat_axis(ticker_code),
                            "③ 밸류에이션": principles.get_korea_valuation_axis(ticker_code),
                            "④ 주주환원": principles.get_korea_shareholder_return_axis(ticker_code),
                            "⑤ 미래비전·적응력": principles.get_korea_future_vision_axis(ticker_code),
                        }
                    render_principles_section(axes)

                # 한국 뉴스는 네이버 API 키가 있어야 조회할 수 있습니다.
                if not config.NAVER_CLIENT_ID or not config.NAVER_CLIENT_SECRET:
                    st.divider()
                    st.subheader("📰 최신 뉴스")
                    st.warning(".env 파일에 NAVER_CLIENT_ID / NAVER_CLIENT_SECRET을 입력하면 뉴스를 볼 수 있습니다.")
                else:
                    news_list = news.get_korea_news(stock_name)
                    render_news_section(news_list)

    else:
        # ------------------------------
        # 미국 주식 처리
        # ------------------------------
        ticker = query.strip().upper()
        df = get_us_price_history(ticker)

        if df.empty:
            st.error("종목을 찾을 수 없습니다. 정확한 티커를 입력해주세요. (예: AAPL, TSLA)")
        else:
            st.subheader(f"{ticker} - 최근 1년 주가")
            st.line_chart(df["Close"])
            with st.expander("원본 데이터 보기"):
                st.dataframe(df)

            fin = financials.get_us_financials(ticker)
            trend_df, trend_note = _select_trend_data(
                fin.get("trend"),
                lambda: financials.get_us_quarterly_trend(ticker),
            )
            render_financials_section(fin, unit_label="달러", trend_df=trend_df, trend_note=trend_note)

            with st.spinner("투자 원칙 5대 지표를 계산하는 중입니다 (peer 종목 비교 때문에 조금 걸릴 수 있어요)..."):
                axes = {
                    "① 경영진 역량·도덕성": principles.get_us_management_axis(ticker),
                    "② 해자·확장성": principles.get_us_moat_axis(ticker),
                    "③ 밸류에이션": principles.get_us_valuation_axis(ticker),
                    "④ 주주환원": principles.get_us_shareholder_return_axis(ticker),
                    "⑤ 미래비전·적응력": principles.get_us_future_vision_axis(ticker),
                }
            render_principles_section(axes)

            news_list = news.get_us_news(ticker)
            render_news_section(news_list)
else:
    st.info("검색창에 종목명이나 티커를 입력하고 Enter를 눌러보세요.")
