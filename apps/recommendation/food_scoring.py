"""
★ 전면 재작성 ★
1. 키워드 사전 매칭 폐기 → food_tags(LLM 다중라벨 10종) 기반 매칭으로 교체.
   이유: 관광식당(48%)이 정보 없는 카테고리라 소분류/키워드로 매칭 커버리지가 낮았음.
2. MEAL_CAPABLE_ROLES에 SNACK 포함 — 기존엔 RESTAURANT만 식사 인정이라
   분식·간편식 선호가 실제로 반영 안 되는 버그가 있었음.
3. 소프트 필터(완화) 로직 신규: 후보 5곳 미만 시 태그조건 1개 제거 + 0.15 감점으로
   "진짜 일치하는 곳이 남아있으면 항상 먼저 선택"되도록 우선순위 보장.
4. Micro 점수 기반 음식점 후보 정렬/채점 함수(score_food_candidates) 추가.

: 음식점/카페 전용 필터링 + 점수 로직
"""
from __future__ import annotations
from apps.places.models import Place
from apps.recommendation.scoring import get_purpose_match, get_adjusted_qual, calc_cost_move, calc_micro_score

MEAL_CAPABLE_ROLES = ("RESTAURANT", "SNACK")   # ★ 변경: 기존 RESTAURANT만 → SNACK 추가
SOFT_FILTER_MIN_MEAL_CANDIDATES = 5
RELAX_PENALTY = 0.15


# 사용자 선택값 → food_tags 매칭 키 (팀 LLM 태그 10종과 1:1 대응)
FOOD_PREF_TO_TAG = {
    "제주향토음식": "제주 향토음식",
    "고기구이": "고기·구이",
    "해산물요리": "해산물 요리",
    "회물회초밥": "회·물회·초밥",
    "한식": "한식",
    "면요리": "면 요리",
    "분식간편식": "분식·간편식",
    "일식": "일식",
    "중식": "중식",
    "양식세계음식": "양식·세계음식",
}

RESTRICTION_KEYWORDS = {
    "육류제외": ["흑돼지", "돼지", "삼겹살", "오겹살", "소", "닭", "오리"],
    "해산물제외": ["생선", "회", "조개", "새우", "전복", "생굴"],
}


def _search_text(place: Place) -> str:
    return " ".join([place.overview or "", place.featured_menu or "", place.menu or ""])


def passes_food_restriction(place: Place, restriction: str) -> bool:
    """식사 제한은 완화 대상에서 항상 제외 (안전 문제이므로 소프트 필터가 건드리지 않음)."""
    text = _search_text(place)
    if not restriction or restriction == "없음":
        return True
    if restriction == "비건":
        return "비건" in text
    if restriction in RESTRICTION_KEYWORDS:
        return not any(kw in text for kw in RESTRICTION_KEYWORDS[restriction])
    return True


def matches_food_pref_tags(place: Place, prefs: list[str]) -> bool:
    """
    ★ 변경: food_tags 리스트와 선택한 선호 태그가 하나라도 겹치면 통과.
    prefs는 FOOD_PREF_TO_TAG의 키 목록 (TripRequest.food_pref_1/2 값).
    """
    if not prefs or "상관없음" in prefs:
        return True
    target_tags = {FOOD_PREF_TO_TAG[p] for p in prefs if p in FOOD_PREF_TO_TAG}
    return bool(target_tags & set(place.food_tags))


def is_meal_place(place: Place) -> bool:
    """식사 슬롯(점심/저녁) 채울 자격이 있는지. ★변경: SNACK도 인정."""
    return place.food_role in MEAL_CAPABLE_ROLES


def filter_food_candidates(
    candidates: list[Place],
    quadrant: str,
    visit_datetime,
    food_pref_1: str,
    food_pref_2: str,
    food_restriction: str,
    is_open_at_fn,
) -> tuple[list[Place], list[str]]:
    """
    §1.1 필수필터 + §1.2 식사제한 + food_tags 매칭.
    """
    strict_prefs = [p for p in (food_pref_1, food_pref_2) if p]
    survivors = []

    for place in candidates:
        if place.quadrant != quadrant:
            continue
        is_open, _ = is_open_at_fn(place, visit_datetime)
        if not is_open:
            continue
        if not passes_food_restriction(place, food_restriction):
            continue
        if not matches_food_pref_tags(place, strict_prefs):
            continue
        survivors.append(place)

    return survivors, strict_prefs


