"""
홈 "JEJU BY TIME OF DAY" 카드용 시간대별 장소 목록을 만든다.

근거가 두 갈래다.
  아침/낮/노을/밤 — AI Hub 실측 도착시각의 시간대 특징도(lift)
  새벽           — 영업·개방 시간 (도착시각 표본이 0.8%뿐이라 랭킹이 불안정)

출력: data/period_places.json  (커밋해서 런타임에 읽는다. 배포 시 실행되지 않는다.)
"""

import argparse
import json
import math
from collections import Counter, defaultdict
from pathlib import Path

import pandas as pd

BASE = Path(__file__).resolve().parent.parent

AI_HUB = Path(r"C:\Users\zx90o\Downloads\이동엔진\AI_Hub\03_visit_제주.csv")
LINK = Path(r"C:\Users\zx90o\Downloads\이동엔진\matching\data\output\place_link_verified.csv")
HOURS = BASE / "data" / "hours_cache.json"
OUT = BASE / "data" / "period_places.json"
DB_CSVS = [BASE / "data" / "jeju_places_stay_time.csv",
           BASE / "data" / "jeju_restaurants_stay_time.csv"]

PERIODS = ["dawn", "morning", "midday", "sunset", "night"]
ARRIVAL_PERIODS = ["morning", "midday", "sunset", "night"]
TOP_N = 10
MIN_N = 3          # 시간대별 최소 관측 수
MIN_LIFT = 1.2     # 그 시간대 쏠림이 전체 평균보다 20% 이상
SMOOTH_K = 5.0     # 전역 사전확률로 끌어당기는 강도
DAWN_WINDOW = (4 * 60, 7 * 60)


def period_for(hour):
    """프론트 src/hooks/useNow.ts 의 periodFor 와 동일한 경계."""
    if 4 <= hour < 7:
        return "dawn"
    if 7 <= hour < 11:
        return "morning"
    if 11 <= hour < 16:
        return "midday"
    if 16 <= hour < 19:
        return "sunset"
    return "night"


def to_min(hhmm):
    h, m = hhmm.split(":")
    return int(h) * 60 + int(m)


def load_visits(ai_hub, link):
    """AI Hub 방문 로그를 TourAPI content_id 에 귀속시킨다."""
    v = pd.read_csv(ai_hub, encoding="utf-8-sig")
    v = v[v["IS_JEJU"] == True].dropna(subset=["ARRIVE_HOUR"])

    lk = pd.read_csv(link, encoding="utf-8-sig")
    lk["content_id"] = lk["content_id"].astype(str)

    j = v.merge(lk[["content_id", "PLACE_KEY"]], on="PLACE_KEY", how="inner")
    j["period"] = j["ARRIVE_HOUR"].astype(int).map(period_for)
    return j


def rank_by_arrival(visits):
    """장소가 그 시간대에 얼마나 치우쳐 있는지(lift)로 순위를 매긴다."""
    prior = visits["period"].value_counts(normalize=True).to_dict()

    n = Counter(zip(visits["content_id"], visits["period"]))
    total = Counter(visits["content_id"])

    out = defaultdict(list)
    for (cid, period), cnt in n.items():
        if period not in ARRIVAL_PERIODS or cnt < MIN_N:
            continue
        p = prior[period]
        share_s = (cnt + SMOOTH_K * p) / (total[cid] + SMOOTH_K)
        lift = share_s / p
        if lift < MIN_LIFT:
            continue
        out[period].append({
            "content_id": cid,
            "evidence": "arrival",
            "n": cnt,
            "total": total[cid],
            "share": round(cnt / total[cid], 3),
            "lift": round(lift, 2),
            "score": round(lift * math.log1p(cnt), 2),
        })

    for period in out:
        out[period].sort(key=lambda r: -r["score"])
        del out[period][TOP_N:]
    return out


def rank_dawn(hours_cache, observed):
    """
    새벽에 '문을 여는' 곳. open 시각이 04:00~07:00 안에 있을 것을 요구하면
    00:00/01:00부터 열려 있는 24시간 편의점이 자연히 빠진다.
    """
    rows = []
    for cid, rec in hours_cache.items():
        rule = rec["hours_rule"]
        if rule["type"] != "windows":
            continue
        for w in rule.get("windows", []):
            o, c = to_min(w["open"]), to_min(w["close"])
            if c <= o:
                continue  # 파싱 오류 (예: 협재동굴 18:30~18:00)
            if DAWN_WINDOW[0] <= o < DAWN_WINDOW[1]:
                obs = observed.get(cid, 0)
                if obs:  # 실제 방문 기록이 있는 곳만
                    rows.append({
                        "content_id": cid,
                        "evidence": "hours",
                        "open": w["open"],
                        "close": w["close"],
                        "obs": obs,
                        "_o": o,
                    })
                break

    rows.sort(key=lambda r: (r["_o"], -r["obs"]))
    for r in rows:
        del r["_o"]
    return rows[:TOP_N]


def main():
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--ai-hub", type=Path, default=AI_HUB)
    ap.add_argument("--link", type=Path, default=LINK)
    ap.add_argument("--hours", type=Path, default=HOURS)
    ap.add_argument("--out", type=Path, default=OUT)
    args = ap.parse_args()

    visits = load_visits(args.ai_hub, args.link)
    result = rank_by_arrival(visits)
    result["dawn"] = rank_dawn(
        json.loads(args.hours.read_text(encoding="utf-8")),
        Counter(visits["content_id"]),
    )
    result = {p: result[p] for p in PERIODS}

    check(result)
    args.out.write_text(json.dumps(result, ensure_ascii=False, indent=1), encoding="utf-8")

    print(f"wrote {args.out}")
    for p in PERIODS:
        print(f"  {p:<8} {len(result[p]):>2}곳  ({result[p][0]['evidence']})")


def check(result):
    known = set()
    for csv in DB_CSVS:
        known |= set(pd.read_csv(csv, encoding="utf-8-sig")["content_id"].astype(str))

    for period in PERIODS:
        rows = result[period]
        assert len(rows) == TOP_N, f"{period}: {len(rows)}곳 (기대 {TOP_N})"

        want = "hours" if period == "dawn" else "arrival"
        assert all(r["evidence"] == want for r in rows), f"{period}: evidence 불일치"

        missing = [r["content_id"] for r in rows if r["content_id"] not in known]
        assert not missing, f"{period}: DB에 없는 content_id {missing}"

        if want == "arrival":
            scores = [r["score"] for r in rows]
            assert scores == sorted(scores, reverse=True), f"{period}: 정렬 깨짐"

    overlap = sum(
        len({r["content_id"] for r in result[a]} & {r["content_id"] for r in result[b]})
        for i, a in enumerate(PERIODS) for b in PERIODS[i + 1:]
    )
    assert overlap <= 4, f"카드 간 중복 {overlap}건 (기대 4건 이하)"


if __name__ == "__main__":
    main()
