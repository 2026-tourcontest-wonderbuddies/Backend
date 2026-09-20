"""
후보 장소 1차 필터 (Hard Constraints & Time Filtering)
: 물리적으로 갈 수 없는 곳 필터링(영업시간, 권역, 이동수단 제약, 시간예산)
"""

from __future__ import annotations
from datetime import datetime, timedelta
from apps.recommendation.constraints import estimate_airport_travel_min, JEJU_AIRPORT_PROXY_CONTENT_ID


WEEKDAY_MAP = ["월", "화", "수", "목", "금", "토", "일"]


def is_open_at(place, visit_datetime: datetime) -> tuple[bool, bool]:
    """
    영업시간·휴무일 하드필터 (5.1-① 영업 조건).

    Returns:
        (통과 여부, uncertain 여부)
        - hours_status='always' → (True, False)
        - hours_status='uncertain' → (True, True)  # 확인불가는 제외하지 않고 통과, 대신 표시
        - hours_status='windows' → 실제 요일/시간 비교해서 판정 (False, False)면 하드 제외
    """
    if place.hours_status == "uncertain":
        return True, True

    weekday_str = WEEKDAY_MAP[visit_datetime.weekday()]
    if weekday_str in place.closed_weekdays:
        return False, False

    if place.hours_status == "always":
        return True, False

    # hours_status == "windows"일 경우
    windows = place.open_windows.get(weekday_str)
    if not windows:
        return True, True

    visit_time_str = visit_datetime.strftime("%H:%M")
    for start, end in windows:
        if start <= visit_time_str <= end:
            return True, False

    return False, False


def matches_quadrant(place, region_quadrant: str | None) -> bool:
    if not region_quadrant:
        return True
    return place.quadrant == region_quadrant


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
    return place.parking == "불가능"


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

def check_airport_deadline(
    place,
    arrival_and_stay_end_dt:datetime,
    airport_deadline_dt: datetime,
    matrix_ids: set,
    get_travel_time_fn,
    transport_mode: str,
) -> bool:
    """
    마지막날 전용. 이후보를 방문하고나서 공항까지 갈 시간까지 확보되는지 확인
    제주공항 매트릭스로 실측 이동시간을 조회한다.
    """
    if place.content_id in matrix_ids and JEJU_AIRPORT_PROXY_CONTENT_ID in matrix_ids:
        result = get_travel_time_fn(place.content_id, JEJU_AIRPORT_PROXY_CONTENT_ID, depart_at=arrival_and_stay_end_dt)
        travel_to_airport = result["duration_min_adjusted"]
    else:
        travel_to_airport = estimate_airport_travel_min(place.latitude, place.longitude, transport_mode)

    airport_arrival = arrival_and_stay_end_dt + timedelta(minutes=travel_to_airport)
    return airport_arrival <= airport_deadline_dt


def filter_candidates(
    current_place,           # 현재 위치의 Place 인스턴스 (None이면 출발지 좌표 사용은 course_builder가 처리)
    candidates: list,        # Place 인스턴스 리스트 (미리 지역 필터링된 후보 풀)
    visit_datetime: datetime,
    remain_time_min: float,
    transport_mode: str,
    region_quadrant: str | None,
    exclude_place_ids: list[str],
    exclude_categories: list[str],
    get_travel_time_fn,      # routing_engine.get_travel_time을 감싼 콜러블 (아래 시그니처 참고)
    get_stay_time_fn,        # place.content_id -> stay_min(float)을 반환하는 콜러블
    mode: str = "dist",
    airport_deadline_dt: datetime | None = None, # 마지막날에만 전달됨
    matrix_ids: set | None = None,  # airport_deadline_dt와 함께 필요
) -> list[dict]:
    """
    반환 dict에 "hours_uncertain" 추가 — course_builder가 최종 아이템에 표시 여부 전달용.
    """
    survivors = []

    for place in candidates:
        if not matches_quadrant(place, region_quadrant):
            continue

        is_open, uncertain = is_open_at(place, visit_datetime)
        # 영업시간·휴무일
        if not is_open:
            continue

        # 사용자 제외 조건
        if is_excluded(place, exclude_place_ids, exclude_categories):
            continue

        # 이동수단 제약 (주차 불가 등 명확한 케이스만 하드 제외)
        if fails_transport_constraint(place, transport_mode):
            continue

        # 이동시간 계산 (현재 위치가 없으면 0으로 취급 — 코스 첫 장소인 경우)
        if current_place is not None:
            travel_result = get_travel_time_fn(current_place.content_id, place.content_id, depart_at=visit_datetime)
            travel_min = travel_result["duration_min_adjusted"]
        else:
            travel_min = estimate_airport_travel_min(place.latitude, place.longitude, transport_mode)

        # 이동시간 60분 초과 시 사전 컷
        if mode == "dist":
            limit = 60
            if travel_min > limit:   # ← 여기까지는 dist에서만 문제없음
                continue

        # 체류시간 조회
        stay_min = get_stay_time_fn(place)

        # 가용시간 충족 여부
        if not check_time_budget(travel_min, stay_min, remain_time_min):
            continue

        if airport_deadline_dt is not None:
            arrival_and_stay_end = visit_datetime + timedelta(minutes=travel_min + stay_min)
            if not check_airport_deadline(
                place, arrival_and_stay_end, airport_deadline_dt,
                matrix_ids or set(), get_travel_time_fn, transport_mode,
            ):
                continue 

        survivors.append({
            "place": place,
            "travel_min": travel_min,
            "stay_min": stay_min,
            "hours_uncertain": uncertain,
        })

    return survivors


# 최소 동작 확인 
if __name__ == "__main__":
    # DB 없이 순수 로직만 확인하고 싶을 때 쓰는 더미 테스트.
    # 실제 Place 인스턴스가 필요한 함수(is_open_at 등)는 Django shell에서
    # 실제 객체로 테스트하는 게 더 정확하니, 여기서는 시간 계산 함수만 확인.
    print(check_time_budget(travel_min=20, stay_min=60, remain_time_min=90))   # False (80>90 아님, 20+60=80<=90 True여야함)
    print(check_time_budget(travel_min=20, stay_min=60, remain_time_min=70))   # False