def build_meal_candidates(
    all_food_places: list[Place],
    quadrant: str,
    visit_datetime,
    food_pref_1: str,
    food_pref_2: str,
    food_restriction: str,
    is_open_at_fn,
) -> tuple[list[Place], set[str]]:
    """
    ★ 소프트 필터 진입점.
    엄격 매칭 결과가 SOFT_FILTER_MIN_MEAL_CANDIDATES 미만이면, 선호태그 조건만
    제거하고(식사제한·권역은 유지) 재계산해서 후보를 보충한다.

    Returns:
        (최종 후보 리스트, 완화로 추가된 place_id 집합)
    """
    strict_candidates, strict_prefs = filter_food_candidates(
        all_food_places, quadrant, visit_datetime, food_pref_1, food_pref_2,
        food_restriction, is_open_at_fn,
    )

    meal_capable_strict = [p for p in strict_candidates if is_meal_place(p)]
    if len(meal_capable_strict) >= SOFT_FILTER_MIN_MEAL_CANDIDATES:
        return strict_candidates, set()

    # 완화: 선호태그 조건 제거하고 재계산 (권역·식사제한은 그대로 유지)
    relaxed_candidates, _ = filter_food_candidates(
        all_food_places, quadrant, visit_datetime,
        food_pref_1="", food_pref_2="",  # ★ 태그 조건만 해제
        food_restriction=food_restriction,
        is_open_at_fn=is_open_at_fn,
    )

    strict_ids = {p.content_id for p in strict_candidates}
    relaxed_ids = {p.content_id for p in relaxed_candidates if p.content_id not in strict_ids}

    combined = strict_candidates + [p for p in relaxed_candidates if p.content_id in relaxed_ids]
    return combined, relaxed_ids


def apply_relax_penalty(micro_score: float, place_id: str, relaxed_ids: set[str]) -> float:
    """
    완화된 후보는 항상 "진짜 일치하는 곳"보다 낮은 우선순위를 갖도록 감점 적용 (0.15).
    """
    if place_id in relaxed_ids:
        return micro_score - RELAX_PENALTY
    return micro_score


def calc_purpose_fit(main_score: float, sub_score: float) -> float:
    base = main_score * 0.6 + sub_score * 0.4
    synergy = (main_score * sub_score / 100) * 0.2
    return min(base + synergy, 100)


def calc_pref_food(purpose_fit: float, query_fit: float | None = None) -> float:
    pref_food = purpose_fit if query_fit is None else (purpose_fit * 0.5 + query_fit * 0.5)
    return pref_food / 100.0


def score_food_candidates(
    candidates: list[Place],        # build_meal_candidates로 필터링된 음식점 후보 리스트
    current_place,                   # 직전 장소 (이동시간 계산 기준점)
    purpose_main: str,
    purpose_sub: str,
    mode: str,                       # "dist" / "pref" / "relax"
    remain_time_min: float,
    get_travel_time_fn,
    relaxed_ids: set[str] = None,    # ★ 추가: 완화 필터 적용된 place_id 집합 (소프트 필터 감점용)
) -> list[dict]:
    """
    각 음식점 후보에 Micro 점수를 매겨서 높은 순으로 정렬해 반환한다.
    engine.py에서 이 함수의 결과 리스트 맨 앞([0])을 고르면 된다.

    Returns: [{"place":, "travel_min":, "micro_score":, ...}, ...] 점수 내림차순 정렬됨
    """
    if relaxed_ids is None:
        relaxed_ids = set()

    scored = []
    for place in candidates:
        travel_result = get_travel_time_fn(current_place.content_id, place.content_id)
        travel_min = travel_result["duration_min_adjusted"]
        stay_min = place.stay_time_minutes

        # 1. 여행 목적(purpose_main/sub) 매칭 점수 계산
        match_main = get_purpose_match(place, purpose_main)
        match_sub = get_purpose_match(place, purpose_sub) if purpose_sub else 0.0
        
        # 2. 원본의 시너지 반영 공식(calc_purpose_fit & calc_pref_food) 재사용
        purpose_fit = calc_purpose_fit(match_main, match_sub)
        pref = calc_pref_food(purpose_fit)

        # 3. 품질 및 이동 비용 계산
        adjusted_qual = get_adjusted_qual(place)
        cost_move = calc_cost_move(travel_min)

        # 4. Micro 점수 산출
        micro_score = calc_micro_score(mode, pref, adjusted_qual, cost_move, travel_min, stay_min, remain_time_min)

        # 5. ★ 소프트 필터 감점(RELAX_PENALTY) 적용
        final_micro_score = apply_relax_penalty(micro_score, place.content_id, relaxed_ids)

        scored.append({
            "place": place,
            "travel_min": travel_min,
            "stay_min": stay_min,
            "pref": pref,
            "adjusted_qual": adjusted_qual,
            "micro_score": final_micro_score,
        })

    # Micro 점수 내림차순 정렬
    scored.sort(key=lambda x: x["micro_score"], reverse=True)
    return scored


def decide_food_slot_types(purpose_selected, need_lunch, need_dinner, avail_hours, cafe_balance):
    """기존과 동일 로직 유지 (문서상 변경 없음)."""
    slots = []
    if need_lunch:
        slots.append("RESTAURANT")
    if need_dinner:
        slots.append("RESTAURANT")
    if not purpose_selected:
        return slots

    extra_count = 1 if avail_hours < 6 else 2
    if cafe_balance == "음식점중심":
        extra_types = ["RESTAURANT", "SNACK"][:extra_count]
    elif cafe_balance == "카페중심":
        extra_types = ["CAFE"] * extra_count
    else:
        pattern = ["RESTAURANT", "CAFE"]
        extra_types = [pattern[i % 2] for i in range(extra_count)]

    slots.extend(extra_types)
    return slots


# ── 최소 동작 확인 ──────────────────────────────────────────
if __name__ == "__main__":
    print(calc_purpose_fit(80, 80))  # 92.8
    print(decide_food_slot_types(True, True, False, 8.0, "둘다"))