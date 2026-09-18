"""
Pipeline 5.2~5.3 — Micro 평가 (단일 장소 스코어링).
: filters.py에서 걸러진 후보들에 점수 매기기(취향 매칭, 품질, 이동비용)

핵심 값 3가지를 계산한다:
  Pref_k          : 취향(선호도) 점수 — 사용자 목적 태그와 장소 태그의 매칭도
  AdjustedQual_k  : 보정 품질 점수 — PlaceStayStat.satisfaction_score를 그대로 씀
  CostMove_R,k    : 이동 비용 — 이동시간을 30분=1.0 기준으로 환산

이 세 값으로 코스 모드(dist/pref/relax)별 Micro 점수를 산출한다.
동적 가중치 스와핑(4:6 전환)은 시간 여유 있을 때 넣는 stretch 기능이라
스위치로 켜고 끌 수 있게 만들어둔다 (기본 off).
"""
from __future__ import annotations
import math

from apps.recommendation.constraints import snap_to_15min, floor_to_15min

MAIN_PURPOSE_THRESHOLD = 0.40
SYNERGY_BONUS_WEIGHT = 0.20
DYNAMIC_SWAP_TRIGGER = 0.80


def get_purpose_match(place, purpose_key: str) -> float:
    """
    장소의 목적 태그 점수(0~100)를 0~1로 정규화해서 반환.
    tag_score가 없으면(LLM 태깅 전) 0.5(중립)로 처리 — 완전히 0점 주면
    태깅 안 된 장소가 전부 탈락하게 되어 후보 풀이 텅 비는 걸 방지.
    """
    return place.get_purpose_score(purpose_key) / 100.0


def calc_pref(place, purpose_main, purpose_sub=None, nlp_match_score=None,
              use_dynamic_swap=False, prev_main_match=None) -> float:
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
    match_main = get_purpose_match(place, purpose_main)

    # 주목적 과락 — 이 이하면 다른 계산 없이 바로 0점 처리
    if match_main < MAIN_PURPOSE_THRESHOLD:
        return 0.0

    match_sub = get_purpose_match(place, purpose_sub) if purpose_sub else 0.0

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


def get_adjusted_qual(place) -> float:
    """
    Pipeline 5.2-② AdjustedQual_k.
    이미 팀원이 satisfaction_score(=AdjustedQual_k)로 계산해서 CSV에 넣어줬으므로
    여기서 다시 계산하지 않고 그대로 가져다 쓴다.
    stay_stat이 없거나 satisfaction_score가 null이면(관측 0건) 중립값 0.5 사용.
    """
    if place.satisfaction_score is None:
        return 0.5
    return place.satisfaction_score


def calc_dwell_time(mode: str, pref: float, place) -> float:
    stay_time = place.stay_time_minutes or 0
    stay_max = place.stay_max

    if mode == "dist":
        return stay_time
    if mode == "relax":
        base = stay_max if stay_max is not None else stay_time
        return base + 30
    if mode == "pref":
        if stay_max is None:
            return stay_time
        return snap_to_15min(stay_time + (stay_max - stay_time) * pref)
    raise ValueError(f"알 수 없는 코스 모드: {mode}")


# 팀 문서(이동엔진/docs/08.03_AI Hub EDA)의 "인기 장소(10회+)" 기준과 동일하게,
# obs_travel_n≈10 이상을 인기 장소로 본다. popularity_score = 1-exp(-0.2*obs_travel_n) 이므로
# obs_travel_n=10 → 0.865. 0.85를 컷오프로 둔다.
POPULAR_THRESHOLD = 0.85

