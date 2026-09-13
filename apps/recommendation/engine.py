from __future__ import annotations
from datetime import datetime, timedelta   # ★ datetime 추가
from zoneinfo import ZoneInfo

from apps.places.models import Place
from apps.trips.models import TripRequest, RecommendedCourse, ItineraryDay, ItineraryItem
from apps.recommendation.constraints import (
    calc_avail_hours_from_schedule, calc_target_slots, estimate_airport_travel_min,
)
from apps.recommendation.course_builder import beam_search_day, select_best_course, calc_macro_score
from apps.recommendation.food_scoring import build_meal_candidates, decide_food_slot_types, score_food_candidates
from apps.recommendation.lodging_adapter import get_lodging_anchor
from apps.recommendation.nlp_matching import calc_nlp_match_scores

KST = ZoneInfo("Asia/Seoul")
MODES = ["dist", "pref", "relax"]
DEFAULT_VEHICLE = "car"
LUNCH_TARGET_MIN = 12 * 60 + 30
DINNER_TARGET_MIN = 18 * 60 + 30


def _get_travel_time_fn(routing_engine):
    def _fn(origin_id: str, destination_id: str) -> dict:
        return routing_engine.get_travel_time(origin_id, destination_id, mode="osrm", vehicle=DEFAULT_VEHICLE)
    return _fn


def _split_evenly(total: int, n_parts: int) -> list[int]:
    if n_parts <= 0:
        return [total]
    base = total // n_parts
    remainder = total % n_parts
    return [base + (1 if i < remainder else 0) for i in range(n_parts)]


def _run_general_chunk(all_general_places, start_place, chunk_avail_hours, chunk_target, mode,
                        purpose_main, purpose_sub, exclude_categories,
                        region_quadrant, visit_start_dt, get_travel_time_fn, get_stay_time_fn,
                        visited_across_days, nlp_scores):
    if chunk_target <= 0 or chunk_avail_hours <= 0:
        return None
    courses = beam_search_day(
        candidate_pool=all_general_places, start_place=start_place,
        avail_hours=chunk_avail_hours, target_slots=chunk_target, mode=mode,
        purpose_main=purpose_main, purpose_sub=purpose_sub,
        transport_mode=DEFAULT_VEHICLE, region_quadrant=region_quadrant,
        exclude_place_ids=[], exclude_categories=exclude_categories,
        visit_start_datetime=visit_start_dt,
        get_travel_time_fn=get_travel_time_fn, get_stay_time_fn=get_stay_time_fn,
        need_lunch=False, need_dinner=False, visited_across_days=visited_across_days,
    )
    return select_best_course(courses, chunk_avail_hours, mode)


def _combine_date_and_time(base_date, day_index: int, time_str: str) -> datetime:
    """start_date + (day_index-1)일 + 'HH:MM' → KST datetime 객체로 조합"""
    target_date = base_date + timedelta(days=day_index - 1)
    hour, minute = map(int, time_str.split(":"))
    return datetime(target_date.year, target_date.month, target_date.day, hour, minute, tzinfo=KST)


def generate_all_courses(trip: TripRequest, routing_engine) -> list[RecommendedCourse]:
    return [generate_one_course(trip, routing_engine, mode) for mode in MODES]


