"""
음식점/카페 전용 스코어링 (문서 "음식점 로직" 섹션).
일반 장소(scoring.py)와 별도 모듈로 분리한 이유:
- 후보 필터링 기준이 다름 (희망권역, 식사제한 키워드 필터가 추가됨)
- Pref 계산식이 PurposeFit/QueryFit 조합으로 다름 (목적 태그 매칭이 아님)
- 슬롯 타입(RESTAURANT/CAFE/SNACK)별로 후보를 분리해서 다뤄야 함
"""

from apps.places.models import Place


# 식사 제한 키워드 (음식점 로직 §1.2)
RESTRICTION_KEYWORDS = {
    "육류제외": ["흑돼지", "돼지", "삼겹살", "오겹살", "소", "닭", "오리"],
    "해산물제외": ["생선", "회", "조개", "새우", "전복", "생굴"],
}

# 먹고싶은 음식 선택값 → 검색 키워드 매핑 (§2 선호 음식 매칭 표)
FOOD_PREF_KEYWORDS = {
    "고기구이": ["흑돼지", "오겹살", "갈비", "구이"],
    "해산물회초밥": ["생선", "회", "초밥", "전복", "갈치", "해물"],
    "면요리": ["국수", "라멘", "우동", "짬뽕", "파스타"],
}
# 소분류로 바로 매칭 가능한 선택값들
FOOD_PREF_SMALL_CATEGORY = {
    "일식": "일식", "중식": "중식", "양식": "서양식",
    "분식간편식": "김밥 분식", "세계음식퓨전": ["기타외국식", "퓨전음식"],
}


def _search_text(place: Place) -> str:
    """식사제한/선호음식 키워드 검색 대상 텍스트를 합친다."""
    return " ".join([
        place.overview or "", place.featured_menu or "", place.menu or "",
    ])


def passes_food_restriction(place: Place, restriction: str) -> bool:
    """
    §1.2 식사 제한 처리.
    '비건'은 포함 여부(있어야 통과), 육류/해산물 제외는 배제 키워드(있으면 탈락)로 반대 방향이라
    분기 처리한다.
    """
    text = _search_text(place)

    if not restriction or restriction == "없음":
        return True

    if restriction == "비건":
        return "비건" in text

    if restriction in RESTRICTION_KEYWORDS:
        banned = RESTRICTION_KEYWORDS[restriction]
        return not any(kw in text for kw in banned)

    # "알레르기·기타 직접입력"은 자유 텍스트라 여기서 처리하지 않고 통과시킴
    # (자유입력 QueryFit 단계에서 유사도로 자연스럽게 걸러지길 기대)
    return True


def matches_food_pref(place: Place, pref: str) -> bool:
    """§2 선호 음식 매칭. 소분류 직접 매칭 또는 키워드 매칭 중 하나라도 맞으면 True."""
    if not pref or pref == "상관없음":
        return True

    if pref == "제주향토음식":
        # 향토음식은 소분류 지정이 없으므로 overview/메뉴 텍스트만 본다.
        # 구체적 키워드가 문서에 명시 안 돼 있어 임의로 대표 키워드를 넣어둠 — 필요시 보강.
        return any(kw in _search_text(place) for kw in ["향토", "제주", "흑돼지", "몸국", "고기국수"])

    small_cat_target = FOOD_PREF_SMALL_CATEGORY.get(pref)
    if small_cat_target:
        targets = small_cat_target if isinstance(small_cat_target, list) else [small_cat_target]
        if place.small_category_name in targets:
            return True

    keywords = FOOD_PREF_KEYWORDS.get(pref, [])
    if keywords and any(kw in _search_text(place) for kw in keywords):
        return True

    return False


