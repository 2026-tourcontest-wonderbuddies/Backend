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
import math
from apps.places.models import Place
from apps.recommendation.scoring import get_purpose_match, get_adjusted_qual, calc_cost_move, calc_micro_score, calc_dwell_time
from apps.recommendation.constraints import estimate_airport_travel_min, floor_to_15min

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
    require_breakfast_suitable: bool = False,
) -> tuple[list[Place], list[str]]:
    """
    §1.1 필수필터 + §1.2 식사제한 + food_tags 매칭.
    require_breakfast_suitable=True면 breakfast_suitable=True인 곳만 통과시킨다(아침 슬롯 전용).
    """
    strict_prefs = [p for p in (food_pref_1, food_pref_2) if p]
    survivors = []

    for place in candidates:
        if quadrant and place.quadrant != quadrant:
            continue
        is_open, _ = is_open_at_fn(place, visit_datetime)
        if not is_open:
            continue
        if not passes_food_restriction(place, food_restriction):
            continue
        if not matches_food_pref_tags(place, strict_prefs):
            continue
        if require_breakfast_suitable and not place.breakfast_suitable:
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
        food_pref_1="", food_pref_2="",
        food_restriction=food_restriction,
        is_open_at_fn=is_open_at_fn,
    )

    strict_ids = {p.content_id for p in strict_candidates}
    relaxed_ids = {p.content_id for p in relaxed_candidates if p.content_id not in strict_ids}

    combined = strict_candidates + [p for p in relaxed_candidates if p.content_id in relaxed_ids]
    return combined, relaxed_ids


def build_morning_meal_candidates(
    all_food_places: list[Place],
    quadrant: str,
    visit_datetime,
    food_pref_1: str,
    food_pref_2: str,
    food_restriction: str,
    is_open_at_fn,
) -> tuple[list[Place], set[str]]:
    """
    ★ 아침 식사 전용 후보 조회. build_meal_candidates()와 같은 2단계 구조를 쓰되,
    두 단계 모두 breakfast_suitable=True는 항상 유지한다(권역·식사제한도 항상 유지) —
    부족할 때만 선호 음식 태그 조건을 뺀다.

    1단계(엄격): 권역+영업중+식사제한+선호음식+breakfast_suitable=True
    2단계(완화, 1단계가 SOFT_FILTER_MIN_MEAL_CANDIDATES 미만이면): 선호음식만 제거

    Returns:
        (최종 후보 리스트, 완화로 추가된 place_id 집합)
    """
    strict_candidates, strict_prefs = filter_food_candidates(
        all_food_places, quadrant, visit_datetime, food_pref_1, food_pref_2,
        food_restriction, is_open_at_fn, require_breakfast_suitable=True,
    )

    meal_capable_strict = [p for p in strict_candidates if is_meal_place(p)]
    if len(meal_capable_strict) >= SOFT_FILTER_MIN_MEAL_CANDIDATES:
        return strict_candidates, set()

    # 완화: 선호태그 조건만 제거 (권역·식사제한·breakfast_suitable=True는 그대로 유지)
    relaxed_candidates, _ = filter_food_candidates(
        all_food_places, quadrant, visit_datetime,
        food_pref_1="", food_pref_2="",
        food_restriction=food_restriction,
        is_open_at_fn=is_open_at_fn, require_breakfast_suitable=True,
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
    candidates: list,        # build_meal_candidates로 필터링된 음식점 후보 리스트
    current_place,                   # 직전 장소 (이동시간 계산 기준점)
    purpose_main: str,
    purpose_sub: str,
    mode: str,                       # "dist" / "pref" / "relax"
    remain_time_min: float,
    get_travel_time_fn,
    relaxed_ids: set = None,
    nlp_scores: dict = None,    # 완화 필터 적용된 place_id 집합 (소프트 필터 감점용)
    transport_mode="car",
    visit_datetime=None,       # 이동시간 혼잡보정 k(t) 조회용 출발 시각
) -> list[dict]:
    """
    calc_purpose_fit()으로 시너지보너스 포함 PurposeFit 계산 ->
    nlp_scores에서 이 장소의 QueryFit조회 -> calc_pref_food()로 최종 결합
    relaxed_ids도 실제로 감점에 반영
    """
    from apps.recommendation.scoring import get_purpose_match, get_adjusted_qual, calc_cost_move, calc_micro_score, calc_dwell_time
    from apps.recommendation.constraints import floor_to_15min

    relaxed_ids = relaxed_ids or set()
    nlp_scores = nlp_scores or {}

    scored = []
    for place in candidates:
        if current_place is not None:
            travel_result = get_travel_time_fn(current_place.content_id, place.content_id, depart_at=visit_datetime)
            travel_min = travel_result["duration_min_adjusted"]
        else:
            travel_min = estimate_airport_travel_min(place.latitude, place.longitude, transport_mode)

        # PurposeFit: 목적 태그 매칭 + 시너지 보너스
        match_main = get_purpose_match(place, purpose_main) * 100
        match_sub = get_purpose_match(place, purpose_sub) * 100 if purpose_sub else 0.0
        purpose_fit = calc_purpose_fit(match_main, match_sub)

        # QueryFit: free_text_input 임베딩 유사도. nlp_matching.py가 만든 0~1 값을 0~100으로 스케일
        raw_nlp = nlp_scores.get(place.content_id)
        query_fit = raw_nlp * 100 if raw_nlp is not None else None

        pref = calc_pref_food(purpose_fit, query_fit)

        stay_min = calc_dwell_time(mode, pref, place)
        if remain_time_min > 0:
            available = max(remain_time_min - travel_min, 0)
            if stay_min > available:
                stay_min = floor_to_15min(available)

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
            "is_relaxed": place.content_id in relaxed_ids,
        })

    # Micro 점수 내림차순 정렬
    scored.sort(key=lambda x: x["micro_score"], reverse=True)
    return scored


