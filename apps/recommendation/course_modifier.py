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
from apps.recommendation.constraints import estimate_airport_travel_min, snap_travel_time_5min, JEJU_AIRPORT_PROXY_CONTENT_ID

KST = ZoneInfo("Asia/Seoul")
DEFAULT_VEHICLE = "car"


def _get_travel_time_fn(routing_engine):
    def _fn(origin_id: str, destination_id: str, depart_at: datetime | None = None) -> dict:
        return routing_engine.get_travel_time(
            origin_id, destination_id, mode="osrm", vehicle=DEFAULT_VEHICLE, depart_at=depart_at
        )
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
    airport_available = JEJU_AIRPORT_PROXY_CONTENT_ID in matrix_ids

    days = list(course.days.order_by("day_index"))
    total_days = len(days)

    for i, day in enumerate(days):
        items = list(day.items.order_by("order"))
        if not items:
            continue

        first_item = items[0]
        last_item = items[-1]

        target_date = day.course.trip.start_date + timedelta(days=day.day_index - 1)
        day_start = datetime(
            target_date.year, target_date.month, target_date.day,
            day.avail_start_min // 60, day.avail_start_min % 60, tzinfo=KST,
        )

        # 첫 항목 이동시간
        if i == 0:
            if airport_available and first_item.place.content_id in matrix_ids:
                # 매트릭스에 있으면 OSRM 실측값 사용
                result = routing_engine.get_travel_time(
                    JEJU_AIRPORT_PROXY_CONTENT_ID, first_item.place.content_id,
                    mode="osrm", vehicle=DEFAULT_VEHICLE, depart_at=day_start,
                )
                travel_min_in = result["duration_min_adjusted"]
            else:
                # 없으면 제주공항으로 부터 직선거리라도...
                travel_min_in = estimate_airport_travel_min(
                    first_item.place.latitude, first_item.place.longitude, DEFAULT_VEHICLE
                )
        else:
            # 그날 자신의 lodging_snapshot이 아니라, "전날" 숙소를 참조
            prev_day = days[i - 1]
            lodging = prev_day.lodging_snapshot
            if lodging:
                lodging_content_id = lodging.get("content_id")
                if lodging_content_id and lodging_content_id in matrix_ids and first_item.place.content_id in matrix_ids:
                    result = routing_engine.get_travel_time(lodging_content_id, first_item.place.content_id,
                                                              mode="osrm", vehicle=DEFAULT_VEHICLE, depart_at=day_start)
                    travel_min_in = result["duration_min_adjusted"]
                else:
                    travel_min_in = _travel_from_coords(lodging["lat"], lodging["lon"], first_item.place, DEFAULT_VEHICLE)
            else:
                travel_min_in = None

        if travel_min_in is not None:
            travel_min_in = snap_travel_time_5min(travel_min_in)

            # 식사 슬롯 시간이면 원래 depart_at-arrvie_at 유지
            # stay_time_minutes로 계산x - engine.py가 설정한 식사 시간대 폭으로 고정
            if first_item.slot_type == "RESTAURANT":
                original_duration = first_item.depart_at - first_item.arrive_at
                arrive_at = first_item.arrive_at  # 식사는 시간대 시작에 이미 고정되어 있으므로 arrive_at 자체를 안 건드림
                depart_at = arrive_at + original_duration
            else:
                target_date = day.course.trip.start_date + timedelta(days=day.day_index - 1)
                day_start = datetime(
                    target_date.year, target_date.month, target_date.day,
                    day.avail_start_min // 60, day.avail_start_min % 60, tzinfo=KST,
                )
                arrive_at = day_start + timedelta(minutes=travel_min_in)
                stay_duration = first_item.stay_min if first_item.stay_min is not None else first_item.place.stay_time_minutes
                depart_at = arrive_at + timedelta(minutes=first_item.place.stay_time_minutes)

            first_item.travel_min_from_prev = travel_min_in
            first_item.arrive_at = arrive_at
            first_item.depart_at = depart_at
            first_item.save(update_fields=["travel_min_from_prev", "arrive_at", "depart_at"])

            recalc_timeline_from(day, start_order=1)
            last_item.refresh_from_db()

            if i == 0:
                day.airport_to_first_travel_min = travel_min_in
                day.save(update_fields=["airport_to_first_travel_min"])

        # 2. 마지막 항목 → 숙소/공항 
        if i == total_days - 1:
            if airport_available and last_item.place.content_id in matrix_ids:
                # OSRM 실측값 사용
                result = routing_engine.get_travel_time(
                    last_item.place.content_id, JEJU_AIRPORT_PROXY_CONTENT_ID,
                    mode="osrm", vehicle=DEFAULT_VEHICLE, depart_at=last_item.depart_at,
                )
                travel_min_out = result["duration_min_adjusted"]
            else:
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
                                                              mode="osrm", vehicle=DEFAULT_VEHICLE, depart_at=last_item.depart_at)
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
    airport_available = JEJU_AIRPORT_PROXY_CONTENT_ID in matrix_ids

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

        # Day1이면 공항 기준(prev_place=None)유지
        # Day2-N-1이면 전날 숙소를 전날 숙소를 가상의 출발점으로 사용
        if day.day_index == 1:
            prev_place = None
            prev_lodging = None
        else:
            prev_day = day.course.days.filter(day_index=day.day_index - 1).first()
            prev_place = None  # Place 객체가 아니라 좌표만 있으므로 None 유지하되, 아래서 좌표를 따로 씀
            prev_lodging = prev_day.lodging_snapshot if prev_day else None
    else:
        current_time = items[start_order - 1].depart_at.astimezone(KST)
        prev_place = items[start_order - 1].place
        prev_lodging = None

    day_end_min = day.avail_start_min + int(day.avail_hours * 60)

    for idx, item in enumerate(items[start_order:]):
        is_first_in_range = (idx == 0)

        if prev_place is not None and prev_place.content_id in matrix_ids and item.place.content_id in matrix_ids:
            travel_result = get_travel_time_fn(prev_place.content_id, item.place.content_id, depart_at=current_time)
            travel_min = travel_result["duration_min_adjusted"]
        elif is_first_in_range and start_order == 0 and day.day_index > 1 and prev_lodging:
            # Day2+ 첫 항목은 전날 숙소 좌표 기준으로 계산
            lodging_content_id = prev_lodging.get("content_id")
            if lodging_content_id and lodging_content_id in matrix_ids and item.place.content_id in matrix_ids:
                travel_result = get_travel_time_fn(lodging_content_id, item.place.content_id, depart_at=current_time)
                travel_min = travel_result["duration_min_adjusted"]
            else:
                travel_min = _travel_from_coords(prev_lodging["lat"], prev_lodging["lon"], item.place, DEFAULT_VEHICLE)
        else:
            travel_min = estimate_airport_travel_min(item.place.latitude, item.place.longitude, DEFAULT_VEHICLE)

        travel_min = snap_travel_time_5min(travel_min)

        # 식사 슬롯이면 시간대 고정 유지, 관광지면 기존처럼 순차 계산
        if item.slot_type == "RESTAURANT":
            original_duration = item.depart_at - item.arrive_at
            # 이동시간은 갱신하되, 시각은 시간대 고정 유지 (arrive_at은 그대로 둠)
            item.travel_min_from_prev = travel_min
            item.save(update_fields=["travel_min_from_prev"])
            current_time = item.depart_at.astimezone(KST)  # 다음 항목 계산을 위해 시간만 이어받음
        else:
            current_time += timedelta(minutes=travel_min)
            item.arrive_at = current_time
            item.travel_min_from_prev = travel_min
            stay_duration = item.stay_min if item.stay_min is not None else item.place.stay_time_minutes
            current_time += timedelta(minutes=stay_duration)
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


