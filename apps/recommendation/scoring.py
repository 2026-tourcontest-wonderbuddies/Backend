"""
Pipeline 5.2~5.3 — Micro 평가 (단일 장소 스코어링).

핵심 값 3가지를 계산한다:
  Pref_k          : 취향(선호도) 점수 — 사용자 목적 태그와 장소 태그의 매칭도
  AdjustedQual_k  : 보정 품질 점수 — PlaceStayStat.satisfaction_score를 그대로 씀
  CostMove_R,k    : 이동 비용 — 이동시간을 30분=1.0 기준으로 환산

이 세 값으로 코스 모드(dist/pref/relax)별 Micro 점수를 산출한다.
동적 가중치 스와핑(4:6 전환)은 시간 여유 있을 때 넣는 stretch 기능이라
스위치로 켜고 끌 수 있게 만들어둔다 (기본 off).
"""

from apps.places.models import PlaceTagScore, PlaceStayStat


# 사용자 목적 6종 → PlaceTagScore 필드 매핑 키 (TripRequest.PURPOSE_CHOICES와 동일)
PURPOSE_KEYS = ["nature", "food", "photo", "culture", "activity", "shopping"]

MAIN_PURPOSE_THRESHOLD = 0.40   # 주목적 과락 기준 (5.2-① 방어로직 1번)
SYNERGY_BONUS_WEIGHT = 0.20     # 시너지 보너스 계수
DYNAMIC_SWAP_TRIGGER = 0.80     # 직전 장소 주목적 점수가 이 이상이면 가중치 스와핑 (기본 off)


def get_purpose_match(tag_score: PlaceTagScore | None, purpose_key: str) -> float:
    """
    장소의 목적 태그 점수(0~100)를 0~1로 정규화해서 반환.
    tag_score가 없으면(LLM 태깅 전) 0.5(중립)로 처리 — 완전히 0점 주면
    태깅 안 된 장소가 전부 탈락하게 되어 후보 풀이 텅 비는 걸 방지.
    """
    if tag_score is None:
        return 0.5
    raw = tag_score.get_score(purpose_key)  # 0~100
    return raw / 100.0


def calc_pref(
    tag_score: PlaceTagScore | None,
    purpose_main: str,
    purpose_sub: str | None,
    nlp_match_score: float | None = None,
    use_dynamic_swap: bool = False,
    prev_main_match: float | None = None,
) -> float:
    """
    Pipeline 5.2-① Pref_k 계산.

    Args:
        purpose_main / purpose_sub: TripRequest의 목적 선택값 (예: "nature", "food")
        nlp_match_score: 자유입력 SBERT 유사도 (0~1). 없으면 자유입력 없는 것으로 처리.
        use_dynamic_swap: 동적 가중치 스와핑 기능 on/off (기본 off, stretch 기능)
        prev_main_match: 직전 장소의 주목적 매칭 점수 (스와핑 트리거 판정용)

    Returns:
        0.0 ~ 1.0 사이의 최종 Pref_k. 주목적 과락이면 0.0 반환.
    """
    match_main = get_purpose_match(tag_score, purpose_main)

    # 주목적 과락 — 이 이하면 다른 계산 없이 바로 0점 처리
    if match_main < MAIN_PURPOSE_THRESHOLD:
        return 0.0

    match_sub = get_purpose_match(tag_score, purpose_sub) if purpose_sub else 0.0

    # 가중치 결정: 기본 6:4, 동적 스와핑 조건 충족 시 4:6
    w_main, w_sub = 0.6, 0.4
    if use_dynamic_swap and prev_main_match is not None and prev_main_match >= DYNAMIC_SWAP_TRIGGER:
        w_main, w_sub = 0.4, 0.6

    if nlp_match_score is not None:
        # 자유입력 있는 경우: 주목적 0.4 + 보조목적 0.3 + NLP매칭 0.3
        pref_base = 0.4 * match_main + 0.3 * match_sub + 0.3 * nlp_match_score
    else:
        pref_base = w_main * match_main + w_sub * match_sub

    # 시너지 보너스 (주+보조 둘 다 높으면 가산, Clipping으로 1.0 초과 방지)
    synergy = (match_main * match_sub) * SYNERGY_BONUS_WEIGHT
    pref_raw = pref_base + synergy

    return min(pref_raw, 1.0)


