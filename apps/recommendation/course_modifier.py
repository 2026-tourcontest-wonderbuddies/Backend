"""
코스 수정 시 연쇄 재계산을 위한 엔진
- 장소를 다시 고를 피룡가 없으면(순서 변경/삭제/이미 정해진 장소 삽입),
빔서치 다시 실행X -> 순서대로 시간만 다시 채우는 재계산만 실행

- 장소를 새로 골라야 하는 경우(ex: 챗봇의 덜 걷는곳으로 등)만 그 구간에 한 해
빔서치 실행
"""

from __future__ import annotations
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

from apps.trips.models import ItineraryDay, ItineraryItem
from apps.recommendation.course_builder import beam_search_day, select_best_course
from apps.recommendation.engine_provider import get_routing_engine
from apps.recommendation.constraints import estimate_airport_travel_min, snap_travel_time_5min

KST = ZoneInfo("Asia/Seoul")
DEFAULT_VEHICLE = "car"


def _get_travel_time_fn(routing_engine):
    def _fn(origin_id: str, destination_id: str) -> dict:
        return routing_engine.get_travel_time(origin_id, destination_id, mode="osrm", vehicle=DEFAULT_VEHICLE)
    return _fn

def _travel_from_coords(lat:float, lon:float, place, transport_mode:str = "car") -> float:
    import math
    R = 6371
    dlat = math.radians(place.latitude - lat)
    dlng = math.radians(place.longitude - lon)
    a = (math.sin(dlat/2)**2 +
         math.cos(math.radians(lat)) * math.cos(math.radians(place.latitude)) * math.sin(dlng/2)**2)
    distance_km = R * 2 * math.asin(math.sqrt(a)) * 1.3
    avg_speed_kmh = 40 if transport_mode in ("car",) else 35
    return (distance_km / avg_speed_kmh) * 60


def recalc_first_and_last_item_travel(course) -> None:
    routing_engine = get_routing_engine()
    matrix_ids = set(routing_engine._pos.keys())

    days = list(course.days.order_by("day_index"))
    total_days = len(days)

    for i, day in enumerate(days):
        items = list(day.items.order_by("order"))
        if not items:
            continue

        first_item = items[0]
        last_item = items[-1]

        # ── 1. 첫 항목 이동시간 ──
        if i == 0:
            travel_min_in = estimate_airport_travel_min(
                first_item.place.latitude, first_item.place.longitude, DEFAULT_VEHICLE
            )
        else:
            # ★ 수정: 그날 자신의 lodging_snapshot이 아니라, "전날" 숙소를 참조
            prev_day = days[i - 1]
            lodging = prev_day.lodging_snapshot   # ★ day → prev_day로 변경!
            if lodging:
                lodging_content_id = lodging.get("content_id")
                if lodging_content_id and lodging_content_id in matrix_ids and first_item.place.content_id in matrix_ids:
                    result = routing_engine.get_travel_time(lodging_content_id, first_item.place.content_id,
                                                              mode="osrm", vehicle=DEFAULT_VEHICLE)
                    travel_min_in = result["duration_min_adjusted"]
                else:
                    travel_min_in = _travel_from_coords(lodging["lat"], lodging["lon"], first_item.place, DEFAULT_VEHICLE)
            else:
                travel_min_in = None

        if travel_min_in is not None:
            travel_min_in = snap_travel_time_5min(travel_min_in)
            target_date = day.course.trip.start_date + timedelta(days=day.day_index - 1)
            day_start = datetime(
                target_date.year, target_date.month, target_date.day,
                day.avail_start_min // 60, day.avail_start_min % 60, tzinfo=KST,
            )
            arrive_at = day_start + timedelta(minutes=travel_min_in)
            depart_at = arrive_at + timedelta(minutes=first_item.place.stay_time_minutes)

            first_item.travel_min_from_prev = travel_min_in
            first_item.arrive_at = arrive_at
            first_item.depart_at = depart_at
            first_item.save(update_fields=["travel_min_from_prev", "arrive_at", "depart_at"])

            recalc_timeline_from(day, start_order=1)
            last_item.refresh_from_db()

        # ── 2. 마지막 항목 → 숙소/공항 (이건 원래도 맞았음, day.lodging_snapshot이 "오늘 밤 묵을 곳"이 맞으니까) ──
        if i == total_days - 1:
            travel_min_out = estimate_airport_travel_min(
                last_item.place.latitude, last_item.place.longitude, DEFAULT_VEHICLE
            )
        else:
            lodging = day.lodging_snapshot   # 이건 그대로 맞음 (오늘 밤 묵을 숙소)
            if not lodging:
                travel_min_out = None
            else:
                lodging_content_id = lodging.get("content_id")
                if lodging_content_id and lodging_content_id in matrix_ids and last_item.place.content_id in matrix_ids:
                    result = routing_engine.get_travel_time(last_item.place.content_id, lodging_content_id,
                                                              mode="osrm", vehicle=DEFAULT_VEHICLE)
                    travel_min_out = result["duration_min_adjusted"]
                else:
                    travel_min_out = _travel_from_coords(lodging["lat"], lodging["lon"], last_item.place, DEFAULT_VEHICLE)

        if travel_min_out is not None:
            day.travel_to_next_min = snap_travel_time_5min(travel_min_out)
            day.save(update_fields=["travel_to_next_min"])

