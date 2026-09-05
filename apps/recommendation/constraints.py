"""
Pipeline 3 — 타임라인 분석 및 슬롯 할당.

: 하루 가용시간 계산(도착/출발 시각->실제 활동시간), 몇 곳 방문할지 계산, 권역(4분면) 판정 함수

이 모듈은 Django ORM에 의존하지 않는 '순수 함수'로만 구성한다.
이유: course_builder.py가 완성되기 전에도 더미 값으로 바로 실행·검증할 수 있어야 하고,
      단위테스트를 정식으로 못 짜는 상황이라 파일 하단 __main__ 블록으로
      최소한의 동작 확인을 대신하기 위함.

설계서 근거: [Pipeline 3] 타임라인 분석 및 슬롯 할당.
Baseline은 일차별 표가 아니라 단일 상수 7.0 (팀원 08.14 검증 결과 채택안).
격자는 15분 (팀원 08.15 결정 — 30분이 아니라 15분으로 최종 확정됨, 주의!).
"""
from __future__ import annotations
from datetime import datetime, timedelta
from dataclasses import dataclass


# ── 상수 정의 ────────────────────────────────────────────────
DAY_START_ANCHOR = 9 * 60          # 09:00, 분 단위
DAY_END_ANCHOR = 21 * 60           # 21:00, 분 단위
ARRIVAL_BUFFER_MIN = 30            # 입도일 렌터카 인수 버퍼 (기본값, 임의설정값)
AIRPORT_BUFFER_MIN = 90            # 출도일 공항 수속 버퍼 (기본값, 임의설정값)

BASELINE_SLOTS = 7.0               # 12시간 기준 실측 회귀 상수 (팀원 검증, 일차 무관 단일값)
FULL_DAY_MIN = 12 * 60             # 720분

MODE_MULTIPLIER = {
    "dist": 1.0,   # 이동 최소
    "pref": 0.9,   # 취향 중심
    "relax": 0.7,  # 여유로운 코스
}

TARGET_SLACK = {
    "dist": 0.05,
    "pref": 0.10,
    "relax": 0.20,
}

LUNCH_WINDOW = (11 * 60 + 30, 13 * 60 + 30)   # 11:30~13:30
DINNER_WINDOW = (18 * 60, 20 * 60)             # 18:00~20:00

STAY_GRID_MIN = 15   # 체류시간 격자 (팀원 최종 확정: 15분, 30분 아님)


@dataclass
class DayAvailability:
    """하루치 계산 결과를 한 번에 담는 컨테이너. day_case는 ItineraryDay.day_case에 그대로 저장 가능."""
    day_index: int
    day_case: str          # "A" 입도일 / "B" 중간일차 / "C" 출도일 / "D" 당일치기
    avail_hours: float
    avail_start_min: int   # 그날 활동 시작 시각(분, 0~1440 범위의 하루 내 분)
    avail_end_min: int
    need_morning: bool
    need_lunch: bool
    need_dinner: bool
    need_night_spot: bool
    target_slots: int

MORNING_WINDOW = (7 * 60, 9 * 60)  # ★ 신규: 07:00~09:00


def check_meal_flags(start_min: int, end_min: int) -> tuple[bool, bool, bool]:
    """★ 변경: 반환값이 2개→3개 (morning 추가)."""
    need_morning = not (end_min <= MORNING_WINDOW[0] or start_min >= MORNING_WINDOW[1])
    need_lunch = not (end_min <= LUNCH_WINDOW[0] or start_min >= LUNCH_WINDOW[1])
    need_dinner = not (end_min <= DINNER_WINDOW[0] or start_min >= DINNER_WINDOW[1])
    return need_morning, need_lunch, need_dinner


def check_checkin_flag(day_index: int, day_last_arrival_min: int) -> bool:
    """
    ★ 신규 — accommodations 계약의 CHECKIN_CUTOFF(18:00) 규칙 그대로 반영.
    1일차(day_index==1)에만 의미 있음 (앵커 분할 비활성이라 숙소 바뀌는 날이 없음).
    """
    if day_index != 1:
        return False
    return day_last_arrival_min < 18 * 60


def _minutes_of_day(dt: datetime) -> int:
    """datetime에서 '그날 0시부터 몇 분 지났는지'만 뽑아낸다."""
    return dt.hour * 60 + dt.minute


def calc_day_case(day_index: int, total_days: int) -> str:
    """
    일차 인덱스와 전체 일수로 Case A~D를 결정한다.
    day_index는 1부터 시작.
    """
    if total_days == 1:
        return "D"  # 당일치기
    if day_index == 1:
        return "A"  # 입도일
    if day_index == total_days:
        return "C"  # 출도일
    return "B"      # 중간일차


