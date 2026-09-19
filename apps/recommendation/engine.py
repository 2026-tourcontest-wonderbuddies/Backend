from __future__ import annotations
from datetime import datetime, timedelta   # ★ datetime 추가
from zoneinfo import ZoneInfo

from apps.places.models import Place
from apps.trips.models import TripRequest, RecommendedCourse, ItineraryDay, ItineraryItem
from apps.recommendation.constraints import (
    calc_avail_hours_from_schedule, calc_target_slots, estimate_airport_travel_min,
    snap_travel_time_5min, get_meal_windows, ADJACENT_QUADRANTS
)
from apps.recommendation.course_builder import (
    beam_search_day, select_best_course, calc_macro_score, POPULAR_QUOTA_RATIO,
)
from apps.recommendation.food_scoring import build_meal_candidates, decide_food_slot_types, score_food_candidates
from apps.recommendation.lodging_adapter import get_lodging_anchor
from apps.recommendation.nlp_matching import calc_nlp_match_scores
from apps.recommendation.filters import is_open_at
from apps.recommendation.scoring import is_popular_place
import math
import time

KST = ZoneInfo("Asia/Seoul")
MODES = ["dist", "pref", "relax"]
DEFAULT_VEHICLE = "car"

MORNING_TARGET_MIN = 8 * 60
LUNCH_TARGET_MIN = 12 * 60
DINNER_TARGET_MIN = 19 * 60

MIN_LEFTOVER_TO_RECORD = 20
NIGHT_TAIL_MIN = 60   # 야간 명소 뒤에 이만큼 이상 남으면 일반 후보로 더 채운다

# 캐싱
_place_cache = {"general": None, "food": None}

def _get_cached_places(matrix_ids):
    if _place_cache["general"] is None:   # 딱 한 번만 확인, TTL 없음
        _place_cache["general"] = list(
            Place.objects.exclude(content_type_name="음식점")
            .filter(content_id__in=matrix_ids)
            .defer("overview")
        )
        _place_cache["food"] = list(
            Place.objects.filter(content_type_name="음식점")
            .filter(content_id__in=matrix_ids)
            .defer("overview")
        )
    return _place_cache["general"], _place_cache["food"]


def _night_pool(all_general_places, quadrant, visited):
    """야간 명소 후보. 사용자 권역 → (비면) 인접 권역 순. 그래도 없으면 빈 리스트."""
    night = [p for p in all_general_places if p.is_night_spot and p.content_id not in visited]
    if not quadrant:
        return night
    for allowed in ({quadrant}, {quadrant} | ADJACENT_QUADRANTS.get(quadrant, set())):
        pool = [p for p in night if p.quadrant in allowed]
        if pool:
            return pool
    return []


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
                        visited_across_days, nlp_scores, popular_quota=0):
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
        popular_quota=popular_quota,
    )
    return select_best_course(courses, chunk_avail_hours, mode)


def _combine_date_and_time(base_date, day_index: int, time_str: str) -> datetime:
    target_date = base_date + timedelta(days=day_index - 1)
    hour, minute = map(int, time_str.split(":"))
    return datetime(target_date.year, target_date.month, target_date.day, hour, minute, tzinfo=KST)


def _replay_day_timeline(day_obj, get_travel_time_fn) -> None:
    """
    인기 맛집 교체로 바뀐 장소 기준으로 이동시간·도착/출발 시각을 하루 처음부터 다시 흘려보낸다.

    RESTAURANT 슬롯은 시간대가 고정이라 도착/출발 시각 자체는 절대 안 바뀐다(그 지점에서
    drift가 리셋됨) — travel_min_from_prev만 다시 계산한다. GENERAL 슬롯은 원래 체류시간
    (depart-arrive)은 그대로 유지한 채, 직전 아이템 출발시각 + 새 이동시간으로 도착시각을
    다시 계산해서 이어붙인다. 그래서 실제로 값이 밀리는 범위는 "교체 지점부터 다음 식사
    전까지"로 자연히 국한된다.
    """
    items = list(day_obj.items.select_related("place").order_by("order"))
    if not items:
        return

    # 하루의 첫 아이템이 GENERAL이면, 그 기준 출발시각(세그먼트 시작)은 장소가 바뀌어도
    # 변하지 않는 값이므로 현재 저장된 값에서 역산해 앵커로 재사용한다.
    first = items[0]
    day_anchor = (
        first.arrive_at - timedelta(minutes=first.travel_min_from_prev)
        if first.slot_type == "GENERAL" else None
    )

    prev_place_id = None
    current_time = None
    updates = []

    for item in items:
        if prev_place_id is None:
            travel_min = snap_travel_time_5min(
                estimate_airport_travel_min(item.place.latitude, item.place.longitude, DEFAULT_VEHICLE)
            )
        else:
            travel_min = snap_travel_time_5min(
                get_travel_time_fn(prev_place_id, item.place_id)["duration_min_adjusted"]
            )

        if item.slot_type == "RESTAURANT":
            new_arrive, new_depart = item.arrive_at, item.depart_at   # 시간대 고정 — 안 바뀜
        else:
            stay_duration = item.depart_at - item.arrive_at   # 원래 체류시간은 유지
            base_time = current_time if current_time is not None else day_anchor
            new_arrive = base_time + timedelta(minutes=travel_min)
            new_depart = new_arrive + stay_duration

        if (item.travel_min_from_prev, item.arrive_at, item.depart_at) != (travel_min, new_arrive, new_depart):
            item.travel_min_from_prev = travel_min
            item.arrive_at = new_arrive
            item.depart_at = new_depart
            updates.append(item)

        current_time = new_depart
        prev_place_id = item.place_id

    if updates:
        ItineraryItem.objects.bulk_update(updates, ["travel_min_from_prev", "arrive_at", "depart_at"])


