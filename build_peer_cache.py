# peer_cache.json을 새로 만들거나 갱신하는 스크립트입니다.
#
# 투자원칙 5대 지표의 ②③ 축은 종목 하나를 볼 때마다 같은 업종 peer(최대 3~5개)의
# PER/PBR/영업이익률/매출총이익률을 함께 조회하는데, 이걸 매번 실시간으로 하면
# 종목 하나 볼 때 DART 호출이 크게 늘어납니다 (Streamlit Cloud처럼 DART와 먼 서버에서는
# 이게 타임아웃의 주요 원인이었습니다).
#
# 그래서 peer 값들을 미리 계산해서 이 스크립트로 peer_cache.json에 저장해두고,
# 앱(principles.py)은 그 파일을 읽기만 하도록 바꿨습니다. peer 값은 분기 실적
# 발표 전까지는 자주 안 바뀌므로, 이 스크립트는 "필요할 때"(분기 실적 발표 후,
# 또는 peer 목록을 수정했을 때) 직접 실행하면 됩니다. 앱이 알아서 주기적으로
# 돌리지는 않습니다.
#
# 사용법 (로컬 PC 터미널에서):
#     python build_peer_cache.py
#
# 실행 후 peer_cache.json이 갱신되면, 배포판(Streamlit Cloud)에도 반영하기 위해
# 그 파일을 git commit & push 해야 합니다.

import json
from datetime import datetime, timezone

import financials
import principles


def _fetch_kr(ticker: str) -> dict:
    """한국 종목 하나의 PER/PBR/영업이익률/매출총이익률을 실시간으로 조회합니다."""
    fin = financials.get_korea_financials(ticker, years=1)
    gross_margin = principles.get_gross_margin(ticker)
    operating_margin = None
    if not fin["income"].empty:
        row = fin["income"].iloc[-1]
        if row["매출액"]:
            operating_margin = row["영업이익"] / row["매출액"] * 100
    return {
        "per": fin["per"],
        "pbr": fin["pbr"],
        "gross_margin": gross_margin,
        "operating_margin": operating_margin,
    }


def _fetch_us(ticker: str) -> dict:
    """미국 종목 하나의 PER/PBR/영업이익률/매출총이익률을 실시간으로 조회합니다."""
    fin = financials.get_us_financials(ticker, years=1)
    gross_margin = principles._get_us_gross_margin(ticker)
    operating_margin = None
    if not fin["income"].empty:
        row = fin["income"].iloc[-1]
        # .get()을 씁니다 - 은행주(JPM, BAC, WFC)처럼 "영업이익" 계정 자체가
        # 없는 업종은 row["영업이익"]로 바로 접근하면 KeyError가 납니다.
        revenue = row.get("매출액")
        operating_income = row.get("영업이익")
        if revenue and operating_income is not None:
            operating_margin = operating_income / revenue * 100
    return {
        "per": fin["per"],
        "pbr": fin["pbr"],
        "gross_margin": gross_margin,
        "operating_margin": operating_margin,
    }


def main() -> None:
    kr_tickers = sorted({t for info in principles.KR_SECTOR_PEERS.values() for t in info["peers"]})
    us_tickers = sorted({t for info in principles.US_SECTOR_PEERS.values() for t in info["peers"]})

    cache = {
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "KR": {},
        "US": {},
    }

    print(f"한국 peer {len(kr_tickers)}개 조회 중...")
    for i, ticker in enumerate(kr_tickers, 1):
        try:
            cache["KR"][ticker] = _fetch_kr(ticker)
            print(f"  [{i}/{len(kr_tickers)}] {ticker} 완료 -> {cache['KR'][ticker]}")
        except Exception as e:
            print(f"  [{i}/{len(kr_tickers)}] {ticker} 실패: {e}")

    print(f"미국 peer {len(us_tickers)}개 조회 중...")
    for i, ticker in enumerate(us_tickers, 1):
        try:
            cache["US"][ticker] = _fetch_us(ticker)
            print(f"  [{i}/{len(us_tickers)}] {ticker} 완료 -> {cache['US'][ticker]}")
        except Exception as e:
            print(f"  [{i}/{len(us_tickers)}] {ticker} 실패: {e}")

    with open("peer_cache.json", "w", encoding="utf-8") as f:
        json.dump(cache, f, ensure_ascii=False, indent=2)

    print(f"\n완료: peer_cache.json에 한국 {len(cache['KR'])}개 / 미국 {len(cache['US'])}개 종목을 저장했습니다.")
    print("배포판(Streamlit Cloud)에 반영하려면 이 파일을 git commit & push 하세요.")


if __name__ == "__main__":
    main()
