from rest_framework import serializers
from apps.curated.models import CuratedCourse, CuratedCourseItem
from apps.trips.serializers import PlaceSummarySerializer


class CuratedCourseListSerializer(serializers.ModelSerializer):
    """GET /api/curated-courses/ — 목록/필터링 화면용. 장소 상세는 안 담는다."""

    class Meta:
        model = CuratedCourse
        fields = ["id", "title", "region", "region_label", "duration",
                  "badge", "time_of_day", "meta_chips", "gradient"]


class CuratedCourseItemSerializer(serializers.ModelSerializer):
    place = PlaceSummarySerializer(read_only=True)

    class Meta:
        model = CuratedCourseItem
        fields = ["order", "place"]


class CuratedCourseDetailSerializer(serializers.ModelSerializer):
    items = CuratedCourseItemSerializer(many=True, read_only=True)

    class Meta:
        model = CuratedCourse
        fields = ["id", "title", "region", "region_label", "duration",
                  "badge", "time_of_day", "meta_chips", "gradient", "items"]
