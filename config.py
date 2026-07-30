# .env 파일에 저장된 환경 변수(API 키 등)를 불러오는 모듈입니다.
# 지금 당장은 사용하지 않지만, 나중에 실적(DART)이나 뉴스(네이버) 기능을
# 추가할 때 이 파일에서 키를 가져다 쓰면 됩니다.

import os
from dotenv import load_dotenv

# .env 파일을 읽어서 환경 변수로 등록합니다.
load_dotenv()

# 추후 실적 조회(DART 공시 API)에 사용할 키
DART_API_KEY = os.getenv("DART_API_KEY")

# 추후 뉴스 조회(네이버 검색 API)에 사용할 키
NAVER_CLIENT_ID = os.getenv("NAVER_CLIENT_ID")
NAVER_CLIENT_SECRET = os.getenv("NAVER_CLIENT_SECRET")
