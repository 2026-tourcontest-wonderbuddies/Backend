"""
전체 파이프라인 진입점. constraints -> filters/scoring -> course_builder를 
Day별로 순서대로 호출하고 음식 슬록까지 병합하여 최종 InteraryDay/Item을 만든다.
"""

from datetime import datetime, timedelta
from apps.places.models import Place, Lodging
from apps.trips.models import TripRequest, InineraryDay, ItineraryItem
from apps.recommendation.constraints import calc_avail_hours, calc_target_slots
from apps.recommendation.course_builder import beam_search_day, select_best_course
from apps.recommendation.food_scoring import (
    build_meal_candidates, decide_food_slot_types, is_meal_place,
)

def _get_travel_time_fn(routing_engine, transport_mode: str):
    """
    routing_engine.get_travel_time()을 filters.py가 기대하는 시그니처로 감싼다.
    transport_mode가 rental_car/own_car면 vehicle="car"로 매핑 (routing 엔진 인터페이스 기준).
    """
    vehicle = "car" if transport_mode in ("rental_car", "own_car") else "taxi"

    def _fn(origin_id: str, destination_id: str) -> dict:
        return routing_engine.get_travel_time(origin_id, destination_id, mode="osrm", vehicle=vehicle)

    return _fn


def _get_stay_time_fn():
    """Place.stay_time_minutes를 filters.py가 기대하는 형태로 감싼다."""
    def _fn(place: Place) -> float:
        return place.stay_time_minutes
    return _fn


def generate_itinerary(trip: TripRequest, routing_engine) -> list[ItineraryDay]:
    """
    이 함수 하나가 "입력 → 코스 완성"의 전체 흐름이다.
    trip.start_datetime ~ trip.end_datetime 사이 일수를 계산해서 Day별로 반복 실행한다.
    """
    total_days = (trip.end_datetime.date() - trip.start_datetime.date()).days + 1
    quadrant = trip.region_preference  # TripRequest.region_preference에 "NE"/"NW"/"SE"/"SW" 저장 가정

    # 후보 풀: 희망권역 필터는 course_builder 내부(filters.py)에서 처리하므로
    # 여기서는 전체 Place를 넘긴다. 음식점은 별도로 관리(food_scoring 전용 흐름).
    all_general_places = list(Place.objects.exclude(content_type_name="음식점"))
    all_food_places = list(Place.objects.filter(content_type_name="음식점"))

    get_travel_time_fn = _get_travel_time_fn(routing_engine, trip.transport_mode)
    get_stay_time_fn = _get_stay_time_fn()

    result_days = []
    visited_across_days: set[str] = set()   # Day1 결과가 Day2 후보에서 제외되도록 누적

    for day_index in range(1, total_days + 1):
        avail = calc_avail_hours(day_index, total_days, trip.start_datetime, trip.end_datetime)
        target_slots = calc_target_slots(avail.avail_hours, trip.course_priority, avail.need_night_spot)

        visit_start_dt = trip.start_datetime.replace(
            hour=avail.avail_start_min // 60, minute=avail.avail_start_min % 60
        )

        # ── 음식 슬롯 유형 결정 (몇 곳을 RESTAURANT/CAFE/SNACK으로 채울지) ──
        purpose_selected = trip.purpose_main == "food" or trip.purpose_sub == "food"
        food_slot_types = decide_food_slot_types(
            purpose_selected, avail.need_lunch, avail.need_dinner,
            avail.avail_hours, trip.food_cafe_balance,
        )

        # ── 일반 장소 빔서치 (음식 슬롯 개수만큼은 목표 슬롯에서 제외하고 진행) ──
        general_target = max(target_slots - len(food_slot_types), 0)

        courses = beam_search_day(
            candidate_pool=all_general_places,
            start_place=None,  # 숙소 좌표 연동은 lodging_matcher 이후 보강 예정
            avail_hours=avail.avail_hours,
            target_slots=general_target,
            mode=trip.course_priority,
            purpose_main=trip.purpose_main,
            purpose_sub=trip.purpose_sub,
            transport_mode=trip.transport_mode,
            region_quadrant=quadrant,
            exclude_place_ids=trip.exclude_places,
            exclude_categories=trip.exclude_categories,
            visit_start_datetime=visit_start_dt,
            get_travel_time_fn=get_travel_time_fn,
            get_stay_time_fn=get_stay_time_fn,
            need_lunch=False,   # 식사는 별도 음식슬롯에서 처리하므로 일반 빔서치는 False 고정
            need_dinner=False,
            visited_across_days=visited_across_days,
        )
        best_course = select_best_course(courses, avail.avail_hours, trip.course_priority)

        # ── 음식 후보 준비 (소프트필터 포함) ──
        meal_candidates, relaxed_ids = build_meal_candidates(
            all_food_places, quadrant, visit_start_dt,
            trip.food_pref_1, trip.food_pref_2, trip.food_restriction,
            is_open_at_fn=lambda p, dt: (True, True),  # hours_status 캐시 도착 전 임시 통과
        )

        # ── ItineraryDay 저장 ──
        day_obj = ItineraryDay.objects.create(
            trip=trip, day_index=day_index, day_case=avail.day_case,
            avail_hours=avail.avail_hours, target_slots=target_slots,
            need_lunch=avail.need_lunch, need_dinner=avail.need_dinner,
            need_night_spot=avail.need_night_spot,
        )

        current_time = visit_start_dt
        order = 0

        if best_course:
            for item in best_course.items:
                current_time += timedelta(minutes=item["travel_min"])
                arrive = current_time
                current_time += timedelta(minutes=item["stay_min"])
                depart = current_time

                ItineraryItem.objects.create(
                    day=day_obj, order=order, place=item["place"],
                    slot_type="GENERAL", arrive_at=arrive, depart_at=depart,
                    travel_min_from_prev=item["travel_min"],
                )
                visited_across_days.add(item["place"].content_id)
                order += 1

        # ── 음식 슬롯 채우기 (단순 배치: 필요시 course_builder처럼 이동시간 고려한
        #    빔서치로 고도화 가능하나, 시간 제약상 우선 그리디로 근사) ──
        for slot_type in food_slot_types:
            candidates_by_role = [p for p in meal_candidates if p.food_role == slot_type or
                                   (slot_type == "RESTAURANT" and is_meal_place(p))]
            candidates_by_role = [p for p in candidates_by_role if p.content_id not in visited_across_days]
            if not candidates_by_role:
                continue
            chosen = candidates_by_role[0]  # TODO: Pref_food 점수순 정렬 후 최고점 선택으로 고도화

            travel_min = 15  # 임시값 — routing 엔진 연동 필요 (숙소/직전장소 기준)
            current_time += timedelta(minutes=travel_min)
            arrive = current_time
            current_time += timedelta(minutes=chosen.stay_time_minutes)
            depart = current_time

            ItineraryItem.objects.create(
                day=day_obj, order=order, place=chosen, slot_type=slot_type,
                arrive_at=arrive, depart_at=depart, travel_min_from_prev=travel_min,
            )
            visited_across_days.add(chosen.content_id)
            order += 1

        result_days.append(day_obj)

    return result_days