def decide_food_slot_types(is_main: bool, general_target: int, cafe_balance: str) -> list[str]:
    """
    식당/카페가 목적(주 또는 보조)으로 선택됐을 때, 관광 슬롯(general_target) 안에서
    몇 곳을 식당/카페 "추가 슬롯"으로 채울지 + 무슨 타입으로 채울지 결정한다.

    개수는 general_target(그날 관광용 슬롯 수, 식사 슬롯 제외)에 비례해서 정한다 —
    시간이 짧아지면 자동으로 개수도 줄어들게(고정 개수 하드코딩 X).
    주목적이면 항상 절반 이상(ceil), 보조목적이면 항상 그보다 적게(약 30%, floor) —
    "주목적이면 식당/카페가 일반 장소보다 많거나 같아야 한다"는 원칙을 항상 만족시킴.
    아침/점심/저녁 고정 식사 슬롯(RESTAURANT/SNACK)은 이 함수가 아니라 기존
    meal_segments 로직이 별도로 채우므로 여기서는 다루지 않는다.
    """
    if general_target <= 0:
        return []

    extra_count = math.ceil(general_target / 2) if is_main else int(general_target * 0.3)
    extra_count = min(extra_count, general_target)
    if extra_count <= 0:
        return []

    if cafe_balance == "음식점중심":
        pattern = ["RESTAURANT", "SNACK"]
    elif cafe_balance == "카페중심":
        pattern = ["CAFE"]
    else:
        pattern = ["CAFE", "RESTAURANT"]

    return [pattern[i % len(pattern)] for i in range(extra_count)]


# ── 최소 동작 확인 ──────────────────────────────────────────
if __name__ == "__main__":
    print(calc_purpose_fit(80, 80))  # 92.8
    print(decide_food_slot_types(True, 5, "둘다"))
    print(decide_food_slot_types(False, 5, "둘다"))