# 무거운 재계산: 고정 안 된 구간만 빔서치로 장소 자체를 다시 고름

def regenerate_unlocked_segment(day: ItineraryDay, purpose_main: str, purpose_sub: str,
                                 mode: str, extra_exclude_ids: set[str] = None) -> dict:
    """
    day.items 중 다시 뽑아도 되는 항목(GENERAL이면서 locked=False)만 beam_search_day로
    새로 채운다. 고정 항목과 식사 슬롯은 장소·위치·시각 그대로 남긴다.

    남겨둔 항목이 하루를 여러 조각으로 쪼개므로 구간별로 따로 탐색한다. 구간의 예산은
    "앞 항목이 끝나는 시각 ~ 뒤 항목이 시작하는 시각"이다. 이렇게 나누지 않고 하루치
    예산을 통째로 주면, 체류가 긴 장소 하나를 골라 고정된 식사 시각을 덮어버린다.
    """
    from apps.places.models import Place

    routing_engine = get_routing_engine()
    get_travel_time_fn = _get_travel_time_fn(routing_engine)
    get_stay_time_fn = lambda p: p.stay_time_minutes

    items = list(day.items.order_by("order"))

    # 식사 슬롯은 고정 항목과 똑같이 취급한다. 시각이 시간대에 묶여 있는 데다
    # 후보 풀에 음식점이 없어서, 다시 뽑게 두면 그 날 식사가 통째로 사라진다.
    # (사용자가 "그 식당 빼줘"라고 하면 호출부가 먼저 지우므로 여기까지 오지 않는다.)
    def _kept(item) -> bool:
        return item.locked or item.slot_type != "GENERAL"

    kept_items = [i for i in items if _kept(i)]
    unlocked_items = [i for i in items if not _kept(i)]

    if not unlocked_items:
        return {"ok": True, "regenerated": 0}

    target_date = day.course.trip.start_date + timedelta(days=day.day_index - 1)
    day_start = datetime(
        target_date.year, target_date.month, target_date.day,
        day.avail_start_min // 60, day.avail_start_min % 60, tzinfo=KST,
    )
    day_end = day_start + timedelta(hours=day.avail_hours)

    # 남겨둔 항목을 경계로 구간을 나눈다. 각 구간은 연속한 재생성 대상 한 묶음이다.
    segments = []
    group, prev_kept = [], None
    for it in items:
        if _kept(it):
            if group:
                segments.append((group, prev_kept, it))
                group = []
            prev_kept = it
        else:
            group.append(it)
    if group:
        segments.append((group, prev_kept, None))

    matrix_ids = set(routing_engine._pos.keys())
    exclude_ids = {i.place.content_id for i in kept_items}
    if extra_exclude_ids:
        exclude_ids |= extra_exclude_ids

    base_pool = list(
        Place.objects.exclude(content_type_name="음식점")
        .filter(content_id__in=matrix_ids)
        .exclude(content_id__in=exclude_ids)
    )

    first_unlocked_order = unlocked_items[0].order
    filled = []   # [(order, place, stay_min)] — 비운 자리를 그대로 재사용한다

    for group, prev_kept, next_kept in segments:
        start_place = prev_kept.place if prev_kept else None
        start_time = prev_kept.depart_at.astimezone(KST) if prev_kept else day_start
        end_time = next_kept.arrive_at.astimezone(KST) if next_kept else day_end
        avail_hours = (end_time - start_time).total_seconds() / 3600
        if avail_hours <= 0:
            continue   # 앞뒤 항목이 붙어 있으면 그 사이에 넣을 자리가 없다

        beams = beam_search_day(
            candidate_pool=[p for p in base_pool if p.content_id not in exclude_ids],
            start_place=start_place,
            avail_hours=avail_hours, target_slots=len(group), mode=mode,
            purpose_main=purpose_main, purpose_sub=purpose_sub,
            transport_mode=DEFAULT_VEHICLE, region_quadrant=None,
            exclude_place_ids=[], exclude_categories=[],
            visit_start_datetime=start_time,
            get_travel_time_fn=get_travel_time_fn, get_stay_time_fn=get_stay_time_fn,
            visited_across_days=exclude_ids,
        )
        best = select_best_course(beams, avail_hours, mode)
        if not best:
            continue
        for item, chunk_item in zip(group, best.items):
            filled.append((item.order, chunk_item["place"], chunk_item["stay_min"]))
            # 뒤 구간이 같은 곳을 또 고르지 않게 누적한다.
            exclude_ids.add(chunk_item["place"].content_id)

    for it in unlocked_items:
        it.delete()

    for order, place, stay_min in filled:
        ItineraryItem.objects.create(
            day=day, order=order, place=place, slot_type="GENERAL",
            arrive_at=day_start, depart_at=day_start,   # 임시값, 아래에서 재계산으로 확정
            travel_min_from_prev=0,
            # 빔서치가 고른 체류시간을 그대로 보관한다. recalc_timeline_from이
            # place.stay_time_minutes 대신 이 값을 우선 쓴다.
            stay_min=stay_min,
        )

    resequence_orders(day)
    recalc_timeline_from(day, start_order=first_unlocked_order)
    return {"ok": True, "regenerated": len(filled)}


