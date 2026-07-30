# 주식 정보 대시보드 (4단계: 경제 용어 해설 추가)
# - 종목을 검색하면 최근 1년 주가 차트 + 최근 3년 실적(매출액/영업이익/순이익) +
#   주요 투자지표(PER/PBR/ROE) + 최신 뉴스 10건을 함께 보여줍니다.
# - 지표 이름 옆 물음표(?) 아이콘에 마우스를 올리면 쉬운 한국어 설명이 나옵니다.

import streamlit as st

import config  # .env 값을 미리 불러와 둡니다.
import financials
import glossary
import news
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


def render_financials_section(fin: dict, unit_label: str) -> None:
    """실적 표 + 막대그래프 + 주요 지표(PER/PBR/ROE)를 그리는 공통 함수.

    market(한국/미국) 구분 없이 같은 형태의 딕셔너리를 받아서 그리기 때문에,
    나중에 다른 시장을 추가하더라도 이 함수를 그대로 재사용할 수 있습니다.
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
                    render_financials_section(fin, unit_label="원")

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
            render_financials_section(fin, unit_label="달러")

            news_list = news.get_us_news(ticker)
            render_news_section(news_list)
else:
    st.info("검색창에 종목명이나 티커를 입력하고 Enter를 눌러보세요.")
