from django.shortcuts import render

# Create your views here.
from rest_framework.views import APIView
from rest_framework.response import Response
from rest_framework import status
from django.shortcuts import get_object_or_404

from apps.places.models import Place
from apps.places.serializers import PlaceDetailSerializer   # 아래에서 새로 만듦
from apps.nlp.rag_qa import answer_place_question


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