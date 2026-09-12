from rest_framework import serializers
from apps.places.models import Place

# 정보 상세
class PlaceDetailSerializer(serializers.ModelSerializer):
    images = serializers.SerializerMethodField()
    
    class Meta:
        model = Place
        fields = [
            "content_id", "title", "address", "content_type_name",
            "large_category_name", "middle_category_name", "small_category_name",
            "overview", "overview_summary",
            "hours_raw", "closed_days_raw", "fees", "parking", "contact","restroom",
            "stay_time_minutes", "latitude", "longitude", "images",
        ]

    def get_images(self, obj):
        return list(obj.images.order_by("order").values_list("image_url", flat=True))