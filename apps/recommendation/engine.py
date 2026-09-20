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
from apps.recommendation.food_scoring import build_meal_candidates, build_morning_meal_candidates, decide_food_slot_types, score_food_candidates, filter_food_candidates
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


def _assign_extras_to_segments(extra_types: list[str], chunk_targets: list[int]) -> list[list[str]]:
    """
    decide_food_slot_types()가 정한 추가 식당/카페 타입들을, 관광구간(tour_segments)에
    "몰아넣지 않고 퍼뜨려" 배정한다 — 구간마다 최대 1개까지, 시간이 긴 구간부터 우선.

    "안전하게 끼워넣을 자리가 있는 구간"에만 배정한다 — 식사류가 식사시간(meal)과
    바로 붙는 걸 막는 게 목적이므로, 자리가 없는 구간에 억지로 넣느니 그 추가 슬롯은
    포기(drop)한다:
      - 일반 장소가 2곳 이상인 구간: 항상 안전(양옆에 일반 장소를 둘 수 있음)
      - 일반 장소가 1곳뿐인 구간: 하루의 첫/마지막 관광구간일 때만 안전
        (한쪽이 식사가 아니라 하루 시작/끝이라 그쪽으로 붙이면 됨)
      - 그 외(일반 장소 0곳, 또는 중간 구간에 1곳뿐)는 배정하지 않음
    """
    n = len(chunk_targets)
    assigned: list[list[str]] = [[] for _ in range(n)]
    if not extra_types or n == 0:
        return assigned

    def is_safe(i: int) -> bool:
        if chunk_targets[i] >= 2:
            return True
        if chunk_targets[i] == 1 and (i == 0 or i == n - 1):
            return True
        return False

    eligible = sorted((i for i in range(n) if is_safe(i)), key=lambda i: chunk_targets[i], reverse=True)

    ei = 0
    for i in eligible:
        if ei >= len(extra_types):
            return assigned
        assigned[i].append(extra_types[ei])
        ei += 1
    # 남는 추가 슬롯은 안전한 자리가 없으면 그냥 포기한다(억지 배치보다 품질 우선).
    return assigned