# 장소 그대로, 시간만 순서대로 다시 채우기
def recalc_timeline_from(day: ItineraryDay, start_order: int = 0) -> dict:
    """
    day.items를 order 순으로 훑으면서, start_order 지점부터 끝까지
    도착/출발 시각·이동시간을 다시 계산해서 DB에 반영한다.
    """
    routing_engine = get_routing_engine()
    get_travel_time_fn = _get_travel_time_fn(routing_engine)
    matrix_ids = set(routing_engine._pos.keys())

    items = list(day.items.order_by("order"))
    if start_order >= len(items):
        return {"ok": True, "over_budget": False}

    if start_order == 0:
        target_date = day.course.trip.start_date + timedelta(days=day.day_index - 1)
        current_time = datetime(
            target_date.year, target_date.month, target_date.day,
            day.avail_start_min // 60, day.avail_start_min % 60,
            tzinfo=KST,
        )
        prev_place = None
    else:
        current_time = items[start_order - 1].depart_at.astimezone(KST)
        prev_place = items[start_order - 1].place

    day_end_min = day.avail_start_min + int(day.avail_hours * 60)

    for item in items[start_order:]:
        if prev_place is not None and prev_place.content_id in matrix_ids and item.place.content_id in matrix_ids:
            travel_result = get_travel_time_fn(prev_place.content_id, item.place.content_id)
            travel_min = travel_result["duration_min_adjusted"]
        else:
            travel_min = estimate_airport_travel_min(item.place.latitude, item.place.longitude, DEFAULT_VEHICLE)

        travel_min = snap_travel_time_5min(travel_min)   # ★ 5분 정규화 적용

        current_time += timedelta(minutes=travel_min)
        item.arrive_at = current_time
        item.travel_min_from_prev = travel_min

        current_time += timedelta(minutes=item.place.stay_time_minutes)
        item.depart_at = current_time
        item.save(update_fields=["arrive_at", "depart_at", "travel_min_from_prev"])

        prev_place = item.place

    finish_min = current_time.hour * 60 + current_time.minute
    over_budget = finish_min > day_end_min
    return {"ok": True, "over_budget": over_budget}


def resequence_orders(day: ItineraryDay) -> None:
    """items를 arrive_at 순 등 원하는 순서로 만든 뒤 order 필드를 0,1,2...로 재부여."""
    items = list(day.items.order_by("order"))
    for i, item in enumerate(items):
        if item.order != i:
            item.order = i
            item.save(update_fields=["order"])