def filter_food_candidates(
    candidates: list[Place],
    region_signgu_code: str,
    visit_datetime,
    food_pref_1: str,
    food_pref_2: str,
    food_restriction: str,
    is_open_at_fn,   # filters.is_open_at 재사용
) -> list[Place]:
    """
    §1.1 필수 필터 + §1.2 식사제한 처리.
    최대 2개 선호 음식 중 하나만 일치해도 통과 (§2 "최대 2개 중 하나만 일치해도 후보 포함").
    """
    survivors = []
    for place in candidates:
        if place.signgu_code != region_signgu_code:
            continue
        if not is_open_at_fn(place, visit_datetime):
            continue
        if not passes_food_restriction(place, food_restriction):
            continue

        prefs = [p for p in (food_pref_1, food_pref_2) if p]
        if prefs and not any(matches_food_pref(place, p) for p in prefs):
            continue

        survivors.append(place)
    return survivors


def calc_purpose_fit(main_score: float, sub_score: float) -> float:
    """
    §4 PurposeFit. main_score/sub_score는 0~100 스케일 (문서 예시와 동일하게 유지).
    """
    base = main_score * 0.6 + sub_score * 0.4
    synergy = (main_score * sub_score / 100) * 0.2
    return min(base + synergy, 100)


def calc_pref_food(
    purpose_fit: float,          # 0~100
    query_fit: float | None,     # 0~100, 자유입력 없으면 None
) -> float:
    """
    §4 Pref_food. 자유입력 유무에 따라 가중치가 달라짐.
    반환값은 scoring.py의 Pref_k와 스케일을 맞추기 위해 0~1로 정규화해서 돌려준다
    (문서는 0~100 스케일이지만, 기존 Micro 수식이 0~1 기준이라 여기서 변환).
    """
    if query_fit is None:
        pref_food = purpose_fit
    else:
        pref_food = purpose_fit * 0.5 + query_fit * 0.5
    return pref_food / 100.0


def decide_food_slot_types(
    purpose_selected: bool,       # 사용자가 목적으로 '음식/카페'를 선택했는지
    need_lunch: bool,
    need_dinner: bool,
    avail_hours: float,
    cafe_balance: str,            # "음식점중심"/"카페중심"/"둘다"/""
) -> list[str]:
    """
    §음식점 슬롯 구성 표 그대로 구현.
    Returns: 이번 날에 배치해야 할 슬롯 타입 리스트, 예: ["RESTAURANT","RESTAURANT","CAFE"]
    """
    slots = []

    # 식사시간 기반 필수 RESTAURANT
    if need_lunch:
        slots.append("RESTAURANT")
    if need_dinner:
        slots.append("RESTAURANT")

    if not purpose_selected:
        return slots  # 목적 선택 안 했으면 식사 슬롯만, 추가 없음

    # 추가 음식 장소 수 (코스 생성 시간 기준, §5-1)
    if avail_hours < 6:
        extra_count = 1
    else:
        extra_count = 2  # 6시간 이상 및 12시간 코스 모두 2곳

    if cafe_balance == "음식점중심":
        extra_types = ["RESTAURANT", "SNACK"][:extra_count]
    elif cafe_balance == "카페중심":
        extra_types = ["CAFE"] * extra_count
    else:  # "둘다" 또는 미지정
        # 현재 슬롯에서 RESTAURANT/CAFE 중 적은 유형 우선 — 여기선 아직 배치 전이므로
        # 단순 교대 배치로 근사 (실제 갱신은 course_builder에서 실시간 카운트로 보정)
        pattern = ["RESTAURANT", "CAFE"]
        extra_types = [pattern[i % 2] for i in range(extra_count)]

    slots.extend(extra_types)
    return slots


# ── 최소 동작 확인 ──────────────────────────────────────────
if __name__ == "__main__":
    print(calc_purpose_fit(100, 100))  # 100 (클리핑)
    print(calc_purpose_fit(80, 80))    # 92.8
    print(calc_pref_food(purpose_fit=92.8, query_fit=None))  # 0.928
    print(decide_food_slot_types(True, True, True, 12.0, "둘다"))
    # 기대값: ['RESTAURANT','RESTAURANT','RESTAURANT','CAFE'] (점심+저녁 필수 2개 + 추가 2개)