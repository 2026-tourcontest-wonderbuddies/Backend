from django.shortcuts import render
from rest_framework.views import APIView
from rest_framework.response import Response
from apps.places.models import SavedPlace
from apps.trips.serializers import PlaceSummarySerializer
from apps.places.serializers import PlaceDetailSerializer
from apps.places.models import Place
from apps.nlp.rag_qa import answer_place_question
from rest_framework import status
from django.shortcuts import get_object_or_404

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

class PlaceDetailView(APIView):
    """
    GET /api/places/{content_id}/
    장소 상세 정보 조회
    """

    def get(self, request, content_id):
        place = get_object_or_404(Place, content_id=content_id)
        return Response(PlaceDetailSerializer(place).data)


class PlaceAskView(APIView):
    """
    POST /api/places/{content_id}/ask/
    RAG 방식으로 이 장소에 대한 자유 질문에 답변.
    """

    def post(self, request, content_id):
        question = request.data.get("question")
        if not question:
            return Response(
                {"error": "question 필드가 필요합니다."},
                status=status.HTTP_400_BAD_REQUEST,
            )

        # 장소가 실제로 존재하는지 먼저 확인 (없으면 404)
        get_object_or_404(Place, content_id=content_id)

        try:
            answer = answer_place_question(content_id, question)
        except Exception as e:
            return Response(
                {"error": str(e)},
                status=status.HTTP_500_INTERNAL_SERVER_ERROR,
            )

        return Response({"content_id": content_id, "question": question, "answer": answer})