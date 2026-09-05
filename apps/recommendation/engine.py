"""
1. 모드 하나가 아니라 3개(dist/pref/relax)를 전부 생성 → RecommendedCourse 3개 저장.
2. Day1 출발지 = trip.departure_place_id (사용자가 입력한 Place).
   Day2 이후 출발지 = 전날 자동 선택된(top1) 숙소 — "사용자가 무조건 하나 선택한다"는
   전제로, 알고리즘 계산 시점엔 top1을 임시 확정해서 이어감. 실제 화면에서 사용자가
   다른 숙소를 고르면 그 지점부터 재계산이 필요함(수정 로직에서 처리).
3. 음식 슬롯은 이제 food_scoring.score_food_candidates()로 순위 매겨서 1등 선택.
"""
from __future__ import annotations
from datetime import timedelta
from apps.places.models import Place
from apps.trips.models import TripRequest, RecommendedCourse, ItineraryDay, ItineraryItem
from apps.recommendation.constraints import calc_avail_hours, calc_target_slots
from apps.recommendation.course_builder import beam_search_day, select_best_course, calc_macro_score
from apps.recommendation.food_scoring import build_meal_candidates, decide_food_slot_types
from apps.recommendation.food_scoring import score_food_candidates
from apps.recommendation.lodging_matcher import match_lodging_for_day
from apps.recommendation.lodging_adapter import get_lodging_anchor


MODES = ["dist", "pref", "relax"]


def _get_travel_time_fn(routing_engine, transport_mode: str):
    vehicle = "car" if transport_mode in ("rental_car", "own_car") else "taxi"

    def _fn(origin_id: str, destination_id: str) -> dict:
        return routing_engine.get_travel_time(origin_id, destination_id, mode="osrm", vehicle=vehicle)
    return _fn


def generate_all_courses(trip: TripRequest, routing_engine) -> list[RecommendedCourse]:
    """
    3개 모드 각각에 대해 generate_one_course()를 호출한다.
    API 뷰에서는 이 함수 하나만 부르면 됨.
    """
    results = []
    for mode in MODES:
        course = generate_one_course(trip, routing_engine, mode)
        results.append(course)
    return results


