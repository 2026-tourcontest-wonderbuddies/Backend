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
        fields = ["content_id", "title", "content_type_name", "address",
                  "latitude", "longitude", "overview", "hours_raw", "fees",
                  "parking", "stay_time_minutes"]

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
                "need_morning", "need_lunch", "need_dinner", "need_night_spot", "items"]


class RecommendedCourseSerializer(serializers.ModelSerializer):
    days = ItineraryDaySerializer(many=True, read_only=True)

    class Meta:
        model = RecommendedCourse
        fields = ["id", "mode", "is_selected", "final_score", "created_at", "days"]


class RecommendedCourseSummarySerializer(serializers.ModelSerializer):
    class Meta:
        model = RecommendedCourse
        fields = ["id", "mode", "is_selected", "final_score", "created_at"]


class ModifyRequestSerializer(serializers.Serializer):
    raw_message = serializers.CharField()