def get_adjusted_qual(stay_stat: PlaceStayStat | None) -> float:
    """
    Pipeline 5.2-② AdjustedQual_k.
    이미 팀원이 satisfaction_score(=AdjustedQual_k)로 계산해서 CSV에 넣어줬으므로
    여기서 다시 계산하지 않고 그대로 가져다 쓴다.
    stay_stat이 없거나 satisfaction_score가 null이면(관측 0건) 중립값 0.5 사용.
    """
    if stay_stat is None or stay_stat.satisfaction_score is None:
        return 0.5
    return stay_stat.satisfaction_score


def calc_cost_move(travel_min: float) -> float:
    """
    Pipeline 5.2-③ CostMove_R,k = TravelTime / 30 (30분을 비용 1.0으로 환산).
    60분 초과 컷은 filters.py의 filter_candidates에서 이미 처리했으므로
    여기서는 순수 계산만 한다.
    """
    return travel_min / 30.0


def calc_micro_score(
    mode: str,
    pref: float,
    adjusted_qual: float,
    cost_move: float,
    travel_min: float,
    stay_min: float,
    remain_time_min: float,
) -> float:
    """
    Pipeline 5.3 — 코스 모드별 Micro 점수.

    Args:
        mode: "dist"(동선효율) | "pref"(취향맞춤) | "relax"(여유여행)
    """
    if mode == "dist":
        # 이동시간 제곱 페널티 — 근거리 장소를 강하게 선호
        return 0.2 * pref + 0.3 * adjusted_qual - 0.5 * (cost_move ** 2)

    elif mode == "pref":
        # 취향·품질 우선, 이동 부담은 약하게만 반영
        return 0.5 * pref + 0.4 * adjusted_qual - 0.1 * cost_move

    elif mode == "relax":
        # 이동+체류가 잔여시간에서 차지하는 비중이 크면 감점
        time_ratio = (travel_min + stay_min) / remain_time_min if remain_time_min > 0 else 1.0
        return 0.3 * pref + 0.4 * adjusted_qual - 0.3 * time_ratio

    raise ValueError(f"알 수 없는 코스 모드: {mode}")


def score_candidate(
    candidate: dict,       # filters.filter_candidates()가 반환한 {"place":, "travel_min":, "stay_min":} 딕셔너리
    mode: str,
    purpose_main: str,
    purpose_sub: str | None,
    remain_time_min: float,
    nlp_match_score: float | None = None,
) -> dict:
    """
    filters.py 출력 하나를 받아서 Micro 점수까지 계산해 붙여주는 통합 함수.
    course_builder.py에서 후보 리스트를 map 돌리듯 이 함수 하나만 호출하면 된다.

    Returns:
        candidate 딕셔너리에 pref/adjusted_qual/cost_move/micro_score를 추가해서 반환.
        Micro 점수가 낮은 순으로 정렬하기 쉽게 그대로 리스트 원소로 쓸 수 있다.
    """
    place = candidate["place"]
    travel_min = candidate["travel_min"]
    stay_min = candidate["stay_min"]

    tag_score = getattr(place, "placetagscore", None)   # OneToOne 역참조, 없으면 None
    stay_stat = getattr(place, "stay_stat", None)

    pref = calc_pref(tag_score, purpose_main, purpose_sub, nlp_match_score)
    adjusted_qual = get_adjusted_qual(stay_stat)
    cost_move = calc_cost_move(travel_min)

    micro_score = calc_micro_score(
        mode, pref, adjusted_qual, cost_move, travel_min, stay_min, remain_time_min
    )

    return {
        **candidate,
        "pref": pref,
        "adjusted_qual": adjusted_qual,
        "cost_move": cost_move,
        "micro_score": micro_score,
    }


# ── 최소 동작 확인 ──────────────────────────────────────────
if __name__ == "__main__":
    # DB 없이 순수 계산 함수만 확인
    print("주목적 과락 케이스:", calc_pref(None, "nature", "food"))  # tag_score=None → 0.5 매칭 → 통과함

    # cost_move / micro_score 계산 확인
    cm = calc_cost_move(45)  # 45분 이동
    print("CostMove(45분):", cm)  # 기대값 1.5

    score_dist = calc_micro_score("dist", pref=0.8, adjusted_qual=0.7, cost_move=1.5,
                                   travel_min=45, stay_min=60, remain_time_min=300)
    print("동선효율 모드 Micro:", score_dist)