# 단일 코스 생성
def generate_one_course(trip: TripRequest, routing_engine, mode: str) -> RecommendedCourse:
    """모드 하나짜리 코스를 끝까지 생성해서 RecommendedCourse로 저장."""
    # 총 여행 일수
    total_days = (trip.end_datetime.date() - trip.start_datetime.date()).days + 1
    quadrant = trip.region_preference

    # 관광지/쇼핑/문화시설
    all_general_places = list(Place.objects.filter(content_type_name__in=["관광지", "문화시설", "쇼핑"]))    # 음식점
    all_food_places = list(Place.objects.filter(content_type_name="음식점"))

    get_travel_time_fn = _get_travel_time_fn(routing_engine, trip.transport_mode)
    get_stay_time_fn = lambda p: p.stay_time_minutes

    course = RecommendedCourse.objects.get_or_create(trip=trip, mode=mode)

    visited_across_days: set[str] = set()

    # ★ Day1 시작 장소 확정
    try:
        # 출/도착지는 제주공항으로 고정 -> 제주공항으로 바꿔야함
        current_start_place = Place.objects.get(content_id=trip.departure_place_id)
    except Place.DoesNotExist:
        current_start_place = None  # 출발지 미입력/미매칭 시 좌표 없이 시작 (첫 이동시간 0 처리됨)

    total_final_score = 0.0

    for day_index in range(1, total_days + 1):
        avail = calc_avail_hours(day_index, total_days, trip.start_datetime, trip.end_datetime)
        target_slots = calc_target_slots(avail.avail_hours, mode, avail.need_night_spot)
        visit_start_dt = trip.start_datetime.replace(
            hour=avail.avail_start_min // 60, minute=avail.avail_start_min % 60
        )

        purpose_selected = trip.purpose_main == "food" or trip.purpose_sub == "food"
        food_slot_types = decide_food_slot_types(
            purpose_selected, avail.need_lunch, avail.need_dinner, avail.avail_hours, trip.food_cafe_balance,
        )
        general_target = max(target_slots - len(food_slot_types), 0)

        courses = beam_search_day(
            candidate_pool=all_general_places,
            start_place=current_start_place,   # ★ Day1=출발지, Day2+=전날 숙소
            avail_hours=avail.avail_hours, target_slots=general_target, mode=mode,
            purpose_main=trip.purpose_main, purpose_sub=trip.purpose_sub,
            transport_mode=trip.transport_mode, region_quadrant=quadrant,
            exclude_place_ids=trip.exclude_places, exclude_categories=trip.exclude_categories,
            visit_start_datetime=visit_start_dt,
            get_travel_time_fn=get_travel_time_fn, get_stay_time_fn=get_stay_time_fn,
            need_lunch=False, need_dinner=False,
            visited_across_days=visited_across_days,
        )
        best_course = select_best_course(courses, avail.avail_hours, mode)
        macro_result = calc_macro_score(best_course, avail.avail_hours * 60, mode) if best_course else {}
        total_final_score += macro_result.get("final_score", 0)

        day_obj = ItineraryDay.objects.create(
            course=course, day_index=day_index, day_case=avail.day_case,
            avail_hours=avail.avail_hours, target_slots=target_slots,
            need_lunch=avail.need_lunch, need_dinner=avail.need_dinner, need_night_spot=avail.need_night_spot,
        )

        current_time = visit_start_dt
        order = 0
        last_place = current_start_place

        if best_course:
            for item in best_course.items:
                current_time += timedelta(minutes=item["travel_min"])
                arrive = current_time
                current_time += timedelta(minutes=item["stay_min"])
                depart = current_time

                ItineraryItem.objects.create(
                    day=day_obj, order=order, place=item["place"], slot_type="GENERAL",
                    arrive_at=arrive, depart_at=depart, travel_min_from_prev=item["travel_min"],
                    hours_uncertain=item.get("hours_uncertain", False),
                )
                visited_across_days.add(item["place"].content_id)
                last_place = item["place"]
                order += 1

        # ★ 음식 슬롯: score_food_candidates()로 순위 매겨서 1등 선택
        meal_candidates, relaxed_ids = build_meal_candidates(
            all_food_places, quadrant, visit_start_dt,
            trip.food_pref_1, trip.food_pref_2, trip.food_restriction,
            is_open_at_fn=lambda p, dt: (True, True),
        )
        for slot_type in food_slot_types:
            role_candidates = [
                p for p in meal_candidates
                if p.content_id not in visited_across_days
                and (p.food_role == slot_type or (slot_type == "RESTAURANT" and p.food_role in ("RESTAURANT", "SNACK")))
            ]
            if not role_candidates:
                continue

            remain_time = avail.avail_hours * 60 - (current_time - visit_start_dt).total_seconds() / 60
            ranked = score_food_candidates(
                role_candidates, last_place, trip.purpose_main, trip.purpose_sub,
                mode, remain_time, get_travel_time_fn,
            )
            if not ranked:
                continue
            chosen = ranked[0]  # ★ 1등 선택

            current_time += timedelta(minutes=chosen["travel_min"])
            arrive = current_time
            current_time += timedelta(minutes=chosen["stay_min"])
            depart = current_time

            ItineraryItem.objects.create(
                day=day_obj, order=order, place=chosen["place"], slot_type=slot_type,
                arrive_at=arrive, depart_at=depart, travel_min_from_prev=chosen["travel_min"],
            )
            visited_across_days.add(chosen["place"].content_id)
            last_place = chosen["place"]
            order += 1

    current_start_place = last_place

    # ★ [신규 추가] 모든 Day의 코스 생성이 끝난 뒤, 여행 전체 앵커를 딱 한 번만 호출합니다.
    day_last_place_ids = [
        day.items.last().place.content_id
        for day in course.days.all() if day.items.exists()
    ]
    
    # 숙소 어댑터를 통해 여행 전체 앵커 카드(리스트)를 가져옴
    lodging_cards = get_lodging_anchor(trip, day_last_place_ids)

    # 마지막 날을 제외한 모든 Day에 숙소 스냅샷 반영
    for day in course.days.exclude(day_index=total_days):
        day.lodging_options_snapshot = lodging_cards
        day.lodging_snapshot = lodging_cards[0] if lodging_cards else None
        day.save(update_fields=["lodging_options_snapshot", "lodging_snapshot"])

    course.final_score = total_final_score / total_days
    course.save(update_fields=["final_score"])
    return course