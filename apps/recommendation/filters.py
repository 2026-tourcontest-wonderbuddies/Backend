"""
후보 장소 1차 필터 (Hard Constraints & Time Filtering)
"""

from datetime import datetime, timedelta


WEEKDAY_MAP = ["월", "화", "수", "목", "금", "토", "일"]


def is_open_at(place, visit_datetime: datetime) -> bool:
    """
    영업시간·휴무일 하드필터 (5.1-① 영업 조건).

    Args:
        place: Place 인스턴스 (open_time, close_time, closed_weekdays 필드 사용)
        visit_datetime: 방문 예정 일시

    Returns:
        영업 중이면 True, 휴무일이거나 영업시간 외면 False

    주의: open_time/close_time이 아직 파싱 안 된 장소(hours_raw만 있고 null)는
    일단 통과시킨다 — 데이터 부족으로 무조건 탈락시키면 후보 풀이 과도하게 줄어든다.
    """
    weekday_str = WEEKDAY_MAP[visit_datetime.weekday()]
    if weekday_str in place.closed_weekdays:
        return False

    # 운영시간 파싱이 안 된 경우(대부분 초기 상태) 통과 처리
    if place.open_time is None or place.close_time is None:
        return True

    visit_time = visit_datetime.time()
    # 자정을 넘기는 영업시간(예: 22:00~02:00)은 별도 처리 필요하나
    # 우선 일반적인 당일 영업시간만 처리 (야간 영업점 예외는 추후 보강)
    if place.close_time >= place.open_time:
        return place.open_time <= visit_time <= place.close_time
    return visit_time >= place.open_time or visit_time <= place.close_time


def is_excluded(place, exclude_place_ids: list[str], exclude_categories: list[str]) -> bool:
    """사용자 제외 조건 (5.1-① 사용자 제외 조건)."""
    if place.content_id in exclude_place_ids:
        return True
    if place.small_category_name in exclude_categories:
        return True
    if place.middle_category_name in exclude_categories:
        return True
    return False


def fails_transport_constraint(place, transport_mode: str) -> bool:
    """
    이동수단 제약. 문서상 '감점/제외'인데, 완전 제외보다는 감점(스코어링 단계에서
    penalty)이 UX상 낫다는 팀 논의(2.1-2 대안)를 반영해 여기서는 하드 제외하지 않고
    스코어링에서 처리하도록 False만 반환하는 placeholder로 둔다.
    렌터카인데 주차 '불가능'이 명시된 경우만 최소한으로 하드 제외한다.
    """
    if transport_mode in ("rental_car", "own_car") and place.parking == "불가능":
        return True
    return False


def check_time_budget(
    travel_min: float,
    stay_min: float,
    remain_time_min: float,
) -> bool:
    """
    5.1-② 가용 시간 충족 조건: TravelTime + StayTime <= RemainTime.

    Returns:
        조건을 만족해서 후보로 유지 가능하면 True
    """
    return (travel_min + stay_min) <= remain_time_min


def filter_candidates(
    current_place,           # 현재 위치의 Place 인스턴스 (None이면 출발지 좌표 사용은 course_builder가 처리)
    candidates: list,        # Place 인스턴스 리스트 (미리 지역 필터링된 후보 풀)
    visit_datetime: datetime,
    remain_time_min: float,
    transport_mode: str,
    exclude_place_ids: list[str],
    exclude_categories: list[str],
    get_travel_time_fn,      # routing_engine.get_travel_time을 감싼 콜러블 (아래 시그니처 참고)
    get_stay_time_fn,        # place.content_id -> stay_min(float)을 반환하는 콜러블
) -> list[dict]:
    """
    Pipeline 5.1 전체를 한 번에 수행하는 진입점.

    get_travel_time_fn(origin_id, destination_id) -> {"duration_min_adjusted": float, ...}
    get_stay_time_fn(place) -> float  (PlaceStayStat.stay_med_15m 조회 래퍼)

    Returns:
        하드필터를 통과한 후보들의 리스트. 각 원소는
        {"place": Place, "travel_min": float, "stay_min": float} 형태.
        이 결과를 그대로 scoring.py의 Micro 평가에 넘긴다.
    """
    survivors = []

    for place in candidates:
        # ① 영업시간·휴무일
        if not is_open_at(place, visit_datetime):
            continue

        # ② 사용자 제외 조건
        if is_excluded(place, exclude_place_ids, exclude_categories):
            continue

        # ③ 이동수단 제약 (주차 불가 등 명확한 케이스만 하드 제외)
        if fails_transport_constraint(place, transport_mode):
            continue

        # ④ 이동시간 계산 (현재 위치가 없으면 0으로 취급 — 코스 첫 장소인 경우)
        if current_place is not None:
            travel_result = get_travel_time_fn(current_place.content_id, place.content_id)
            travel_min = travel_result["duration_min_adjusted"]
        else:
            travel_min = 0.0

        # ⑤ 이동시간 60분 초과 시 사전 컷 (5.2-③ CostMove 주석: 단일 이동 60분 초과는 Micro 평가 전 컷)
        if travel_min > 60:
            continue

        # ⑥ 체류시간 조회
        stay_min = get_stay_time_fn(place)

        # ⑦ 가용시간 충족 여부
        if not check_time_budget(travel_min, stay_min, remain_time_min):
            continue

        survivors.append({
            "place": place,
            "travel_min": travel_min,
            "stay_min": stay_min,
        })

    return survivors


# ── 최소 동작 확인 ──────────────────────────────────────────
if __name__ == "__main__":
    # DB 없이 순수 로직만 확인하고 싶을 때 쓰는 더미 테스트.
    # 실제 Place 인스턴스가 필요한 함수(is_open_at 등)는 Django shell에서
    # 실제 객체로 테스트하는 게 더 정확하니, 여기서는 시간 계산 함수만 확인.
    print(check_time_budget(travel_min=20, stay_min=60, remain_time_min=90))   # False (80>90 아님, 20+60=80<=90 True여야함)
    print(check_time_budget(travel_min=20, stay_min=60, remain_time_min=70))   # False