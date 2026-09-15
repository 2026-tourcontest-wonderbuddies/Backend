"""
API 입출력 스키마. TripRequest 입력 검증 + 결과(코스/장소/숙소) 직렬화.
"""

from datetime import datetime, timedelta, timezone as dt_timezone
from zoneinfo import ZoneInfo

from rest_framework import serializers
from apps.trips.models import TripRequest, RecommendedCourse, ItineraryDay, ItineraryItem
from apps.places.models import Place, Lodging
from apps.recommendation.constraints import estimate_airport_travel_min, snap_travel_time_5min

KST = ZoneInfo("Asia/Seoul")
DEFAULT_VEHICLE = "car"


def _combine_date_and_time(base_date, day_index: int, time_str: str) -> datetime:
    """trip.start_date + (day_index-1)일 + day_schedules의 'HH:MM' → KST datetime"""
    target_date = base_date + timedelta(days=day_index - 1)
    hour, minute = map(int, time_str.split(":"))
    return datetime(target_date.year, target_date.month, target_date.day, hour, minute, tzinfo=KST)


class TripRequestSerializer(serializers.ModelSerializer):
    class Meta:
        model = TripRequest
        fields = [
            "id", "start_date", "end_date", "guests",   # ★ start_datetime/end_datetime → start_date/end_date
            "purpose_main", "purpose_sub", "region_preference",
            "exclude_categories", "free_text_input",
            "food_pref_1", "food_pref_2", "food_cafe_balance",
            "lodging_type", "lodging_need_cooking", "lodging_free_text",
            "day_schedules",
        ]
        read_only_fields = ["id"]


class PlaceSummarySerializer(serializers.ModelSerializer):
    class Meta:
        model = Place
        fields = ["content_id", "title", "content_type_name", "small_category_name", "address",
                  "latitude", "longitude", "overview", "contact", "hours_raw", "closed_days_raw",
                  "fees", "parking", "menu", "featured_menu", "stay_time_minutes"]

class ItineraryItemSerializer(serializers.ModelSerializer):
    place = PlaceSummarySerializer(read_only=True)
    recommend_reason = serializers.SerializerMethodField()

    class Meta:
        model = ItineraryItem
        fields = ["id", "order", "place", "slot_type", "arrive_at", "depart_at",
                  "travel_min_from_prev", "locked", "hours_uncertain",
                  "is_relaxed_preference", "recommend_reason"]

    # 장소 추천 이유
    def get_recommend_reason(self, obj):
        # 임시 비활성화
        return None
        # from apps.nlp.rag_qa import generate_place_recommend_reason

        # trip = obj.day.course.trip
        # day_index = obj.day.day_index

        # override = next((ov for ov in trip.day_overrides if ov.get("day_index") == day_index), None)
        # purpose_main = override.get("purpose_main", trip.purpose_main) if override else trip.purpose_main
        # purpose_sub = override.get("purpose_sub", trip.purpose_sub) if override else trip.purpose_sub

        # return generate_place_recommend_reason(obj.place, purpose_main, purpose_sub)

# 일정 상세 정보
class ItineraryDaySerializer(serializers.ModelSerializer):
    items = ItineraryItemSerializer(many=True, read_only=True)
    lodging = serializers.SerializerMethodField()

    class Meta:
        model = ItineraryDay
        fields = ["id", "day_index", "day_case", "avail_hours", "target_slots",
                "need_morning", "need_lunch", "need_dinner", "need_night_spot", 
                "lodging", "travel_to_next_min", "items"]

    # 필요한 숙소 정보만 같이 보내기 
    def get_lodging(self, obj):
        if not obj.lodging_snapshot:
            return None
        snap = obj.lodging_snapshot
        return {
            "content_id": snap.get("content_id"),
            "title": snap.get("title"),
            "category": snap.get("category"),
            "price_hint": snap.get("price_hint"),
            "address": snap.get("address"),
            "room_type": snap.get("room_type"),
            "check_in_time": snap.get("check_in_time"),
            "check_out_time": snap.get("check_out_time"),
            "tripcom_link": snap.get("tripcom_link"),
            "lat": snap.get("lat"),
            "lon": snap.get("lon"),
            "region": snap.get("region"),
        }


class RecommendedCourseSerializer(serializers.ModelSerializer):
    days = ItineraryDaySerializer(many=True, read_only=True)
    # 제주공항 기준 여행 양 끝 시각. 프론트가 타임라인 맨 위·맨 아래 공항 항목으로 쓴다.
    trip_start_datetime = serializers.SerializerMethodField()
    trip_end_datetime = serializers.SerializerMethodField()
    # 마지막 날 마지막 장소 → 공항 복귀 이동시간(분). 프론트가 맨 아래 공항 항목 이전 구간에 쓴다.
    return_to_airport_travel_min = serializers.SerializerMethodField()

    class Meta:
        model = RecommendedCourse
        fields = ["id", "mode", "is_selected", "final_score", "created_at",
                  "trip_start_datetime", "trip_end_datetime", "return_to_airport_travel_min", "days"]

    def get_trip_start_datetime(self, obj):
        trip = obj.trip
        schedules = sorted(trip.day_schedules, key=lambda d: d["day_index"])
        first = schedules[0]
        dt = _combine_date_and_time(trip.start_date, first["day_index"], first["start_time"])
        return dt.astimezone(dt_timezone.utc)

    def get_trip_end_datetime(self, obj):
        trip = obj.trip
        schedules = sorted(trip.day_schedules, key=lambda d: d["day_index"])
        last = schedules[-1]
        dt = _combine_date_and_time(trip.start_date, last["day_index"], last["end_time"])
        return dt.astimezone(dt_timezone.utc)

    def get_return_to_airport_travel_min(self, obj):
        last_day = obj.days.order_by("day_index").last()
        if not last_day:
            return None
        last_item = last_day.items.order_by("order").last()
        if not last_item:
            return None
        travel_min = estimate_airport_travel_min(
            last_item.place.latitude, last_item.place.longitude, DEFAULT_VEHICLE
        )
        return snap_travel_time_5min(travel_min)

# 코스 요약
class RecommendedCourseSummarySerializer(serializers.ModelSerializer):
    place_count = serializers.SerializerMethodField()           # 방문한 장소 몇 곳
    total_duration_min = serializers.SerializerMethodField()    # 총 소요시간
    total_travel_min = serializers.SerializerMethodField()      # 총 이동시간 합계
    days_summary = serializers.SerializerMethodField()          # day마다 장소 수/가용시간

    class Meta:
        model = RecommendedCourse
        fields = ["id", "mode", "is_selected", "final_score", "created_at",
        "place_count", "total_duration_min", "total_travel_min", "days_summary"]

    def get_place_count(self, obj):
        return sum(day.items.count() for day in obj.days.all())

    def get_total_duration_min(self, obj):
        total = 0
        for day in obj.days.all():
            items = list(day.items.all())
            if items:
                total += int((items[-1].depart_at - items[0].arrive_at).total_seconds() / 60)
        return total

    def get_total_travel_min(self, obj):
        return sum(
            item.travel_min_from_prev or 0
            for day in obj.days.all() for item in day.items.all()
        )

    def get_days_summary(self, obj):
        """day마다 장소 몇 곳/가용시간 몇 시간인지."""
        return [
            {"day_index": day.day_index, "place_count": day.items.count(), "avail_hours": day.avail_hours}
            for day in obj.days.all()
        ]


class ModifyRequestSerializer(serializers.Serializer):
    raw_message = serializers.CharField()