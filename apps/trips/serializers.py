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
            "id", "start_datetime", "end_datetime", "departure_place_id",
            "return_to_departure", "transport_mode", "companion_type",
            "purpose_main", "purpose_sub", "region_preference",
            "mood_tags", "include_places", "exclude_places", "exclude_categories",
            "walk_light", "indoor_outdoor_pref", "free_text_input",
            "food_pref_1", "food_pref_2", "food_restriction", "food_cafe_balance",
            "lodging_capacity", "lodging_type", "lodging_conditions",
            "lodging_budget", "lodging_free_text",
        ]
        read_only_fields = ["id"]


class PlaceSummarySerializer(serializers.ModelSerializer):
    """장소 카드에 필요한 최소 정보만 (Pipeline 7 Place Cards 기준)."""
    class Meta:
        model = Place
        fields = [
            "content_id", "title", "content_type_name", "address",
            "latitude", "longitude", "overview", "hours_raw", "fees",
            "parking", "stay_time_minutes",
        ]


class ItineraryItemSerializer(serializers.ModelSerializer):
    place = PlaceSummarySerializer(read_only=True)

    class Meta:
        model = ItineraryItem
        fields = [
            "id", "order", "place", "slot_type", "arrive_at", "depart_at",
            "travel_min_from_prev", "locked", "hours_uncertain",
        ]


class LodgingSummarySerializer(serializers.ModelSerializer):
    class Meta:
        model = Lodging
        fields = [
            "content_id", "title", "address", "latitude", "longitude",
            "small_category_name", "check_in_time", "check_out_time",
            "min_room_price", "tripcom_hotel_id",
        ]


class ItineraryDaySerializer(serializers.ModelSerializer):
    items = ItineraryItemSerializer(many=True, read_only=True)
    lodging = LodgingSummarySerializer(read_only=True)

    class Meta:
        model = ItineraryDay
        fields = [
            "id", "day_index", "day_case", "avail_hours", "target_slots",
            "need_lunch", "need_dinner", "need_night_spot",
            "lodging", "lodging_options", "items",
        ]


class RecommendedCourseSerializer(serializers.ModelSerializer):
    days = ItineraryDaySerializer(many=True, read_only=True)

    class Meta:
        model = RecommendedCourse
        fields = ["id", "mode", "is_selected", "final_score", "created_at", "days"]


class RecommendedCourseSummarySerializer(serializers.ModelSerializer):
    """3개 코스 목록 조회 시엔 day 전체를 다 안 보내고 요약만 (응답 크기 절약)."""
    class Meta:
        model = RecommendedCourse
        fields = ["id", "mode", "is_selected", "final_score", "created_at"]


class ModifyRequestSerializer(serializers.Serializer):
    raw_message = serializers.CharField()


class SelectLodgingSerializer(serializers.Serializer):
    lodging_id = serializers.CharField()