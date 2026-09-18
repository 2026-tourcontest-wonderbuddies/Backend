"""
AI_Hub 실측 방문 데이터(obs_travel_n) 기반 인기도 점수를 jeju_places_stay_time.csv /
jeju_restaurants_stay_time.csv에 병합한다.

popularity_score = 1 - exp(-0.2 * obs_travel_n)  (기존 qual_rel과 동일한 형태, obs_travel_n=10 → 0.865)
매칭 안 되는 장소는 0.0 (미관측 = 인기 근거 없음. satisfaction_score의 0.5 중립 폴백과는 다름).

실행: python scripts/merge_popularity_into_places_csv.py
대상 CSV들을 제자리에서 덮어쓴다 (재실행 가능/멱등).
"""

import math
from pathlib import Path

import pandas as pd

BASE = Path(__file__).resolve().parent.parent
STAY_BY_POI = Path(r"C:\Users\zx90o\Downloads\이동엔진\matching\data\output\stay_time_by_poi.csv")
TARGET_CSVS = [
    BASE / "data" / "jeju_places_stay_time.csv",
    BASE / "data" / "jeju_restaurants_stay_time.csv",
]


def popularity_score(obs_travel_n) -> float:
    if pd.isna(obs_travel_n) or obs_travel_n <= 0:
        return 0.0
    return 1 - math.exp(-0.2 * obs_travel_n)


def main():
    pop = pd.read_csv(STAY_BY_POI, encoding="utf-8-sig", usecols=["content_id", "obs_travel_n"])
    pop["content_id"] = pop["content_id"].astype(str)
    pop["popularity_score"] = pop["obs_travel_n"].apply(popularity_score)
    pop = pop[["content_id", "popularity_score"]]

    for csv_path in TARGET_CSVS:
        places = pd.read_csv(csv_path, encoding="utf-8-sig", dtype={"content_id": str})
        places = places.drop(columns=["popularity_score"], errors="ignore")
        merged = places.merge(pop, on="content_id", how="left")
        merged["popularity_score"] = merged["popularity_score"].fillna(0.0)

        merged.to_csv(csv_path, index=False, encoding="utf-8-sig")

        n_popular = (merged["popularity_score"] >= 0.85).sum()
        print(f"{csv_path.name}: {len(merged)}곳 중 popularity_score>=0.85(인기 장소) {n_popular}곳")


if __name__ == "__main__":
    main()