# ── ② 무거운 재계산: 고정 안 된 구간만 빔서치로 장소 자체를 다시 고름 ──────

def regenerate_unlocked_segment(day: ItineraryDay, purpose_main: str, purpose_sub: str,
                                 mode: str, extra_exclude_ids: set[str] = None) -> None:
    """
    day.items 중 locked=False인 연속 구간을 찾아서, 그 구간만 beam_search_day로
    다시 채운다. locked=True인 항목들은 위치·장소 그대로 유지하고, 그 사이 시간을
    예산으로 삼아 그 구간만 새로 탐색한다.

    단순화: "고정 항목이 하나라도 있으면, 그 고정 항목들 뒤쪽 전체를 한 구간으로
    다시 채운다" (여러 고정 항목 사이사이를 정교하게 나누는 건 후속 개선 과제로 남김 — 여러 세그먼트 분할은 로직이 급격히 복잡해지고, 실무에서 "장소 하나 유지 + 나머지 재추천" 패턴이 대다수라 이 단순화로 충분함)
    """
    from apps.places.models import Place

    routing_engine = get_routing_engine()
    get_travel_time_fn = _get_travel_time_fn(routing_engine)
    get_stay_time_fn = lambda p: p.stay_time_minutes

    items = list(day.items.order_by("order"))
    locked_items = [i for i in items if i.locked]
    unlocked_items = [i for i in items if not i.locked]

    if not unlocked_items:
        return {"ok": True, "regenerated": 0}

    first_unlocked_order = unlocked_items[0].order
    last_locked_before = None
    for it in items:
        if it.order < first_unlocked_order and it.locked:
            last_locked_before = it

    start_place = last_locked_before.place if last_locked_before else None
    start_time = last_locked_before.depart_at.astimezone(KST) if last_locked_before else (
        day.course.trip.start_datetime.astimezone(KST).replace(
            hour=day.avail_start_min // 60, minute=day.avail_start_min % 60
        )
    )
    day_end_min = day.avail_start_min + int(day.avail_hours * 60)
    remain_hours = max(0.0, (day_end_min - (start_time.hour * 60 + start_time.minute)) / 60)

    exclude_ids = {i.place.content_id for i in locked_items}
    if extra_exclude_ids:
        exclude_ids |= extra_exclude_ids

    matrix_ids = set(routing_engine._pos.keys())
    candidate_pool = list(
        Place.objects.exclude(content_type_name="음식점")
        .filter(content_id__in=matrix_ids)
        .exclude(content_id__in=exclude_ids)
    )

    beams = beam_search_day(
        candidate_pool=candidate_pool, start_place=start_place,
        avail_hours=remain_hours, target_slots=len(unlocked_items), mode=mode,
        purpose_main=purpose_main, purpose_sub=purpose_sub,
        transport_mode=DEFAULT_VEHICLE, region_quadrant=None,
        exclude_place_ids=[], exclude_categories=[],
        visit_start_datetime=start_time,
        get_travel_time_fn=get_travel_time_fn, get_stay_time_fn=get_stay_time_fn,
        visited_across_days=exclude_ids,
    )
    best = select_best_course(beams, remain_hours, mode)

    # 기존 unlocked 항목들 삭제 후, 새로 채운 것으로 교체
    for it in unlocked_items:
        it.delete()

    order = first_unlocked_order
    if best:
        for chunk_item in best.items:
            ItineraryItem.objects.create(
                day=day, order=order, place=chunk_item["place"], slot_type="GENERAL",
                arrive_at=start_time, depart_at=start_time,   # 임시값, 아래에서 재계산으로 확정
                travel_min_from_prev=0,
            )
            order += 1

    resequence_orders(day)
    recalc_timeline_from(day, start_order=first_unlocked_order)
    return {"ok": True, "regenerated": order - first_unlocked_order}