def calc_avail_hours(
    day_index: int,
    total_days: int,
    trip_start_dt: datetime,
    trip_end_dt: datetime,
    arrival_buffer_min: int = ARRIVAL_BUFFER_MIN,
    airport_buffer_min: int = AIRPORT_BUFFER_MIN,
) -> DayAvailability:
    """
    Pipeline 3.1 — Case A~D에 따라 하루 가용시간(AVAIL_HOURS)을 계산한다.

    Args:
        day_index: 1부터 시작하는 일차
        total_days: 전체 여행 일수
        trip_start_dt: 사용자가 입력한 여행 시작 일시 (제주 도착 시각)
        trip_end_dt: 사용자가 입력한 여행 종료 일시 (제주 출발 시각)

    Returns:
        DayAvailability — 이후 need_lunch/dinner/night_spot 판정, TargetSlots 계산에 그대로 넘길 수 있음
    """
    day_case = calc_day_case(day_index, total_days)

    if day_case == "D":
        start_min = max(_minutes_of_day(trip_start_dt), DAY_START_ANCHOR)
        end_min = min(_minutes_of_day(trip_end_dt), DAY_END_ANCHOR)
        avail_min = max(0, end_min - start_min)

    elif day_case == "A":
        start_min = max(_minutes_of_day(trip_start_dt), DAY_START_ANCHOR)  # ★ 버퍼 삭제
        end_min = DAY_END_ANCHOR
        avail_min = max(0, end_min - start_min)

    elif day_case == "C":
        start_min = DAY_START_ANCHOR
        end_min = min(_minutes_of_day(trip_end_dt), DAY_END_ANCHOR)  # ★ 버퍼 삭제
        avail_min = max(0, end_min - start_min)

    else:  # "B"
        start_min = DAY_START_ANCHOR
        end_min = DAY_END_ANCHOR
        avail_min = end_min - start_min

    avail_hours = avail_min / 60

    # 예외 플래그 판정 (3.2절)
    need_morning, need_lunch, need_dinner = check_meal_flags(start_min, end_min)
    need_night = check_night_spot_flag(end_min)

    # 야간 입도 케이스: 가용시간이 1시간 이하면 관광지 스케줄링 자체를 중단해야 하므로
    # 여기서는 플래그만 세우고, 실제 "관광지 배제 + 야식만" 판단은 course_builder에서 처리
    is_late_arrival = (day_case == "A" and avail_hours <= 1.0)
    is_early_departure = (day_case == "C" and _minutes_of_day(trip_end_dt) <= 11 * 60 + 30)

    target_slots = calc_target_slots(
        avail_hours,
        mode="pref",  # 기본값. 실제 호출 시 course_priority로 교체
        need_night_spot=need_night,
    )

    return DayAvailability(
        day_index=day_index,
        day_case=day_case,
        avail_hours=round(avail_hours, 2),
        avail_start_min=start_min,
        avail_end_min=end_min,
        need_morning=need_morning and not is_late_arrival,
        need_lunch=need_lunch and not is_late_arrival,
        need_dinner=need_dinner or is_late_arrival,  # 야간 입도는 저녁(야식)만 배치
        need_night_spot=need_night,
        target_slots=target_slots,
    )


def check_meal_flags(start_min: int, end_min: int) -> tuple[bool, bool, bool]:
    """가용시간 구간에 점심/저녁 시간대가 포함되는지 판정 (3.2-1)."""
    need_morning = not (end_min <= MORNING_WINDOW[0] or start_min >= MORNING_WINDOW[1])
    need_lunch = not (end_min <= LUNCH_WINDOW[0] or start_min >= LUNCH_WINDOW[1])
    need_dinner = not (end_min <= DINNER_WINDOW[0] or start_min >= DINNER_WINDOW[1])
    return need_morning, need_lunch, need_dinner


def check_night_spot_flag(end_min: int) -> bool:
    """
    3.2-4: 활동 종료가 21:00 이상이고, 20:00 이후 잔여시간이 60분 이상이면 야간 슬롯 필요.
    주의: DAY_END_ANCHOR가 21:00 고정이라 실질적으로 end_min이 21:00을 넘는 경우는
    사용자가 21시 이후 종료를 입력했을 때만 발생한다.
    """
    if end_min < DAY_END_ANCHOR:
        return False
    remaining_after_20 = end_min - DINNER_WINDOW[1]
    return remaining_after_20 >= 60


def calc_target_slots(avail_hours: float, mode: str, need_night_spot: bool = False) -> int:
    """
    Pipeline 3.3 — TargetSlots(d) = Round(7.0 × AVAIL/12.0 × Mode_M), 하한선 규칙 포함.

    Args:
        mode: "dist" | "pref" | "relax" — TripRequest.course_priority 값과 동일
    """
    if mode not in MODE_MULTIPLIER:
        raise ValueError(f"알 수 없는 코스 모드: {mode}")

    raw = BASELINE_SLOTS * (avail_hours / (FULL_DAY_MIN / 60)) * MODE_MULTIPLIER[mode]
    slots = round(raw)

    # 하한선 보장 규칙: AVAIL_HOURS >= 3.0이면 최소 2슬롯(식당1+관광지/카페1) 보장
    if avail_hours >= 3.0 and slots < 2:
        slots = 2

    # 야간 슬롯이 필요하면 1개를 추가로 확보 (기존 슬롯 수 축소 없이 +1)
    if need_night_spot:
        slots += 1

    return max(slots, 0)


