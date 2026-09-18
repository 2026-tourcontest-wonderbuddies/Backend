from __future__ import annotations
from datetime import datetime, timedelta   # ★ datetime 추가
from zoneinfo import ZoneInfo

from apps.places.models import Place
from apps.trips.models import TripRequest, RecommendedCourse, ItineraryDay, ItineraryItem
from apps.recommendation.constraints import (
    calc_avail_hours_from_schedule, calc_target_slots, estimate_airport_travel_min,
    snap_travel_time_5min, get_meal_windows
)
from apps.recommendation.course_builder import beam_search_day, select_best_course, calc_macro_score
from apps.recommendation.food_scoring import build_meal_candidates, decide_food_slot_types, score_food_candidates
from apps.recommendation.lodging_adapter import get_lodging_anchor
from apps.recommendation.nlp_matching import calc_nlp_match_scores
from apps.recommendation.filters import is_open_at

KST = ZoneInfo("Asia/Seoul")
MODES = ["dist", "pref", "relax"]
DEFAULT_VEHICLE = "car"

MORNING_TARGET_MIN = 8 * 60 
LUNCH_TARGET_MIN = 12 * 60
DINNER_TARGET_MIN = 19 * 60

MIN_LEFTOVER_TO_RECORD = 20


def _get_travel_time_fn(routing_engine):
    def _fn(origin_id: str, destination_id: str, depart_at: datetime | None = None) -> dict:
        return routing_engine.get_travel_time(
            origin_id, destination_id, mode="osrm", vehicle=DEFAULT_VEHICLE, depart_at=depart_at
        )
    return _fn


def _split_evenly(total: int, n_parts: int) -> list[int]:
    if n_parts <= 0:
        return [total]
    base = total // n_parts
    remainder = total % n_parts
    return [base + (1 if i < remainder else 0) for i in range(n_parts)]


def _split_by_time_ratio(total: int, segment_minutes: list[int]) -> list[int]:
    """
    total(관광지 목표 개수)을 각 구간의 실제 시간 길이 비율로 분배.
    구간이 길수록 더 많이, 짧을수록 더 적게 배정된다.
    실제 채워지는 개수는 뒤이은 beam_search_day의 시간예산 체크가 다시 검증한다.
    """
    total_minutes = sum(segment_minutes)
    if total_minutes <= 0 or total <= 0:
        return [0] * len(segment_minutes)

    raw = [total * (m / total_minutes) for m in segment_minutes]
    result = [int(r) for r in raw]  # 내림
    remainder = total - sum(result)

    order_by_frac = sorted(range(len(raw)), key=lambda i: raw[i] - result[i], reverse=True)
    for i in range(remainder):
        result[order_by_frac[i % len(order_by_frac)]] += 1
    return result


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
    target_date = base_date + timedelta(days=day_index - 1)
    hour, minute = map(int, time_str.split(":"))
    return datetime(target_date.year, target_date.month, target_date.day, hour, minute, tzinfo=KST)


def generate_all_courses(trip: TripRequest, routing_engine) -> list[RecommendedCourse]:
    return [generate_one_course(trip, routing_engine, mode) for mode in MODES]


