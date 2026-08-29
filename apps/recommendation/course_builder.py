"""
Pipeline 5.4~5.6 — 휴리스틱 빔서치 + Macro 평가로 하루치 코스를 완성한다.

전체 흐름:
  1. (외부에서 준비) 지역 필터링된 후보 풀
  2. 빔서치로 완성 코스 10개 생성 (5.4)
  3. 완성 코스별 Macro 점수 계산 (5.5~5.6)
  4. 최고점 코스 1개 반환
  *이전 Day에서 방문한 장소는 다음 Day 후보에서 제외
  * 목표 슬롯을 채워도 필수 식사가 해결 안됐을 경우, 안전 상한(12곳)까지 탐색 확장

  ?: 여유 시간이 많이 남는 문제 

이동시간 컷(60분 초과), 시간예산 체크는 filters.py가 담당하고,
이 모듈은 "여러 후보를 조합해서 코스 형태로 완성하는" 조립 로직에 집중한다.
"""

from copy import deepcopy
from apps.recommendation.filters import filter_candidates
from apps.recommendation.scoring import score_candidate
from apps.recommendation.food_scoring import apply_relax_penalty


BEAM_WIDTH = 10          # 5.4 유지 코스 수 (계산량 부담되면 5로 축소 가능, 구조는 동일)
TOP_K_EXPAND = 5         # 매 단계 확장 후보 수
SAFETY_CAP_SLOTS = 12

TARGET_SLACK = {"dist": 0.05, "pref": 0.10, "relax": 0.20}

MACRO_WEIGHTS = {
    "dist":  {"pref": 0.20, "qual": 0.10, "move_eff": 0.60, "slack": 0.10},
    "pref":  {"pref": 0.60, "qual": 0.20, "move_eff": 0.15, "slack": 0.05},
    "relax": {"pref": 0.25, "qual": 0.15, "move_eff": 0.20, "slack": 0.40},
}


class PartialCourse:
    """빔서치 중간 상태 하나를 표현. '부분 코스 R' 그 자체다."""

    def __init__(self, start_place, start_time_min: int):
        self.items = []                 # [{"place":, "travel_min":, "stay_min":, "micro_score":, "slot_type":}, ...]
        self.current_place = start_place
        self.current_time_min = start_time_min
        self.micro_scores = []          # SearchScore 계산용 누적치
        self.visited_ids = set()
        self.meal_filled = {"lunch": False, "dinner": False}
        if start_place:
            self.visited_ids.add(start_place.content_id)

    def clone(self):
        new = PartialCourse.__new__(PartialCourse)
        new.items = deepcopy(self.items)
        new.current_place = self.current_place
        new.current_time_min = self.current_time_min
        new.micro_scores = list(self.micro_scores)
        new.visited_ids = set(self.visited_ids)
        new.meal_filled = dict(self.meal_filled)
        return new

    def add(self, scored_candidate: dict, slot_type: str = "GENERAL"):
        """스코어링 완료된 후보 하나를 코스에 추가하고 상태를 갱신한다."""
        place = scored_candidate["place"]
        self.items.append({
            "place": place,
            "travel_min": scored_candidate["travel_min"],
            "stay_min": scored_candidate["stay_min"],
            "micro_score": scored_candidate["micro_score"],
            "slot_type": slot_type,
            "hours_uncertain": scored_candidate.get("hours_uncertain", False),
        })
        self.current_time_min += scored_candidate["travel_min"] + scored_candidate["stay_min"]
        self.current_place = place
        self.visited_ids.add(place.content_id)
        self.micro_scores.append(scored_candidate["micro_score"])
        if slot_type == "RESTAURANT":
            if not self.meal_filled["lunch"]:
                self.meal_filled["lunch"] = True
            else:
                self.meal_filled["dinner"] = True

    def search_score(self) -> float:
        return sum(self.micro_scores) / len(self.micro_scores) if self.micro_scores else 0.0


