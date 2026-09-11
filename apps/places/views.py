from django.shortcuts import render
from rest_framework.views import APIView
from rest_framework.response import Response
from apps.places.models import SavedPlace
from apps.trips.serializers import PlaceSummarySerializer

# Create your views here.
class PlaceSaveView(APIView):
    """POST /api/places/{content_id}/save/  — 저장  /  DELETE — 저장 취소"""

    def post(self, request, content_id):
        place = get_object_or_404(Place, content_id=content_id)
        SavedPlace.objects.get_or_create(user=request.user, place=place)
        return Response({"content_id": content_id, "saved": True})

    def delete(self, request, content_id):
        SavedPlace.objects.filter(user=request.user, place__content_id=content_id).delete()
        return Response({"content_id": content_id, "saved": False})

class SavedPlaceListView(APIView):
    """GET /api/places/saved/ — 내가 저장한 장소 목록"""

    def get(self, request):
        saved = SavedPlace.objects.filter(user=request.user).select_related("place")
        places = [s.place for s in saved]
        return Response(PlaceSummarySerializer(places, many=True).data)