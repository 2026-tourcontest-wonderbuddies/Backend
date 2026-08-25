"""
후보 장소 1차 필터 (Hard Constraints & Time Filtering)
"""

from datetime import datetime, timedelta

WEEKDAY_MAP = ["월", "화", "수", "목", "금", "토", "일"]

def is_open_at(place, visit_datetime: datetime) -> bool:
    """
    1. 영업시간, 휴무일 하드필터
    """
    weekday_str = WEEKDAY_MAP[visit_datetime.weekday()]
    if weekday_str in place.closed_weekdays:
        return False

    # 운영시간 정보 없는 경우: 통과 처리
    if place.open_time is None or place.close_time is None:
        return True

    visit_time = visit_datetime.time()


    # ****자정 넘기는 시간 별도 처리 필요***
    if place.close_time >= place.open_time:
        return place.open_time <= visit_time <= place.close_time
    return visit_time >= place.open_time or visit_time <= place.close_time