def generate_all_courses(trip: TripRequest, routing_engine) -> list[RecommendedCourse]:
    return [generate_one_course(trip, routing_engine, mode) for mode in MODES]


def generate_one_course(trip: TripRequest, routing_engine, mode: str) -> RecommendedCourse:
    t_start = time.time()

    nlp_scores = calc_nlp_match_scores(trip.free_text_input)
    with open("debug_log.txt", "a") as f:
        f.write(f"[{mode}] nlp_scores 계산: {time.time()-t_start:.2f}초\n")

    day_schedules = sorted(trip.day_schedules, key=lambda d: d["day_index"])
    total_days = len(day_schedules)

    matrix_ids = set(routing_engine._pos.keys())

    # 매번 DB조회 X, 캐시 사용
    all_general_places, all_food_places = _get_cached_places(matrix_ids)

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

        # 야간 후보가 없으면 야간 슬롯(+1)도 만들지 않는다.
        night_pool = _night_pool(all_general_places, quadrant, visited_across_days) if avail.need_night_spot else []
        need_night = bool(night_pool)
        target_slots = calc_target_slots(avail.avail_hours, mode, need_night)
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

        segment_minutes = [end - start for start, end in tour_segments]   # ★ 각 구간의 실제 길이(분) 계산
        general_chunk_targets = _split_by_time_ratio(general_target, segment_minutes) if tour_segments else []

        # 인기 장소 쿼터: 일자별 관광 슬롯수(general_target)의 50%를
        # 일반(관광지) 슬롯 안에서 강제 확보한다. 구간 배분은 관광 목표 개수와 동일하게
        # 시간 비율로 나누고, 각 구간의 실제 목표 개수를 넘지 않도록 캡을 건다.
        popular_quota_total = round(general_target * POPULAR_QUOTA_RATIO)
        general_popular_quotas = _split_by_time_ratio(popular_quota_total, segment_minutes) if tour_segments else []
        general_popular_quotas = [min(q, t) for q, t in zip(general_popular_quotas, general_chunk_targets)]

        # 음식 슬롯도 동일한 방식으로 인기 맛집 쿼터 적용 (일자별 식사 슬롯 수의 50%).
        # round()면 식사가 1~2개인 흔한 케이스가 전부 0으로 내림돼 쿼터가 무력화되므로,
        # 식사가 1개 이상이면 최소 1곳은 보장되도록 ceil(올림) 사용 — 팀 합의 사항.
        food_popular_quota = math.ceil(meal_count * POPULAR_QUOTA_RATIO)

        # 이벤트 목록 조립: tour/meal 시간순 교차
        events = []
        tour_i = 0
        for i in range(0, len(boundaries) - 1, 2):
            seg_start, seg_end = boundaries[i], boundaries[i + 1]
            if seg_end > seg_start:
                events.append((
                    "tour", seg_start, seg_end,
                    general_chunk_targets[tour_i], general_popular_quotas[tour_i],
                ))
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
            need_dinner=avail.need_dinner, need_night_spot=need_night,
        )

        meal_candidates, relaxed_ids = build_meal_candidates(
            all_food_places, quadrant, day_start_kst,
            trip.food_pref_1, trip.food_pref_2, "",
            is_open_at_fn=is_open_at,
        )

        current_place = current_start_place
        order = 0
        day_meal_records = []   # 인기 맛집 재조정용 — 하루치 식사 선택 기록 (아래 for 루프 끝난 뒤 처리)

        last_tour_event = next((e for e in reversed(events) if e[0] == "tour"), None)
        for event in events:
            if event[0] == "tour":
                _, seg_start, seg_end, chunk_target, chunk_popular_quota = event
                seg_start_dt = day_start_kst.replace(hour=seg_start // 60, minute=seg_start % 60)
                seg_end_dt = day_start_kst.replace(hour=seg_end // 60, minute=seg_end % 60)

                # 저녁 이후 마지막 구간: 야간 명소 1곳을 먼저 시도하고, 못 넣었거나 시간이 남으면
                # 같은 구간을 일반 후보로 채운다(야간 명소가 안 맞아도 구간이 비지 않게).
                is_night_seg = need_night and event is last_tour_event
                passes = [("night", night_pool, None, 1, 0)] if is_night_seg else []
                passes.append((
                    "general", all_general_places, quadrant,
                    max(chunk_target, 1) if is_night_seg else chunk_target, chunk_popular_quota,
                ))

                current_time = seg_start_dt
                for kind, pass_places, pass_quadrant, pass_target, pass_quota in passes:
                    remain_min = (seg_end_dt - current_time).total_seconds() / 60
                    if kind == "general" and is_night_seg and remain_min < NIGHT_TAIL_MIN:
                        break
                    best_chunk = _run_general_chunk(
                        pass_places, current_place, max(0.0, remain_min) / 60, pass_target, mode,
                        day_purpose_main, day_purpose_sub, day_exclude,
                        pass_quadrant, current_time, get_travel_time_fn, get_stay_time_fn,
                        visited_across_days, nlp_scores, popular_quota=min(pass_quota, pass_target),
                    )
                    if not best_chunk:
                        continue
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

                prev_place = current_place   # 재조정 시 이동시간 재계산 기준점으로 기록해둠
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

                meal_item = ItineraryItem.objects.create(
                    day=day_obj, order=order, place=chosen_place, slot_type="RESTAURANT",
                    arrive_at=arrive, depart_at=depart, travel_min_from_prev=travel_min,
                    is_relaxed_preference=is_relaxed,
                )
                # 인기 맛집 쿼터는 그 자리에서(마지막 슬롯이라고) 강제하지 않는다 — 하루치 식사를
                # 전부 자연스럽게 고른 뒤, 아래서 "그중 인기 점수가 가장 낮은 슬롯"을 골라 교체한다.
                day_meal_records.append({
                    "item": meal_item, "prev_place": prev_place,
                    "role_candidates": role_candidates, "meal_start_dt": meal_start_dt,
                    "slot_duration_min": slot_duration_min,
                })
                visited_across_days.add(chosen_place.content_id)
                current_place = chosen_place
                order += 1

        # 인기 맛집 재조정: 하루치 식사를 전부 자연스럽게 고른 뒤, 그중 인기 점수가 가장
        # 낮은 슬롯부터 쿼터 부족분만큼 인기 맛집으로 교체한다 — "마지막 슬롯이라 강제"가
        # 아니라 "그날 식사들 중 실제로 제일 아쉬운 슬롯"을 바꾸는 방식.
        current_food_popular = sum(1 for m in day_meal_records if is_popular_place(m["item"].place))
        food_deficit = food_popular_quota - current_food_popular
        any_swapped = False
        if food_deficit > 0:
            worst_first = sorted(day_meal_records, key=lambda m: m["item"].place.popularity_score or 0)
            for m in worst_first[:food_deficit]:
                popular_alt = [
                    p for p in m["role_candidates"]
                    if is_popular_place(p) and p.content_id not in visited_across_days
                ]
                if not popular_alt:
                    continue  # 이 슬롯엔 대체할 인기 후보가 없음 — 강제하지 않고 그대로 둔다

                if m["prev_place"] is None:
                    new_place = popular_alt[0]
                    new_travel = snap_travel_time_5min(
                        estimate_airport_travel_min(new_place.latitude, new_place.longitude, DEFAULT_VEHICLE)
                    )
                else:
                    ranked = score_food_candidates(
                        popular_alt, m["prev_place"], day_purpose_main, day_purpose_sub,
                        mode, m["slot_duration_min"], get_travel_time_fn,
                        relaxed_ids=relaxed_ids, nlp_scores=nlp_scores, visit_datetime=m["meal_start_dt"],
                    )
                    if not ranked:
                        continue
                    new_place = ranked[0]["place"]
                    new_travel = snap_travel_time_5min(ranked[0]["travel_min"])

                old_item = m["item"]
                visited_across_days.discard(old_item.place_id)
                visited_across_days.add(new_place.content_id)
                old_item.place = new_place
                old_item.travel_min_from_prev = new_travel
                old_item.save(update_fields=["place", "travel_min_from_prev"])
                any_swapped = True

        if any_swapped:
            # 교체로 어긋난 이후 일정의 이동시간·도착/출발 시각을 하루 처음부터 다시 흘려보낸다.
            _replay_day_timeline(day_obj, get_travel_time_fn)

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
    t_lodging = time.time()
    if len(day_last_place_ids) == expected_nights and day_last_place_ids:
        lodging_cards = get_lodging_anchor(trip, day_last_place_ids)
    else:
        lodging_cards = []
    with open("debug_log.txt", "a") as f:
        f.write(f"[{mode}] 숙박 앵커 계산: {time.time()-t_lodging:.2f}초\n")

    for day in course.days.exclude(day_index=total_days):
        day.lodging_options_snapshot = lodging_cards
        day.save(update_fields=["lodging_options_snapshot"])

    course.final_score = total_final_score / total_days if total_days else 0
    course.save(update_fields=["final_score"])

    with open("debug_log.txt", "a") as f:
        f.write(f"[{mode}] 전체 소요: {time.time()-t_start:.2f}초\n")

    return course
