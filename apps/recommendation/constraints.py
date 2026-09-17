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

# ── 상수 정의 ────────────────────────────────────────────────
from __future__ import annotations
from datetime import datetime
from dataclasses import dataclass

DAY_START_ANCHOR = 9 * 60
DAY_END_ANCHOR = 21 * 60
BASELINE_SLOTS = 7.0
FULL_DAY_MIN = 12 * 60
MODE_MULTIPLIER = {"dist": 1.0, "pref": 0.9, "relax": 0.7}

MORNING_WINDOW = (7 * 60, 9 * 60)
LUNCH_WINDOW = (11 * 60, 13 * 60)
DINNER_WINDOW = (18 * 60, 20 * 60)

STAY_GRID_MIN = 15
HALLASAN_LAT = 33.3617
HALLASAN_LNG = 126.5292
JEJU_AIRPORT_LAT = 33.5104
JEJU_AIRPORT_LNG = 126.4914
JEJU_AIRPORT_PROXY_CONTENT_ID = "2929823" # 올리브영 제주국제공항점


@dataclass
class DayAvailability:
    day_index: int
    day_case: str
    avail_hours: float
    avail_start_min: int
    avail_end_min: int
    need_morning: bool
    need_lunch: bool
    need_dinner: bool
    need_night_spot: bool
    target_slots: int
    

def _overlap(window: tuple[int, int], start_min: int, end_min: int) -> tuple[int, int] | None:
    """window와 [start_min, end_min)의 겹치는 구간. 안 겹치면 None."""
    o_start = max(window[0], start_min)
    o_end = min(window[1], end_min)
    if o_start >= o_end:
        return None
    return (o_start, o_end)


def get_meal_windows(start_min: int, end_min: int) -> dict:
    """그날 가용시간과 세 식사시간대의 실제 겹치는 구간. 겹친 만큼만 식사시간으로 고정."""
    return {
        "morning": _overlap(MORNING_WINDOW, start_min, end_min),
        "lunch": _overlap(LUNCH_WINDOW, start_min, end_min),
        "dinner": _overlap(DINNER_WINDOW, start_min, end_min),
    }


def _minutes_of_day(dt: datetime) -> int:
    return dt.hour * 60 + dt.minute


def check_meal_flags(start_min: int, end_min: int) -> tuple[bool, bool, bool]:
    need_morning = not (end_min <= MORNING_WINDOW[0] or start_min >= MORNING_WINDOW[1])
    need_lunch = not (end_min <= LUNCH_WINDOW[0] or start_min >= LUNCH_WINDOW[1])
    need_dinner = not (end_min <= DINNER_WINDOW[0] or start_min >= DINNER_WINDOW[1])
    return need_morning, need_lunch, need_dinner


def check_night_spot_flag(end_min: int) -> bool:
    if end_min < DAY_END_ANCHOR:
        return False
    return (end_min - DINNER_WINDOW[1]) >= 60


def calc_avail_hours_from_schedule(day_index, total_days, day_start_kst, day_end_kst) -> DayAvailability:
    """클램프 없음 — 사용자가 입력한 시각을 그대로 가용 구간으로 사용."""
    start_min = _minutes_of_day(day_start_kst)
    end_min = _minutes_of_day(day_end_kst)
    avail_min = max(0, end_min - start_min)
    avail_hours = avail_min / 60

    day_case = "D" if total_days == 1 else ("A" if day_index == 1 else ("C" if day_index == total_days else "B"))

    need_morning, need_lunch, need_dinner = check_meal_flags(start_min, end_min)
    need_night = check_night_spot_flag(end_min)
    target_slots = calc_target_slots(avail_hours, mode="pref", need_night_spot=need_night)

    return DayAvailability(
        day_index=day_index, day_case=day_case, avail_hours=round(avail_hours, 2),
        avail_start_min=start_min, avail_end_min=end_min,
        need_morning=need_morning, need_lunch=need_lunch, need_dinner=need_dinner,
        need_night_spot=need_night, target_slots=target_slots,
    )


def calc_target_slots(avail_hours: float, mode: str, need_night_spot: bool = False) -> int:
    if mode not in MODE_MULTIPLIER:
        raise ValueError(f"알 수 없는 코스 모드: {mode}")
    raw = BASELINE_SLOTS * (avail_hours / (FULL_DAY_MIN / 60)) * MODE_MULTIPLIER[mode]
    slots = round(raw)
    if avail_hours >= 3.0 and slots < 2:
        slots = 2
    if need_night_spot:
        slots += 1
    return max(slots, 0)


def snap_to_15min(minutes: float) -> int:
    snapped = round(minutes / STAY_GRID_MIN) * STAY_GRID_MIN
    return max(int(snapped), STAY_GRID_MIN)


def floor_to_15min(minutes: float) -> int:
    return max(int(minutes // STAY_GRID_MIN) * STAY_GRID_MIN, 0)


def snap_travel_time_5min(minutes: float) -> int:
    """1~4분→0, 5~9분→5 ... (내림)"""
    return int(minutes // 5) * 5


def classify_quadrant(latitude: float, longitude: float) -> str:
    is_north = latitude >= HALLASAN_LAT
    is_east = longitude >= HALLASAN_LNG
    if is_north and is_east: return "NE"
    if is_north and not is_east: return "NW"
    if not is_north and is_east: return "SE"
    return "SW"


def estimate_airport_travel_min(place_lat: float, place_lng: float, transport_mode: str) -> float:
    import math
    R = 6371
    dlat = math.radians(place_lat - JEJU_AIRPORT_LAT)
    dlng = math.radians(place_lng - JEJU_AIRPORT_LNG)
    a = (math.sin(dlat/2)**2 +
         math.cos(math.radians(JEJU_AIRPORT_LAT)) * math.cos(math.radians(place_lat)) * math.sin(dlng/2)**2)
    distance_km = R * 2 * math.asin(math.sqrt(a)) * 1.3
    avg_speed_kmh = 40 if transport_mode == "car" else 35
    return (distance_km / avg_speed_kmh) * 60