def _run_general_chunk(all_general_places, start_place, chunk_avail_hours, chunk_target, mode,
                        purpose_main, purpose_sub, exclude_categories,
                        region_quadrant, visit_start_dt, get_travel_time_fn, get_stay_time_fn,
                        visited_across_days, nlp_scores, popular_quota=0,
                        airport_deadline_dt=None, matrix_ids=None):
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
        popular_quota=popular_quota, airport_deadline_dt=airport_deadline_dt, matrix_ids=matrix_ids, 
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

        day_end_kst = _combine_date_and_time(trip.start_date, day_index, schedule["end_time"])
        avail = calc_avail_hours_from_schedule(day_index, total_days, day_start_kst, day_end_kst)

        # 야간 후보가 없으면 야간 슬롯(+1)도 만들지 않는다.
        night_pool = _night_pool(all_general_places, quadrant, visited_across_days) if avail.need_night_spot else []
        need_night = bool(night_pool)
        target_slots = calc_target_slots(avail.avail_hours, mode, need_night)
        general_target = max(target_slots - meal_count, 0)

        # 음식/카페가 목적(주 또는 보조)으로 선택됐으면, 관광 슬롯 중 일부를
        # 식당/카페 "추가 슬롯"으로 떼어낸다 — 나머지가 실제 일반 장소 슬롯이 된다.
        food_is_main = day_purpose_main == "food"
        food_is_sub = (not food_is_main) and day_purpose_sub == "food"
        extra_food_types = (
            decide_food_slot_types(food_is_main, general_target, trip.food_cafe_balance)
            if (food_is_main or food_is_sub) else []
        )
        general_target = max(general_target - len(extra_food_types), 0)

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
        extra_food_assignment = (
            _assign_extras_to_segments(extra_food_types, general_chunk_targets) if tour_segments else []
        )

        # 인기 장소 쿼터: 일자별 관광 슬롯수(general_target)의 50%를
        # 일반(관광지) 슬롯 안에서 강제 확보한다. 구간 배분은 관광 목표 개수와 동일하게
        # 시간 비율로 나누고, 각 구간의 실제 목표 개수를 넘지 않도록 캡을 건다.
        popular_quota_total = round(general_target * POPULAR_QUOTA_RATIO)
        general_popular_quotas = _split_by_time_ratio(popular_quota_total, segment_minutes) if tour_segments else []
        general_popular_quotas = [min(q, t) for q, t in zip(general_popular_quotas, general_chunk_targets)]

        # 음식 슬롯도 동일한 방식으로 인기 맛집 쿼터 적용 (일자별 식사 슬롯 수의 50%).
        # round()면 식사가 1~2개인 흔한 케이스가 전부 0으로 내림돼 쿼터가 무력화되므로,
        # 식사가 1개 이상이면 최소 1곳은 보장되도록 ceil(올림) 사용 — 팀 합의 사항.
        # ★ 고정 식사 슬롯(점심/저녁)에만 적용 — 추가 식당/카페 슬롯은 대상에서 제외한다.
        # 고정 식사는 시간대가 못 박혀있어 재조정(교체)돼도 시각이 절대 안 바뀌지만, 추가
        # 슬롯은 시간이 흘러가는 방식이라 교체 시 이동시간이 달라지면 다음 식사와 겹칠 수 있다.
        food_popular_quota = math.ceil(meal_count * POPULAR_QUOTA_RATIO)

        # 이벤트 목록 조립: tour/meal 시간순 교차
        events = []
        tour_i = 0
        for i in range(0, len(boundaries) - 1, 2):
            seg_start, seg_end = boundaries[i], boundaries[i + 1]
            if seg_end > seg_start:
                # "안전한 쪽"은 위치(첫/마지막 구간)가 아니라 실제로 그쪽 경계가
                # 하루 시작/끝인지로 판단해야 한다 — 아침식사가 하루 시작 시각과
                # 겹치면 첫 관광구간이라도 그 앞쪽은 이미 식사(불안전)라서.
                starts_at_day_start = seg_start == avail.avail_start_min
                ends_at_day_end = seg_end == avail.avail_end_min
                events.append((
                    "tour", seg_start, seg_end,
                    general_chunk_targets[tour_i], general_popular_quotas[tour_i],
                    extra_food_assignment[tour_i],
                    starts_at_day_start, ends_at_day_end,   # is_first_seg, is_last_seg
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
        # 아침 식사 전용 후보 — breakfast_suitable=True는 항상 유지, 부족할 때만 선호음식 제거.
        morning_meal_candidates, morning_relaxed_ids = build_morning_meal_candidates(
            all_food_places, quadrant, day_start_kst,
            trip.food_pref_1, trip.food_pref_2, "",
            is_open_at_fn=is_open_at,
        )
        # 카페는 "선호 음식 10종" 태그를 애초에 안 갖는 카테고리라(커피·디저트 위주),
        # 식사용 선호 태그로 거르면 후보가 거의 항상 0곳이 돼버린다. 권역·영업시간만
        # 확인하고 나머지는 일반 장소처럼 목적 점수로만 순위를 매기도록 별도 풀을 둔다.
        cafe_pool, _ = filter_food_candidates(
            all_food_places, quadrant, day_start_kst, "", "", "",
            is_open_at_fn=is_open_at,
        )
        cafe_candidates = [p for p in cafe_pool if p.food_role == "CAFE"]

        current_place = current_start_place
        order = 0
        day_meal_records = []   # 인기 맛집 재조정용 — 하루치 식사 선택 기록 (아래 for 루프 끝난 뒤 처리)

        last_tour_event = next((e for e in reversed(events) if e[0] == "tour"), None)
        for event in events:
            if event[0] == "tour":
                (_, seg_start, seg_end, chunk_target, chunk_popular_quota, extra_types_for_seg,
                 is_first_seg, is_last_seg) = event
                seg_start_dt = day_start_kst.replace(hour=seg_start // 60, minute=seg_start % 60)
                seg_end_dt = day_start_kst.replace(hour=seg_end // 60, minute=seg_end % 60)

                # 저녁 이후 마지막 구간: 야간 명소 1곳을 먼저 시도하고, 못 넣었거나 시간이 남으면
                # 같은 구간을 일반 후보로 채운다(야간 명소가 안 맞아도 구간이 비지 않게).
                is_night_seg = need_night and event is last_tour_event
                passes = [("night", night_pool, None, 1, 0, [])] if is_night_seg else []
                passes.append((
                    "general", all_general_places, quadrant,
                    max(chunk_target, 1) if is_night_seg else chunk_target, chunk_popular_quota,
                    extra_types_for_seg,
                ))

                current_time = seg_start_dt
                for kind, pass_places, pass_quadrant, pass_target, pass_quota, pass_extra_types in passes:
                    remain_min = (seg_end_dt - current_time).total_seconds() / 60
                    if kind == "general" and is_night_seg and remain_min < NIGHT_TAIL_MIN:
                        break

                    is_last_day = (day_index == total_days)
                    is_final_tour_segment = (event is last_tour_event)
                    airport_deadline = day_end_kst if (is_last_day and is_final_tour_segment and kind == "general") else None
                    
                    best_chunk = _run_general_chunk(
                        pass_places, current_place, max(0.0, remain_min) / 60, pass_target, mode,
                        day_purpose_main, day_purpose_sub, day_exclude,
                        pass_quadrant, current_time, get_travel_time_fn, get_stay_time_fn,
                        visited_across_days, nlp_scores, popular_quota=min(pass_quota, pass_target),
                        airport_deadline_dt=airport_deadline,
                        matrix_ids=matrix_ids,
                    )
                    chunk_items = list(best_chunk.items) if best_chunk else []

                    # ★ 아래 끼워넣기·시뮬레이션·안전장치는 전부 "식당/카페 추가슬롯"
                    # 전용 로직이다 — 추가슬롯이 없는 구간(순수 일반 장소만)은 beam_search가
                    # 이미 시간 예산 안에서 골라주므로 이 블록 자체가 필요 없어 건너뛴다.
                    if pass_extra_types:
                        # ★ 추가 식당/카페 슬롯을, 이 구간에서 뽑힌 일반 장소들 "중간"에 끼워넣는다.
                        # 맨 앞/맨 뒤에 두면 식사시간(RESTAURANT)이나 다른 식당류 슬롯이랑 바로
                        # 붙어버릴 수 있어서 — 퐁당퐁당 배치 + 식사류 연속 방지가 목적.
                        picked_this_pass: set[str] = set()
                        for extra_type in pass_extra_types:
                            source_pool = cafe_candidates if extra_type == "CAFE" else meal_candidates
                            role_pool = [
                                p for p in source_pool
                                if p.content_id not in visited_across_days
                                and p.content_id not in picked_this_pass
                                and p.food_role == extra_type
                            ]
                            if not role_pool:
                                continue

                            n_items = len(chunk_items)
                            if n_items >= 2:
                                insert_idx = n_items // 2
                            elif n_items == 1:
                                # 한쪽만 있으면 식사(meal)가 아니라 하루 시작/끝과 붙는 쪽으로.
                                insert_idx = 0 if is_first_seg and not is_last_seg else 1
                            elif extra_type == "CAFE" or (is_first_seg and is_last_seg):
                                # 카페는 식사류 연속 규칙 예외라 괜찮고, 하루 유일한 관광구간이면
                                # 어차피 양옆 다 안전(식사 자체가 없거나 이 구간뿐).
                                insert_idx = 0
                            else:
                                continue   # 일반 장소가 하나도 없어 양옆을 감쌀 수 없음 — 억지로 넣지 않고 포기

                            prev_for_extra = chunk_items[insert_idx - 1]["place"] if insert_idx > 0 else current_place
                            # 끼워넣는 지점까지 앞선 일반 장소들이 실제로 소모할 시간을 반영해서
                            # 남는 시간을 정확히 계산 — 안 그러면 뒤 식사시간이랑 겹칠 수 있다.
                            elapsed_before = sum(
                                it["travel_min"] + it["stay_min"] for it in chunk_items[:insert_idx]
                            )
                            time_at_insertion = current_time + timedelta(minutes=elapsed_before)
                            remain_for_extra = max(0.0, (seg_end_dt - time_at_insertion).total_seconds() / 60)
                            if remain_for_extra <= 0:
                                continue   # 낄 시간 자체가 없으면 포기(억지로 겹치게 넣지 않음)

                            if prev_for_extra is None:
                                chosen_place = role_pool[0]
                                food_travel = estimate_airport_travel_min(
                                    chosen_place.latitude, chosen_place.longitude, DEFAULT_VEHICLE
                                )
                                is_relaxed = chosen_place.content_id in relaxed_ids
                                raw_stay = chosen_place.stay_time_minutes or 30
                                available = max(0.0, remain_for_extra - food_travel)
                                stay_min = min(raw_stay, available) if available > 0 else 0
                            else:
                                ranked = score_food_candidates(
                                    role_pool, prev_for_extra, day_purpose_main, day_purpose_sub,
                                    mode, remain_for_extra, get_travel_time_fn,
                                    relaxed_ids=relaxed_ids, nlp_scores=nlp_scores, visit_datetime=current_time,
                                )
                                if not ranked:
                                    continue
                                chosen = ranked[0]
                                chosen_place = chosen["place"]
                                food_travel = chosen["travel_min"]
                                is_relaxed = chosen.get("is_relaxed", False)
                                stay_min = chosen["stay_min"]

                            chunk_items.insert(insert_idx, {
                                "place": chosen_place, "travel_min": food_travel, "stay_min": stay_min,
                                "hours_uncertain": False, "_is_food_extra": True,
                                "_is_relaxed": is_relaxed, "_role_candidates": role_pool,
                            })
                            # visited_across_days에는 아직 안 넣는다 — 뒤 시뮬레이션에서 시간이
                            # 안 맞아 잘려나갈 수도 있으므로, 실제로 확정되는 최종 생성 루프에서만 넣는다.
                            picked_this_pass.add(chosen_place.content_id)
                            # 끼워넣은 바로 다음 장소는 beam search가 계산해둔 이전 이동시간(원래
                            # 이웃 기준)이 더 이상 안 맞으므로, 새 이웃(추가 식당/카페) 기준으로 다시 계산.
                            if insert_idx + 1 < len(chunk_items):
                                nxt = chunk_items[insert_idx + 1]
                                nxt["travel_min"] = get_travel_time_fn(
                                    chosen_place.content_id, nxt["place"].content_id
                                )["duration_min_adjusted"]

                        # ★ 끼워넣은 뒤 전체를 순서대로 시뮬레이션해서, 이 구간 끝(다음 고정
                        # 식사/야간 등)을 넘기는 뒤쪽 항목이 있으면 잘라낸다 — 추가 식당/카페
                        # 때문에 뒤 순서 일반 장소들이 밀려서 다음 식사시간과 겹치는 걸 방지.
                        sim_time = current_time
                        kept_items = []
                        for sim_idx, it in enumerate(chunk_items):
                            if order == 0 and sim_idx == 0:
                                t = estimate_airport_travel_min(
                                    it["place"].latitude, it["place"].longitude, DEFAULT_VEHICLE
                                )
                            else:
                                t = it["travel_min"]
                            # ★ 실제 생성 루프는 travel_min을 5분 단위로 반올림(snap)해서 쓰기 때문에,
                            # 여기서도 똑같이 snap한 값으로 시뮬레이션해야 실제 겹침 여부와 일치한다.
                            # (안 맞추면 반올림으로 몇 분 더 밀리는 걸 못 잡아서 다음 식사시간과 겹칠 수 있음)
                            sim_time += timedelta(minutes=snap_travel_time_5min(t))
                            if sim_time >= seg_end_dt:
                                break
                            # ★ 도착만 구간 안이어도, "체류까지 마친 시각"(출발)이 구간을 넘기면
                            # 그 다음 이벤트(고정 식사 등)랑 겹친다 — 그 경우 이 장소부터 자른다.
                            # (다음 항목이 없는 "마지막 장소"일 때 특히 이 체크가 없으면 못 잡힘)
                            departure_sim = sim_time + timedelta(minutes=it["stay_min"])
                            if departure_sim > seg_end_dt:
                                break
                            sim_time = departure_sim
                            kept_items.append(it)
                        chunk_items = kept_items

                        # ★ 최종 안전장치: 시뮬레이션에서 일반 장소가 잘려나가는 바람에 추가
                        # 식당/카페(RESTAURANT/SNACK)가 결국 구간 경계에 "혼자" 남아 식사시간과
                        # 붙게 됐으면 — 억지로 유지하느니 그 추가 슬롯 자체를 포기한다.
                        def _is_unsafe_food_edge(it, seg_is_safe_side):
                            return (
                                it.get("_is_food_extra")
                                and it["place"].food_role in ("RESTAURANT", "SNACK")
                                and not seg_is_safe_side
                            )
                        if chunk_items and _is_unsafe_food_edge(chunk_items[0], is_first_seg):
                            chunk_items.pop(0)
                        if chunk_items and _is_unsafe_food_edge(chunk_items[-1], is_last_seg):
                            chunk_items.pop()

                        # ★ 부족분 보충: 계획했던 추가 식당/카페 개수만큼 다 못 넣었으면
                        # (구간 수 부족, 식사류 연속 금지 등으로 취소된 경우), 그 자리를 그냥
                        # 비워두지 않고 남은 시간만큼 일반 장소로 채운다 — 총 개수는 지킨다.
                        actual_extra_count = sum(1 for it in chunk_items if it.get("_is_food_extra"))
                        deficit = len(pass_extra_types) - actual_extra_count
                        if deficit > 0:
                            fill_time = current_time
                            for fidx, fit in enumerate(chunk_items):
                                ft = (
                                    estimate_airport_travel_min(
                                        fit["place"].latitude, fit["place"].longitude, DEFAULT_VEHICLE
                                    ) if order == 0 and fidx == 0 else fit["travel_min"]
                                )
                                fill_time += timedelta(minutes=snap_travel_time_5min(ft))
                                fill_time += timedelta(minutes=fit["stay_min"])
                            remain_fill_hours = max(0.0, (seg_end_dt - fill_time).total_seconds() / 60) / 60
                            if remain_fill_hours > 0:
                                fill_start_place = chunk_items[-1]["place"] if chunk_items else current_place
                                fill_chunk = _run_general_chunk(
                                    all_general_places, fill_start_place, remain_fill_hours, deficit, mode,
                                    day_purpose_main, day_purpose_sub, day_exclude,
                                    quadrant, fill_time, get_travel_time_fn, get_stay_time_fn,
                                    visited_across_days, nlp_scores, popular_quota=0,
                                )
                                if fill_chunk:
                                    chunk_items.extend(fill_chunk.items)

                    for item in chunk_items:
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
                        # ★ 추가 식당/카페 슬롯은 인기 맛집 재조정 대상(day_meal_records)에
                        # 넣지 않는다 — 고정 식사와 달리 시간이 흘러가는 방식이라, 재조정으로
                        # 교체되면 이동시간이 달라져 다음 식사시간과 겹칠 수 있기 때문이다.
                        ItineraryItem.objects.create(
                            day=day_obj, order=order, place=item["place"], slot_type="GENERAL",
                            arrive_at=arrive, depart_at=depart, travel_min_from_prev=travel_min,
                            hours_uncertain=item.get("hours_uncertain", False),
                            is_relaxed_preference=item.get("_is_relaxed", False),
                        )
                        visited_across_days.add(item["place"].content_id)
                        current_place = item["place"]
                        order += 1

            else:  # "meal" — ★ 시간대 강제 고정
                _, meal_name, w_start, w_end = event
                slot_duration_min = w_end - w_start   # ★ stay_time_minutes 무시, 겹친 폭 그대로
                meal_start_dt = day_start_kst.replace(hour=w_start // 60, minute=w_start % 60)

                # 아침 슬롯은 breakfast_suitable=True 전용 후보 풀·완화집합을 쓴다.
                is_morning = meal_name == "morning"
                active_meal_candidates = morning_meal_candidates if is_morning else meal_candidates
                active_relaxed_ids = morning_relaxed_ids if is_morning else relaxed_ids

                role_candidates = [
                    p for p in active_meal_candidates
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
                        relaxed_ids=active_relaxed_ids, nlp_scores=nlp_scores, visit_datetime=meal_start_dt,
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
                    "slot_duration_min": slot_duration_min, "relaxed_ids": active_relaxed_ids,
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
                        relaxed_ids=m["relaxed_ids"], nlp_scores=nlp_scores, visit_datetime=m["meal_start_dt"],
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
                # 교체된 장소 기준으로 선호음식 일치 여부도 다시 계산 — 안 하면 원래
                # 뽑았던 장소(교체 전) 기준 값이 그대로 남아 화면에 잘못 표시된다.
                old_item.is_relaxed_preference = new_place.content_id in m["relaxed_ids"]
                old_item.save(update_fields=["place", "travel_min_from_prev", "is_relaxed_preference"])
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