# ponytail: TourAPI 카테고리로는 진짜 명소(예: 오설록 티 뮤지엄)와 전국 어디에나
# 있는 체인 매장이 같은 소분류("사후면세점" 등)로 묶여 있어 카테고리로 못 거른다.
# obs_travel_n이 우연히 높게 잡힌 일부 전국구 체인만 이름으로 콕 집어 제외.
# 새 체인이 인기 목록에서 눈에 띄면 여기 이름만 추가하면 됨(카테고리 재설계는 오버킬).
CHAIN_RETAIL_DENYLIST = (
    "이마트", "다이소", "올리브영", "탑텐", "GS25", "CU", "세븐일레븐",
    "스타벅스", "투썸플레이스", "이디야", "파리바게뜨", "뚜레쥬르", "이니스프리",
)


def is_popular_place(place) -> bool:
    """이 장소가 코스 내 '인기 장소' 쿼터에 해당하는지 여부. popularity_score가 없으면 False."""
    if place.popularity_score is None or place.popularity_score < POPULAR_THRESHOLD:
        return False
    return not any(chain in place.title for chain in CHAIN_RETAIL_DENYLIST)


def calc_cost_move(travel_min: float, mode: str = "dist", pref: float = 0.0) -> float:
    """
    이동시간 페널티.
    dist: 선형
    pref/relax: 로그 기반 완만한 곡선 + pref 점수가 높을수록 페널티 추가 완화
    """
    if mode == "dist":
        FREE_THRESHOLD = 30
        if travel_min <= FREE_THRESHOLD:
            return (travel_min / FREE_THRESHOLD) * 0.3
        return (travel_min - FREE_THRESHOLD) / 30.0


    if travel_min <= 0:
        return 0.0

    # 로그 곡선: 5분 -> 작은값, 30분 -> 0.5 근처, 60분 -> 1.0 근처로 완만하게 수렴
    # log(1 + t/10) 형태로 초반엔 완만, 갈수록 체감 증가폭 감소
    base_cost = math.log(1 + travel_min / 10) / math.log(1 + 60 / 10)

    if mode == "pref":
        damping = 1 - (0.7 * pref)
        return base_cost * damping

    # relax는 완만한 로그곡선만 적용
    return base_cost


def calc_micro_score(
    mode, pref, 
    adjusted_qual, 
    cost_move, 
    travel_min, 
    stay_min, 
    remain_time_min
) -> float:
    """
    Pipeline 5.3 — 코스 모드별 Micro 점수.

    Args:
        mode: "dist"(동선효율) | "pref"(취향맞춤) | "relax"(여유여행)
    """
    if mode == "dist":
        return 0.2 * pref + 0.3 * adjusted_qual - 0.3 * cost_move
    elif mode == "pref":
        return 0.5 * pref + 0.4 * adjusted_qual - 0.1 * cost_move
    elif mode == "relax":
        return 0.3 * pref + 0.4 * adjusted_qual - 0.3 * cost_move
    raise ValueError(f"알 수 없는 코스 모드: {mode}")


def score_candidate(
    candidate,      # filters.filter_candidates()가 반환한 {"place":, "travel_min":, "stay_min":} 딕셔너리
    mode,
    purpose_main,
    purpose_sub,
    remain_time_min,
    nlp_match_score=None,
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

    pref = calc_pref(place, purpose_main, purpose_sub, nlp_match_score)

    stay_min = calc_dwell_time(mode, pref, place)
    if remain_time_min > 0:
        available = max(remain_time_min - travel_min, 0)
        if stay_min > available:
            stay_min = floor_to_15min(available)

    adjusted_qual = get_adjusted_qual(place)
    cost_move = calc_cost_move(travel_min, mode, pref)
    micro_score = calc_micro_score(
        mode, pref, adjusted_qual, cost_move, travel_min, stay_min, remain_time_min
    )

    return {
        **candidate,
        "stay_min": stay_min,
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

    # is_popular_place 판정 확인
    dummy = lambda score, title="테스트장소": type("Place", (), {"popularity_score": score, "title": title})()
    assert is_popular_place(dummy(0.9)) is True
    assert is_popular_place(dummy(0.5)) is False
    assert is_popular_place(dummy(None)) is False
    assert is_popular_place(dummy(0.99, "이마트 서귀포점")) is False  # 체인 매장 denylist
    print("is_popular_place 판정: OK")