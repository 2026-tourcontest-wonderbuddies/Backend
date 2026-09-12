import json
from functools import lru_cache
from pathlib import Path

from django.conf import settings
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

PERIODS = ["dawn", "morning", "midday", "sunset", "night"]


@lru_cache(maxsize=1)
def _period_places():
    """scripts/build_period_places.py 가 만든 정적 순위표."""
    path = Path(settings.BASE_DIR) / "data" / "period_places.json"
    return json.loads(path.read_text(encoding="utf-8"))


class PeriodPlacesView(APIView):
    """
    GET /api/places/by-period/?period=night&limit=10 — 시간대별 장소

    아침/낮/노을/밤은 AI Hub 실측 도착시각(evidence="arrival"),
    새벽은 영업·개방 시간(evidence="hours")이 근거다.
    """

    def get(self, request):
        period = request.query_params.get("period", "")
        if period not in PERIODS:
            return Response(
                {"error": f"period는 {PERIODS} 중 하나여야 합니다."},
                status=status.HTTP_400_BAD_REQUEST,
            )

        try:
            limit = int(request.query_params.get("limit", 10))
        except ValueError:
            return Response(
                {"error": "limit은 정수여야 합니다."},
                status=status.HTTP_400_BAD_REQUEST,
            )
        limit = max(1, min(limit, 10))

        rows = _period_places()[period][:limit]
        places = Place.objects.in_bulk([r["content_id"] for r in rows])

        results = []
        for row in rows:
            place = places.get(row["content_id"])
            if place is None:  # 순위표에는 있는데 DB에서 사라진 장소
                continue
            results.append({**PlaceSummarySerializer(place).data, **row})

        return Response({"period": period, "results": results})