def generate_one_course(trip: TripRequest, routing_engine, mode: str) -> RecommendedCourse:
    day_schedules = sorted(trip.day_schedules, key=lambda d: d["day_index"])
    total_days = len(day_schedules)   # ★ 수정: end_kst/start_kst 없이 개수로 계산

    matrix_ids = set(routing_engine._pos.keys())
    all_general_places = list(Place.objects.exclude(content_type_name="음식점").filter(content_id__in=matrix_ids))
    all_food_places = list(Place.objects.filter(content_type_name="음식점").filter(content_id__in=matrix_ids))

    get_travel_time_fn = _get_travel_time_fn(routing_engine)
    get_stay_time_fn = lambda p: p.stay_time_minutes
    nlp_scores = calc_nlp_match_scores(trip.free_text_input)

    course = RecommendedCourse.objects.create(trip=trip, mode=mode)
    visited_across_days: set[str] = set()
    current_start_place = None
    day_last_place_ids: list[str] = []
    total_final_score = 0.0

    for schedule in day_schedules:   # ★ 수정: range() 대신 day_schedules 순회
        day_index = schedule["day_index"]

        day_purpose_main = schedule.get("purpose_main", trip.purpose_main)
        day_purpose_sub = schedule.get("purpose_sub", trip.purpose_sub)
        day_region = schedule.get("region_preference", trip.region_preference)
        day_exclude = schedule.get("exclude_categories", trip.exclude_categories)
        quadrant = None if day_region == "ALL" else day_region

        day_start_kst = _combine_date_and_time(trip.start_date, day_index, schedule["start_time"])
        day_end_kst = _combine_date_and_time(trip.start_date, day_index, schedule["end_time"])

        avail = calc_avail_hours_from_schedule(day_index, total_days, day_start_kst, day_end_kst)   # ★ 새 함수
        target_slots = calc_target_slots(avail.avail_hours, mode, avail.need_night_spot)
        visit_start_dt = day_start_kst   # ★ replace() 불필요, 이미 정확한 시각

        food_slot_types = decide_food_slot_types(
            day_purpose_main == "food" or day_purpose_sub == "food",
            avail.need_morning, avail.need_lunch, avail.need_dinner,
            avail.avail_hours, trip.food_cafe_balance,
        )
        general_target = max(target_slots - len(food_slot_types), 0)

        meal_checkpoints = []
        remaining_food_types = list(food_slot_types)
        if avail.need_lunch and remaining_food_types:
            t = max(avail.avail_start_min, min(LUNCH_TARGET_MIN, avail.avail_end_min))
            meal_checkpoints.append((t, remaining_food_types.pop(0)))
        if avail.need_dinner and remaining_food_types:
            t = max(avail.avail_start_min, min(DINNER_TARGET_MIN, avail.avail_end_min))
            meal_checkpoints.append((t, remaining_food_types.pop(0)))
        meal_checkpoints.sort(key=lambda x: x[0])
        extra_food_types = remaining_food_types

        day_obj = ItineraryDay.objects.create(
            course=course, day_index=day_index, day_case=avail.day_case,
            avail_hours=avail.avail_hours, target_slots=target_slots,
            avail_start_min=avail.avail_start_min,
            need_morning=avail.need_morning, need_lunch=avail.need_lunch,
            need_dinner=avail.need_dinner, need_night_spot=avail.need_night_spot,
        )

        current_time = visit_start_dt
        current_place = current_start_place
        order = 0
        chunk_targets = _split_evenly(general_target, len(meal_checkpoints) + 1)

        meal_candidates, relaxed_ids = build_meal_candidates(
            all_food_places, quadrant, visit_start_dt,
            trip.food_pref_1, trip.food_pref_2, "",
            is_open_at_fn=lambda p, dt: (True, True),
        )

        segment_bounds = [cp[0] for cp in meal_checkpoints] + [avail.avail_end_min]
        for seg_i, seg_end_min in enumerate(segment_bounds):
            current_minute = current_time.hour * 60 + current_time.minute
            chunk_avail_hours = max(0.0, (seg_end_min - current_minute) / 60)

            best_chunk = _run_general_chunk(
                all_general_places, current_place, chunk_avail_hours, chunk_targets[seg_i], mode,
                day_purpose_main, day_purpose_sub, day_exclude,
                quadrant, current_time, get_travel_time_fn, get_stay_time_fn,
                visited_across_days, nlp_scores,
            )
            if best_chunk:
                for item in best_chunk.items:
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
                    current_place = item["place"]
                    order += 1

            if seg_i < len(meal_checkpoints):
                slot_type = meal_checkpoints[seg_i][1]
                remain_time = avail.avail_end_min - (current_time.hour * 60 + current_time.minute)
                role_candidates = [
                    p for p in meal_candidates if p.content_id not in visited_across_days
                    and (p.food_role == slot_type or (slot_type == "RESTAURANT" and p.food_role in ("RESTAURANT", "SNACK")))
                ]
                if role_candidates:
                    ranked = score_food_candidates(
                        role_candidates, current_place, day_purpose_main, day_purpose_sub,
                        mode, remain_time, get_travel_time_fn,
                        relaxed_ids=relaxed_ids, nlp_scores=nlp_scores,
                    )
                    ranked = [r for r in ranked if r["travel_min"] + r["stay_min"] <= remain_time]
                    if ranked:
                        chosen = ranked[0]
                        current_time += timedelta(minutes=chosen["travel_min"])
                        arrive = current_time
                        current_time += timedelta(minutes=chosen["stay_min"])
                        depart = current_time
                        ItineraryItem.objects.create(
                            day=day_obj, order=order, place=chosen["place"], slot_type=slot_type,
                            arrive_at=arrive, depart_at=depart, travel_min_from_prev=chosen["travel_min"],
                            is_relaxed_preference=chosen.get("is_relaxed", False),
                        )
                        visited_across_days.add(chosen["place"].content_id)
                        current_place = chosen["place"]
                        order += 1

        for slot_type in extra_food_types:
            remain_time = avail.avail_end_min - (current_time.hour * 60 + current_time.minute)
            if remain_time <= 0:
                continue
            role_candidates = [
                p for p in meal_candidates if p.content_id not in visited_across_days
                and (p.food_role == slot_type or (slot_type == "RESTAURANT" and p.food_role in ("RESTAURANT", "SNACK")))
            ]
            if not role_candidates:
                continue
            ranked = score_food_candidates(
                role_candidates, current_place, day_purpose_main, day_purpose_sub,
                mode, remain_time, get_travel_time_fn,
                relaxed_ids=relaxed_ids, nlp_scores=nlp_scores,
            )
            ranked = [r for r in ranked if r["travel_min"] + r["stay_min"] <= remain_time]
            if not ranked:
                continue
            chosen = ranked[0]
            current_time += timedelta(minutes=chosen["travel_min"])
            arrive = current_time
            current_time += timedelta(minutes=chosen["stay_min"])
            depart = current_time
            ItineraryItem.objects.create(
                day=day_obj, order=order, place=chosen["place"], slot_type=slot_type,
                arrive_at=arrive, depart_at=depart, travel_min_from_prev=chosen["travel_min"],
                is_relaxed_preference=chosen.get("is_relaxed", False),
            )
            visited_across_days.add(chosen["place"].content_id)
            current_place = chosen["place"]
            order += 1

        if current_place and day_index < total_days:
            day_last_place_ids.append(current_place.content_id)
        current_start_place = None

        macro_result = calc_macro_score(
            type("obj", (), {"items": [
                {"micro_score": 0.5, "travel_min": 0, "stay_min": 0} for _ in range(order)
            ]})(),
            avail.avail_hours * 60, mode,
        ) if order else {}
        total_final_score += macro_result.get("final_score", 0)

    expected_nights = total_days - 1
    if len(day_last_place_ids) == expected_nights and day_last_place_ids:
        lodging_cards = get_lodging_anchor(trip, day_last_place_ids)
    else:
        lodging_cards = []

    for day in course.days.exclude(day_index=total_days):
        day.lodging_options_snapshot = lodging_cards
        day.save(update_fields=["lodging_options_snapshot"])

    course.final_score = total_final_score / total_days if total_days else 0
    course.save(update_fields=["final_score"])
    return course