# 식사 슬롯 교체: 시각이 시간대에 묶여 있어 장소만 갈아 끼우면 된다.

def replace_meal_place(item: ItineraryItem, purpose_main: str, purpose_sub: str,
                       mode: str, exclude_ids: set[str] | None = None) -> bool:
    """
    식사 슬롯의 장소를 다른 식당으로 바꾼다. 도착·출발 시각은 그 시간대에 고정된 값이라
    그대로 두고, 이동시간만 다시 계산한다.

    쓸 만한 후보가 없으면(권역이 좁거나 그 시각에 문 연 곳이 없으면) 아무것도 바꾸지 않고
    False를 돌려준다 — 호출부가 그냥 빼는 기존 동작으로 떨어지면 된다.
    """
    from apps.recommendation.engine import _get_cached_places
    from apps.recommendation.filters import is_open_at
    from apps.recommendation.food_scoring import (
        MEAL_CAPABLE_ROLES, build_meal_candidates, score_food_candidates,
    )

    day = item.day
    trip = day.course.trip
    routing_engine = get_routing_engine()
    matrix_ids = set(routing_engine._pos.keys())
    _, all_food_places = _get_cached_places(matrix_ids)

    # 권역은 그 날 설정이 우선이고 없으면 여행 전체 설정 — 엔진이 코스를 만들 때와 같은 규칙이다.
    schedule = next((s for s in (trip.day_schedules or []) if s.get("day_index") == day.day_index), {})
    day_region = schedule.get("region_preference") or trip.region_preference
    quadrant = None if day_region == "ALL" else day_region

    visit_at = item.arrive_at.astimezone(KST)
    candidates, relaxed_ids = build_meal_candidates(
        all_food_places, quadrant, visit_at,
        trip.food_pref_1, trip.food_pref_2, "",
        is_open_at_fn=is_open_at,
    )

    # 후보 생성기는 "이 코스가 이미 쓴 식당"을 모른다 — 중복은 여기서 걸러낸다.
    blocked = set(exclude_ids or ()) | {item.place.content_id}
    pool = [
        p for p in candidates
        if p.food_role in MEAL_CAPABLE_ROLES
        and p.content_id not in blocked
        and p.content_id in matrix_ids
    ]
    if not pool:
        return False

    prev = day.items.filter(order__lt=item.order).order_by("-order").first()
    slot_min = (item.depart_at - item.arrive_at).total_seconds() / 60

    if prev is None:
        # 그 날 첫 일정이면 기준점이 공항이라 이동시간으로 줄 세울 수 없다(엔진도 같은 처리).
        chosen_place = pool[0]
        travel_min = snap_travel_time_5min(
            estimate_airport_travel_min(chosen_place.latitude, chosen_place.longitude, DEFAULT_VEHICLE)
        )
        is_relaxed = chosen_place.content_id in relaxed_ids
    else:
        ranked = score_food_candidates(
            pool, prev.place, purpose_main, purpose_sub, mode, slot_min,
            _get_travel_time_fn(routing_engine),
            relaxed_ids=relaxed_ids, visit_datetime=visit_at,
        )
        if not ranked:
            return False
        chosen_place = ranked[0]["place"]
        travel_min = snap_travel_time_5min(ranked[0]["travel_min"])
        is_relaxed = ranked[0].get("is_relaxed", False)

    item.place = chosen_place
    item.travel_min_from_prev = travel_min
    item.is_relaxed_preference = is_relaxed
    item.save(update_fields=["place", "travel_min_from_prev", "is_relaxed_preference"])

    # regenerate_unlocked_segment는 다시 뽑을 관광지가 없으면 바로 끝나서 시각을 손대지 않는다.
    # 새 식당까지의 이동시간과 뒤 일정이 어긋나지 않게 여기서 직접 다시 맞춘다.
    recalc_timeline_from(day, start_order=max(item.order - 1, 0))
    return True
