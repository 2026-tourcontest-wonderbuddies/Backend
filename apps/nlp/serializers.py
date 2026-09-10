from rest_framework import serializers
from apps.places.models import Place


class PlaceDetailSerializer(serializers.ModelSerializer):
    display_overview = serializers.SerializerMethodField()
    
    class Meta:
        model = Place
        fields = [
            "content_id", "title", "address", "content_type_name",
            "large_category_name", "middle_category_name", "small_category_name",
            "overview",
            "hours_raw", "closed_days_raw", "fees", "parking", "contact","restroom",
            "stay_time_minutes", "latitude", "longitude",
        ]

    def get_display_overview(self, obj):
        if obj.overview_summary:
            return obj.overview_summary
        # 문장 수를 대략 마침표 개수로 추정
        sentence_count = obj.overview.count(".") + obj.overview.count("!") + obj.overview.count("?")
        if sentence_count < 3:
            return obj.overview
        return obj.overview_summary or obj.overview