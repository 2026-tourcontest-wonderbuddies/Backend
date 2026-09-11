from rest_framework.views import APIView
from rest_framework.response import Response
from rest_framework import serializers

from apps.places.models import Place

SEARCHABLE_CATEGORIES = ["관광지", "문화시설", "쇼핑", "음식점"]
DEFAULT_PAGE_SIZE = 20


class PlaceSearchSerializer(serializers.ModelSerializer):
    class Meta:
        model = Place
        fields = [
            "content_id", "title", "content_type_name", "small_category_name",
            "address", "latitude", "longitude", "overview", "contact",
            "hours_raw", "closed_days_raw", "fees", "parking", "menu", "featured_menu",
        ]


class PlaceSearchView(APIView):
    """GET /api/places/search/?q=&category=&region=&page=&page_size= — 장소 검색(이름/유형/권역)"""

    def get(self, request):
        q = request.query_params.get("q", "").strip()
        category = request.query_params.get("category", "").strip()
        region = request.query_params.get("region", "").strip()

        try:
            page = max(int(request.query_params.get("page", 1)), 1)
        except ValueError:
            page = 1
        try:
            page_size = max(int(request.query_params.get("page_size", DEFAULT_PAGE_SIZE)), 1)
        except ValueError:
            page_size = DEFAULT_PAGE_SIZE

        qs = Place.objects.filter(content_type_name__in=SEARCHABLE_CATEGORIES)
        if q:
            qs = qs.filter(title__icontains=q)
        if category in SEARCHABLE_CATEGORIES:
            qs = qs.filter(content_type_name=category)
        if region in dict(Place.QUADRANT_CHOICES):
            qs = qs.filter(quadrant=region)

        qs = qs.order_by("title")
        total = qs.count()
        start = (page - 1) * page_size
        results = qs[start:start + page_size]

        return Response({
            "results": PlaceSearchSerializer(results, many=True).data,
            "total": total,
            "has_more": start + page_size < total,
        })
