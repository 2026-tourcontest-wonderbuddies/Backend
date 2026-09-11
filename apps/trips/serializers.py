"""
API 입출력 스키마. TripRequest 입력 검증 + 결과(코스/장소/숙소) 직렬화.
"""

from rest_framework import serializers
from apps.trips.models import TripRequest, RecommendedCourse, ItineraryDay, ItineraryItem
from apps.places.models import Place, Lodging


class TripRequestSerializer(serializers.ModelSerializer):
    class Meta:
        model = TripRequest
        fields = [
            "id", "start_datetime", "end_datetime", "guests",
            "purpose_main", "purpose_sub", "region_preference",
            "exclude_categories", "free_text_input",
            "food_pref_1", "food_pref_2", "food_cafe_balance",
            "lodging_type", "lodging_need_cooking", "lodging_free_text",
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

    class Meta:
        model = ItineraryItem
        fields = ["id", "order", "place", "slot_type", "arrive_at", "depart_at",
                  "travel_min_from_prev", "locked", "hours_uncertain"]

# 일정 상세 정보
class ItineraryDaySerializer(serializers.ModelSerializer):
    items = ItineraryItemSerializer(many=True, read_only=True)

    class Meta:
        model = ItineraryDay
        fields = ["id", "day_index", "day_case", "avail_hours", "target_slots",
                "need_morning", "need_lunch", "need_dinner", "need_night_spot", "lodging", "items"]

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

    class Meta:
        model = RecommendedCourse
        fields = ["id", "mode", "is_selected", "final_score", "created_at", "days"]

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