def beam_search_day(
    candidate_pool: list,       # Place 인스턴스 리스트 (지역 필터링 완료된 상태)
    start_place,                # 출발 장소 (숙소 등). None이면 좌표 없이 시작
    avail_hours: float,
    target_slots: int,
    mode: str,
    purpose_main: str,
    purpose_sub: str,
    transport_mode: str,
    region_quadrant: str,
    exclude_place_ids: list,
    exclude_categories: list,
    visit_start_datetime,
    get_travel_time_fn,
    get_stay_time_fn,
    need_lunch: bool = False,
    need_dinner: bool = False,
    visited_across_days: set[str] = None,
    nlp_match_score: float | None = None,
) -> list[PartialCourse]:
    """
    5.4 빔서치 본체. 완성 코스 최대 BEAM_WIDTH개를 반환한다.
    반환값을 course_builder.select_best_course()에 넘겨 Macro 평가를 받는다.

    주의: 음식 슬롯(RESTAURANT/CAFE/SNACK)은 이 함수에서 다루지 않는다.
    일반 장소만으로 채운 뒤, engine.py 레벨에서 food_scoring 결과를 병합하는 걸 권장한다
    (일반 장소와 음식 슬롯을 완전히 분리해서 "지정된 food_type 후보끼리만 비교"하라는
    문서 규칙(§음식점로직-2 "음식 슬롯에서는 지정된 food_type 후보끼리만 비교")을 지키기 위함).
    """
    avail_min = avail_hours * 60
    visited_across_days = visited_across_days or set()
    beams = [PartialCourse(start_place, start_time_min=0)]

    while True:
        def _done(b):
            slot_ok = len(b.items) >= target_slots
            meal_ok = (not need_lunch or b.meal_filled["lunch"]) and (not need_dinner or b.meal_filled["dinner"])
            hit_cap = len(b.items) >= SAFETY_CAP_SLOTS
            return (slot_ok and meal_ok) or hit_cap

        # 종료 조건: 목표 슬롯 수를 채웠거나, 모든 빔이 더 이상 확장 불가
        if all(_done(b) for b in beams):
            break

        new_beams = []
        any_expanded = False

        for beam in beams:
            remain_time = avail_min - beam.current_time_min
            if remain_time <= 0 or _done(beam):
                new_beams.append(beam)  # 더 확장 안 하고 그대로 유지
                continue

            excluded_ids = beam.visited_ids | visited_across_days
            # 이미 방문한 장소는 후보에서 제외
            fresh_candidates = [p for p in candidate_pool if p.content_id not in beam.visited_ids]

            filtered = filter_candidates(
                current_place=beam.current_place,
                candidates=fresh_candidates,
                visit_datetime=visit_start_datetime,  # 단순화: 실제로는 beam.current_time_min을 datetime에 더해야 정확함
                remain_time_min=remain_time,
                transport_mode=transport_mode,
                region_quadrant=region_quadrant,
                exclude_place_ids=exclude_place_ids,
                exclude_categories=exclude_categories,
                get_travel_time_fn=get_travel_time_fn,
                get_stay_time_fn=get_stay_time_fn,
            )

            scored = [
                score_candidate(c, mode, purpose_main, purpose_sub, remain_time, nlp_match_score)
                for c in filtered
            ]
            scored.sort(key=lambda x: x["micro_score"], reverse=True)
            top5 = scored[:TOP_K_EXPAND]   # Top5 확장

            if not top5:
                new_beams.append(beam)  # 확장할 게 없으면 현재 상태로 종료
                continue

            any_expanded = True
            for candidate in top5:
                branched = beam.clone()
                branched.add(candidate)
                new_beams.append(branched)

        # 가지치기: SearchScore 상위 BEAM_WIDTH개만 유지
        new_beams.sort(key=lambda b: b.search_score(), reverse=True)
        beams = new_beams[:BEAM_WIDTH]

        if not any_expanded:
            break  # 아무도 확장 못 했으면 더 반복해도 의미 없음

    return beams


def calc_move_eff(total_travel_min, avail_min):
    """5.5-MoveEff. 이동비율 35% 이상이면 0점(해당 코스 원천 제외 대상)."""
    if avail_min <= 0:
        return 0.0
    move_ratio = total_travel_min / avail_min
    raw = (0.35 - move_ratio) / 0.20
    return 100 * max(0, min(1, raw))


def calc_slack_score(total_used_min, avail_min, mode):
    """5.5-SlackScore."""
    target_slack = TARGET_SLACK[mode]
    slack_ratio = max(avail_min - total_used_min, 0) / avail_min if avail_min > 0 else 0
    return 100 * min(slack_ratio / target_slack, 1.0)


def calc_macro_score(course: PartialCourse, avail_min: float, mode: str) -> dict:
    """5.5~5.6 완성 코스 하나의 Macro 점수를 전부 계산해서 반환."""
    n = len(course.items)
    if n == 0:
        return {"final_score": -1, "move_eff": 0, "slack_score": 0, "avg_pref": 0, "avg_qual": 0}

    avg_pref = sum(item["micro_score"] for item in course.items) / n * 100  # 근사치 (엄밀히는 Pref_k 별도 누적 필요)
    total_travel = sum(item["travel_min"] for item in course.items)
    total_stay = sum(item["stay_min"] for item in course.items)
    total_used = total_travel + total_stay

    move_eff = calc_move_eff(total_travel, avail_min)
    slack_score = calc_slack_score(total_used, avail_min, mode)

    # 이동비율 35% 이상이면 해당 코스 자체를 원천 제외 (표 비고란 규칙)
    if move_eff == 0 and (total_travel / avail_min if avail_min > 0 else 1) >= 0.35:
        return {"final_score": -1, "move_eff": 0, "slack_score": slack_score,
                "avg_pref": avg_pref, "avg_qual": 0}

    avg_qual = sum(item.get("adjusted_qual", 0.5) for item in course.items) / n * 100 \
        if any("adjusted_qual" in item for item in course.items) else 50.0

    w = MACRO_WEIGHTS[mode]
    final_score = (
        w["pref"] * avg_pref + w["qual"] * avg_qual
        + w["move_eff"] * move_eff + w["slack"] * slack_score
    )

    return {
        "final_score": final_score, "move_eff": move_eff, "slack_score": slack_score,
        "avg_pref": avg_pref, "avg_qual": avg_qual,
    }


def select_best_course(courses: list[PartialCourse], avail_hours: float, mode: str) -> PartialCourse | None:
    """5.6 — 완성된 코스 후보들 중 FinalScore 최고인 1개를 선택."""
    avail_min = avail_hours * 60
    best_course, best_score = None, float("-inf")

    for course in courses:
        result = calc_macro_score(course, avail_min, mode)
        if result["final_score"] > best_score:
            best_score = result["final_score"]
            best_course = course

    return best_course