def generate_one_course(trip: TripRequest, routing_engine, mode: str) -> RecommendedCourse:
    day_schedules = sorted(trip.day_schedules, key=lambda d: d["day_index"])
    total_days = len(day_schedules)

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

    for schedule in day_schedules:
        day_index = schedule["day_index"]

        day_purpose_main = schedule.get("purpose_main", trip.purpose_main)
        day_purpose_sub = schedule.get("purpose_sub", trip.purpose_sub)
        day_region = schedule.get("region_preference", trip.region_preference)
        day_exclude = schedule.get("exclude_categories", trip.exclude_categories)
        quadrant = None if day_region == "ALL" else day_region

        day_start_kst = _combine_date_and_time(trip.start_date, day_index, schedule["start_time"])
        day_end_kst = _combine_date_and_time(trip.start_date, day_index, schedule["end_time"])

        avail = calc_avail_hours_from_schedule(day_index, total_days, day_start_kst, day_end_kst)

        meal_windows = get_meal_windows(avail.avail_start_min, avail.avail_end_min)
        meal_segments = sorted(
            [(name, w) for name, w in meal_windows.items() if w is not None],
            key=lambda x: x[1][0]
        )
        meal_count = len(meal_segments)

        target_slots = calc_target_slots(avail.avail_hours, mode, avail.need_night_spot)
        general_target = max(target_slots - meal_count, 0)

        # 관광구간 경계 계산 (식사 구간을 뺀 나머지)
        boundaries = [avail.avail_start_min]
        for name, (w_start, w_end) in meal_segments:
            boundaries.append(w_start)
            boundaries.append(w_end)
        boundaries.append(avail.avail_end_min)

        tour_segments = []
        for i in range(0, len(boundaries) - 1, 2):
            seg_start, seg_end = boundaries[i], boundaries[i + 1]
            if seg_end > seg_start:
                tour_segments.append((seg_start, seg_end))

        # 여기는 기존 방식 유지 (시간비율 아님, 균등분배 그대로)
        segment_minutes = [end - start for start, end in tour_segments]   # ★ 각 구간의 실제 길이(분) 계산
        general_chunk_targets = _split_by_time_ratio(general_target, segment_minutes) if tour_segments else []
        
        # 이벤트 목록 조립: tour/meal 시간순 교차
        events = []
        tour_i = 0
        for i in range(0, len(boundaries) - 1, 2):
            seg_start, seg_end = boundaries[i], boundaries[i + 1]
            if seg_end > seg_start:
                events.append(("tour", seg_start, seg_end, general_chunk_targets[tour_i]))
                tour_i += 1
            meal_idx = i // 2
            if meal_idx < len(meal_segments):
                name, (w_start, w_end) = meal_segments[meal_idx]
                events.append(("meal", name, w_start, w_end))

        day_obj = ItineraryDay.objects.create(
            course=course, day_index=day_index, day_case=avail.day_case,
            avail_hours=avail.avail_hours, target_slots=target_slots,
            avail_start_min=avail.avail_start_min,
            need_morning=avail.need_morning, need_lunch=avail.need_lunch,
            need_dinner=avail.need_dinner, need_night_spot=avail.need_night_spot,
        )

        meal_candidates, relaxed_ids = build_meal_candidates(
            all_food_places, quadrant, day_start_kst,
            trip.food_pref_1, trip.food_pref_2, "",
            is_open_at_fn=is_open_at,
        )

        current_place = current_start_place
        order = 0
        
        for event in events:
            if event[0] == "tour":
                _, seg_start, seg_end, chunk_target = event
                seg_start_dt = day_start_kst.replace(hour=seg_start // 60, minute=seg_start % 60)
                chunk_avail_hours = max(0.0, (seg_end - seg_start) / 60)

                best_chunk = _run_general_chunk(
                    all_general_places, current_place, chunk_avail_hours, chunk_target, mode,
                    day_purpose_main, day_purpose_sub, day_exclude,
                    quadrant, seg_start_dt, get_travel_time_fn, get_stay_time_fn,
                    visited_across_days, nlp_scores,
                )
                current_time = seg_start_dt
                if best_chunk:
                    for item in best_chunk.items:
                        if order == 0:
                            travel_min = snap_travel_time_5min(
                                estimate_airport_travel_min(item["place"].latitude, item["place"].longitude, DEFAULT_VEHICLE)
                            )
                        else:
                            travel_min = snap_travel_time_5min(item["travel_min"])
                        current_time += timedelta(minutes=travel_min)
                        arrive = current_time
                        current_time += timedelta(minutes=item["stay_min"])
                        depart = current_time
                        ItineraryItem.objects.create(
                            day=day_obj, order=order, place=item["place"], slot_type="GENERAL",
                            arrive_at=arrive, depart_at=depart, travel_min_from_prev=travel_min,
                            hours_uncertain=item.get("hours_uncertain", False),
                        )
                        visited_across_days.add(item["place"].content_id)
                        current_place = item["place"]
                        order += 1

            else:  # "meal" — ★ 시간대 강제 고정
                _, meal_name, w_start, w_end = event
                slot_duration_min = w_end - w_start   # ★ stay_time_minutes 무시, 겹친 폭 그대로
                meal_start_dt = day_start_kst.replace(hour=w_start // 60, minute=w_start % 60)

                role_candidates = [
                    p for p in meal_candidates
                    if p.content_id not in visited_across_days and p.food_role in ("RESTAURANT", "SNACK")
                ]
                if not role_candidates:
                    continue

                if order == 0 or current_place is None:
                    chosen_place = role_candidates[0]
                    travel_min = snap_travel_time_5min(
                        estimate_airport_travel_min(chosen_place.latitude, chosen_place.longitude, DEFAULT_VEHICLE)
                    )
                    is_relaxed = False
                else:
                    ranked = score_food_candidates(
                        role_candidates, current_place, day_purpose_main, day_purpose_sub,
                        mode, slot_duration_min, get_travel_time_fn,
                        relaxed_ids=relaxed_ids, nlp_scores=nlp_scores, visit_datetime=meal_start_dt,
                    )
                    if not ranked:
                        continue
                    chosen = ranked[0]
                    chosen_place = chosen["place"]
                    travel_min = snap_travel_time_5min(chosen["travel_min"])
                    is_relaxed = chosen.get("is_relaxed", False)

                arrive = meal_start_dt   # ★ 이동시간과 무관하게 시간대 시작에 정확히 고정
                depart = meal_start_dt + timedelta(minutes=slot_duration_min)

                ItineraryItem.objects.create(
                    day=day_obj, order=order, place=chosen_place, slot_type="RESTAURANT",
                    arrive_at=arrive, depart_at=depart, travel_min_from_prev=travel_min,
                    is_relaxed_preference=is_relaxed,
                )
                visited_across_days.add(chosen_place.content_id)
                current_place = chosen_place
                order += 1

            # for slot_type in extra_food_types:
            #     remain_time = avail.avail_end_min - (current_time.hour * 60 + current_time.minute)
            #     if remain_time <= 0:
            #         continue
            #     role_candidates = [
            #         p for p in meal_candidates if p.content_id not in visited_across_days
            #         and (p.food_role == slot_type or (slot_type == "RESTAURANT" and p.food_role in ("RESTAURANT", "SNACK")))
            #     ]
            #     if not role_candidates:
            #         continue
            #     ranked = score_food_candidates(
            #         role_candidates, current_place, day_purpose_main, day_purpose_sub,
            #         mode, remain_time, get_travel_time_fn,
            #         relaxed_ids=relaxed_ids, nlp_scores=nlp_scores,
            #     )
            #     ranked = [r for r in ranked if r["travel_min"] + r["stay_min"] <= remain_time]
            #     if not ranked:
            #         continue
            #     chosen = ranked[0]
            #     travel_min = snap_travel_time_5min(chosen["travel_min"])
            #     current_time += timedelta(minutes=travel_min)
            #     arrive = current_time
            #     current_time += timedelta(minutes=chosen["stay_min"])
            #     depart = current_time
            #     ItineraryItem.objects.create(
            #         day=day_obj, order=order, place=chosen["place"], slot_type=slot_type,
            #         arrive_at=arrive, depart_at=depart, travel_min_from_prev=travel_min,
            #         is_relaxed_preference=chosen.get("is_relaxed", False),
            #     )
            #     visited_across_days.add(chosen["place"].content_id)
            #     current_place = chosen["place"]
            #     order += 1

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

