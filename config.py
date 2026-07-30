# API 키(비밀 값)를 불러오는 모듈입니다.
# - 내 PC에서 실행할 때: .env 파일에서 읽습니다. (python-dotenv 사용)
# - Streamlit Community Cloud에 배포됐을 때: .env 파일을 올리지 않으므로,
#   대신 Streamlit이 제공하는 "Secrets"(비밀 값 저장소)에서 읽습니다.
#
# 아래 _get_secret() 함수가 둘 중 어디서 실행 중인지 자동으로 판단해서 값을 가져옵니다.
# app.py나 financials.py, news.py 등 다른 파일은 지금처럼
# config.DART_API_KEY 형태로 그대로 쓰면 되고, 따로 수정할 필요가 없습니다.

import os
from typing import Optional

import streamlit as st
from dotenv import load_dotenv

# .env 파일을 읽어서 환경 변수로 등록합니다. (배포 환경처럼 .env 파일이 없으면 그냥 아무 일도 안 일어남)
load_dotenv()


def _get_secret(key: str) -> Optional[str]:
    """키 이름으로 비밀 값을 가져옵니다. Secrets(배포 환경) 우선, 없으면 .env(로컬)를 봅니다.

    st.secrets는 Streamlit Cloud의 "Secrets" 설정 화면에 저장된 값을 읽어오는 기능인데,
    내 PC에는 그 설정 파일(secrets.toml)이 아예 없어서 접근만 해도 오류가 납니다.
    그래서 try/except로 감싸서, 오류가 나면(=로컬 환경이면) 그냥 넘어가고 .env 값을 대신 씁니다.
    """
    try:
        if key in st.secrets:
            return st.secrets[key]
    except Exception:
        pass
    return os.getenv(key)


# 실적 조회(DART 공시 API)에 사용할 키
DART_API_KEY = _get_secret("DART_API_KEY")

# 뉴스 조회(네이버 검색 API)에 사용할 키
NAVER_CLIENT_ID = _get_secret("NAVER_CLIENT_ID")
NAVER_CLIENT_SECRET = _get_secret("NAVER_CLIENT_SECRET")
