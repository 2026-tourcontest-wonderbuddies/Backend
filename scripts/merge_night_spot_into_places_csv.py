"""
AI_Hub 실측 방문 데이터로 야간 명소 플래그(is_night_spot)를 jeju_places_stay_time.csv에 병합한다.

야간 명소 = 매칭된 방문 관측 n>=MIN_OBS 이면서 20시 이후 도착 비율 >= MIN_NIGHT_RATIO.
체인 매장(CHAIN_RETAIL_DENYLIST + EXTRA_EXCLUDE_TITLES)과 표본이 작아 우연으로 보이는 장소(MANUAL_EXCLUDE_TITLES)는 뺀다.

실행: python scripts/merge_night_spot_into_places_csv.py
대상 CSV를 제자리에서 덮어쓴다 (재실행 가능/멱등).
"""

import sys
from pathlib import Path

import pandas as pd

BASE = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(BASE))
from apps.recommendation.scoring import CHAIN_RETAIL_DENYLIST  # noqa: E402

VISIT_CSV = Path(r"C:\Users\zx90o\OneDrive\Desktop\moving\AI_Hub\04_course_visit_제주.csv")
PLACE_LINK = Path(r"C:\Users\zx90o\Downloads\이동엔진\matching\data\output\place_link.csv")
TARGET_CSV = BASE / "data" / "jeju_places_stay_time.csv"

NIGHT_HOUR = 20
MIN_OBS = 3
MIN_NIGHT_RATIO = 0.25

# 인기 장소 denylist에 없는, 저녁 쇼핑으로만 잡힌 매장 이름
EXTRA_EXCLUDE_TITLES = ("마트", "탑텐", "헬로제주", "선물가게 바나나")
# 실측은 통과하지만 표본이 작아 야간 명소로 보기 애매하다고 팀이 뺀 장소 (2026-09-19)
MANUAL_EXCLUDE_TITLES = ("청수곶자왈", "제주민속촌", "문섬·섶섬·범섬·새섬", "관덕정")
# 2안(overview 키워드) 참고로 팀이 수동 추가한 장소. 실측 조건과 무관하게 포함 (2026-09-19)
MANUAL_INCLUDE_TITLES = (
    "어영공원", "용담해안도로", "자구리문화예술공원(자구리공원)", "제주별빛누리공원", "불란지야시장",
)


def main():
    visits = pd.read_csv(VISIT_CSV, encoding="utf-8-sig")
    visits = visits[visits["IS_COURSE_CAND"] == True]  # noqa: E712
    link = pd.read_csv(PLACE_LINK, encoding="utf-8-sig", dtype={"content_id": str})[["content_id", "PLACE_KEY"]]
    obs = visits.merge(link.drop_duplicates(), on="PLACE_KEY")
    obs["is_night"] = obs["ARRIVE_HOUR"] >= NIGHT_HOUR
    stat = obs.groupby("content_id")["is_night"].agg(n="size", ratio="mean")
    stat = stat[(stat["n"] >= MIN_OBS) & (stat["ratio"] >= MIN_NIGHT_RATIO)]

    places = pd.read_csv(TARGET_CSV, encoding="utf-8-sig", dtype={"content_id": str})
    excluded = CHAIN_RETAIL_DENYLIST + EXTRA_EXCLUDE_TITLES + MANUAL_EXCLUDE_TITLES
    not_excluded = ~places["title"].apply(lambda t: any(x in t for x in excluded))
    manual = places["title"].isin(MANUAL_INCLUDE_TITLES)
    places["is_night_spot"] = (places["content_id"].isin(stat.index) & not_excluded) | manual
    places.to_csv(TARGET_CSV, index=False, encoding="utf-8-sig")

    print(f"{TARGET_CSV.name}: {len(places)}곳 중 is_night_spot {places['is_night_spot'].sum()}곳")
    print(places.loc[places["is_night_spot"], "title"].to_string())


if __name__ == "__main__":
    main()
