"""
숙박 로직
: 코스가 완성된 후, 각 ItineraryDay에 숙소를 매칭해서 붙이고, Trip.com 예약 링크를 붙이는 로직 
"""
from __future__ import annotations
import math
from apps.places.models import Lodging
from apps.trips.models import ItineraryDay


def _haversine_km(lat1, lng1, lat2, lng2) -> float:
    R = 6371
    dlat, dlng = math.radians(lat2 - lat1), math.radians(lng2 - lng1)
    a = (math.sin(dlat / 2) ** 2 +
         math.cos(math.radians(lat1)) * math.cos(math.radians(lat2)) * math.sin(dlng / 2) ** 2)
    return R * 2 * math.asin(math.sqrt(a))


def _passes_required_filters(lodging: Lodging, quadrant: str, capacity: int,
                              lodging_type: str, conditions: list[str]) -> tuple[bool, int]:
    """
    §2.1 기본 필터 + §2.2 Unknown 처리.
    Returns: (통과 여부, unknown_count) — unknown_count는 등급 내 2차 정렬 기준.
    """
    if quadrant and lodging.quadrant != quadrant:
        return False, 0

    if lodging_type and lodging_type != "상관없음" and lodging.small_category_name != lodging_type:
        return False, 0

    unknown_count = 0

    # 인원 조건 — room_capacity_summary가 없으면 Unknown 유지, 있으면 확인
    if capacity:
        if not lodging.room_capacity_summary:
            unknown_count += 1
        else:
            # room_capacity_summary 예: "35평 A: 기준 4, 최대 6" — "최대 N" 숫자만 추출
            import re
            match = re.search(r"최대\s*(\d+)", lodging.room_capacity_summary)
            if match and int(match.group(1)) < capacity:
                return False, 0
            elif not match:
                unknown_count += 1

    for cond in conditions:
        if cond == "주차가능":
            if lodging.parking == "불가능":
                return False, 0
            if lodging.parking not in ("가능",):
                unknown_count += 1
        elif cond == "취사가능":
            if lodging.cooking == "불가능":
                return False, 0
            if lodging.cooking not in ("가능",):
                unknown_count += 1

    return True, unknown_count


def match_lodging_for_day(
    day: ItineraryDay,
    last_place_lat: float,
    last_place_lng: float,
    lodging_capacity: int,
    lodging_type: str,
    lodging_conditions: list[str],
    top_n: int = 3,
) -> list[dict]:
    """
    이동 및 최종 순위. 자유입력(QueryFit)은 임베딩 세팅 전이라 이번 버전에서는
    "자유 입력 없는 경우" 로직만 구현한다 (§3의 1~4등급 로직은 추후 확장).

    Returns: 상위 top_n개의 {"lodging":, "distance_km":, "unknown_count":} 리스트
    """
    quadrant = day.trip.region_preference
    candidates = []

    for lodging in Lodging.objects.all():
        ok, unknown_count = _passes_required_filters(
            lodging, quadrant, lodging_capacity, lodging_type, lodging_conditions,
        )
        if not ok:
            continue
        distance = _haversine_km(last_place_lat, last_place_lng, lodging.latitude, lodging.longitude)
        candidates.append({"lodging": lodging, "distance_km": distance, "unknown_count": unknown_count})

    # 1순위: 이동거리, 2순위: unknown 적은 순 (§3 "자유입력 없는 경우" 정렬 규칙)
    candidates.sort(key=lambda c: (c["distance_km"], c["unknown_count"]))
    return candidates[:top_n]


def build_tripcom_link(lodging: Lodging, checkin_date: str, checkout_date: str,
                        adult_count: int, alliance_id: str, sid: str) -> dict:
    """
    예약 연결. tripcom_hotel_id가 있으면 Tier1, 없으면 Tier2(도시검색) 폴백.
    alliance_id/sid는 팀이 발급받은 값을 .env에서 가져와 여기 인자로 넘겨야 한다.
    """
    if lodging.tripcom_hotel_id:
        link = (
            f"https://hk.trip.com/hotels/redirect?"
            f"hotelid={lodging.tripcom_hotel_id}&Allianceid={alliance_id}&Sid={sid}"
            f"&trip_sub1={lodging.content_id}"
        )
        link_type = "hotel"
    else:
        link = (
            f"https://kr.trip.com/hotels/jeju-hotels-list-737/?"
            f"Allianceid={alliance_id}&SID={sid}&trip_sub1={lodging.content_id}"
            f"&checkin={checkin_date}&checkout={checkout_date}&adult={adult_count}"
        )
        link_type = "city_fallback"

    price_hint = f"참고가 {lodging.min_room_price}원~ (실시간 아님)" if lodging.min_room_price else "가격 확인 필요"

    return {"tripcom_link": link, "tripcom_link_type": link_type, "price_hint": price_hint}