def snap_to_15min(minutes: float) -> int:
    """
    체류시간을 15분 격자로 반올림. 최소 15분 강제.
    이동시간(5분 격자, itinerary.py 담당)과는 다른 별개의 격자이므로 함수도 분리해둔다.
    """
    snapped = round(minutes / STAY_GRID_MIN) * STAY_GRID_MIN
    return max(int(snapped), STAY_GRID_MIN)


# ── 최소 동작 확인 (정식 단위테스트 대신, 시간 절약용) ──────────
if __name__ == "__main__":
    # Case A 예시: 14:00 도착, 3박4일 여행의 1일차
    result = calc_avail_hours(
        day_index=1,
        total_days=4,
        trip_start_dt=datetime(2026, 9, 1, 14, 0),
        trip_end_dt=datetime(2026, 9, 4, 17, 0),
    )
    print("Case A (14:00 입도):", result)
    # 기대값: avail_hours ≈ 6.5, day_case='A'

    # Case B 예시: 중간일차
    result_b = calc_avail_hours(
        day_index=2, total_days=4,
        trip_start_dt=datetime(2026, 9, 1, 14, 0),
        trip_end_dt=datetime(2026, 9, 4, 17, 0),
    )
    print("Case B (중간일차):", result_b)
    # 기대값: avail_hours == 12.0, day_case='B'

    # Case C 예시: 17:00 출발
    result_c = calc_avail_hours(
        day_index=4, total_days=4,
        trip_start_dt=datetime(2026, 9, 1, 14, 0),
        trip_end_dt=datetime(2026, 9, 4, 17, 0),
    )
    print("Case C (17:00 출도):", result_c)
    # 기대값: avail_hours ≈ 6.5, day_case='C'

    print("15분 격자 스냅 테스트:", snap_to_15min(40), snap_to_15min(50))
    # 기대값: 45, 45 (40→45반올림, 50→45반올림... 실제론 round(50/15)*15=45)


# 권역(4분면) 분류
# 한라산 정상 좌표를 기준점으로 위/경도만 비교하는 방식
    
HALLASAN_LAT = 33.3617   # 한라산 정상 기준점
HALLASAN_LNG = 126.5292

def classify_quadrant(latitude: float, longitude: float) -> str:
        """
        위경도를 한라산 정상 기준으로 비교해서 4분면 중 하나를 반환.
        Returns: "NE"(북동) | "NW"(북서) | "SE"(남동) | "SW"(남서)

        import_tour_api 커맨드에서 Place.quadrant를 채울 때 이 함수를 호출한다.
        """
        is_north = latitude >= HALLASAN_LAT
        is_east = longitude >= HALLASAN_LNG

        if is_north and is_east:
            return "NE"
        if is_north and not is_east:
            return "NW"
        if not is_north and is_east:
            return "SE"
        return "SW"

JEJU_AIRPORT_LAT = 33.5104
JEJU_AIRPORT_LNG = 126.4914


def estimate_airport_travel_min(place_lat: float, place_lng: float, transport_mode: str) -> float:
    """
    공항↔장소 구간 전용 근사 계산. OSRM 매트릭스에 공항이 없어 정식 조회 불가하므로
    직선거리(하버사인) × 도로보정계수 ÷ 평균속도로 근사.
    ⚠️ 임시 방편 — 팀에 "다음 OSRM 매트릭스 재빌드 시 공항 노드 추가 가능한지" 확인 권장.
    """
    import math
    R = 6371
    dlat = math.radians(place_lat - JEJU_AIRPORT_LAT)
    dlng = math.radians(place_lng - JEJU_AIRPORT_LNG)
    a = (math.sin(dlat/2)**2 +
         math.cos(math.radians(JEJU_AIRPORT_LAT)) * math.cos(math.radians(place_lat)) * math.sin(dlng/2)**2)
    distance_km = R * 2 * math.asin(math.sqrt(a)) * 1.3  # 도로 보정계수

    avg_speed_kmh = 40 if transport_mode in ("rental_car", "own_car") else 35
    return (distance_km / avg_speed_kmh) * 60


# ── 최소 동작 확인 ──────────────────────────────────────────
if __name__ == "__main__":
    # Case A 예시
    result = calc_avail_hours(
        day_index=1,
        total_days=4,
        trip_start_dt=datetime(2026, 9, 1, 14, 0),
        trip_end_dt=datetime(2026, 9, 4, 17, 0),
    )
    print("Case A (14:00 입도):", result)

    # 4분면 테스트
    print(classify_quadrant(33.4587, 126.9425))  # SE
    print(classify_quadrant(33.4966, 126.2419))  # NW