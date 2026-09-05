"""
신규 파일 — accommodations 패키지(팀원 완성품)를 우리 Django 흐름에 연결하는 얇은 어댑터.
Django ORM을 전혀 안 쓰고, TripRequest 딕셔너리를 accommodations의 TripContext/
LodgingRequest로 변환 → recommend_anchor() 호출 → 결과를 다시 우리 JSON 구조로 변환.
"""

from accommodations.lodging_filter import TripContext, LodgingRequest
from accommodations.recommend import LodgingRecommender
from typing import Optional

VEHICLE_MAP = {
    "rental_car": "rental",
    "own_car": "car",
    "taxi": "taxi",
}

# ★ accommodations 계약(§L0)이 요구하는 형식: 'YYYY-MM-DD'
def _to_date_str(dt) -> str:
    return dt.strftime("%Y-%m-%d")


def get_lodging_anchor(
    trip,                          # apps.trips.models.TripRequest 인스턴스
    day_last_place_ids: list[str],  # 각 박(night)의 그날 마지막 방문 장소 content_id
    day_regions: Optional[list[str]] = None,
) -> list[dict]:
    """
    여행 전체 숙소 추천(앵커 1곳)을 실행하고 LodgingCard 리스트를 dict로 반환.
    engine.py에서 코스 생성 후 이 함수 한 번만 호출하면 됨.

    Returns: [{"content_id":, "title":, "tripcom_link":, ...}, ...] (top 3)
    """
    nights = (trip.end_datetime.date() - trip.start_datetime.date()).days

    trip_ctx = TripContext(
        start_date=_to_date_str(trip.start_datetime),
        nights=nights,
        guests=trip.guests,   # ★ companion_type 삭제, guests로 통일
        vehicle=VEHICLE_MAP.get(trip.transport_mode),
        regions=() if not trip.region_preference or trip.region_preference == "ALL"
                 else (trip.region_preference,),
        day_last_place_ids=tuple(day_last_place_ids),
        day_regions=tuple(day_regions) if day_regions else (),
    )

    lodging_request = LodgingRequest.from_trip_context(
        trip_ctx,
        lodging_type=trip.lodging_type or "상관없음",
        need_cooking=trip.lodging_need_cooking,
        free_text=trip.lodging_free_text,
    )

    recommender = LodgingRecommender()  # engine=None → routing/hybrid_engine 자동 로드
    segments = recommender.recommend_anchor(trip_ctx, lodging_request, top_n=3)

    # SPLIT_ANCHORS_ENABLED=False라 segments는 항상 길이 1 (앵커 1곳 고정, §6)
    cards = segments[0].cards if segments else []
    return [card.to_dict() for card in cards]