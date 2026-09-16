from rest_framework.views import APIView
from rest_framework.response import Response
from django.shortcuts import get_object_or_404
from apps.curated.models import CuratedCourse
from apps.curated.serializers import CuratedCourseListSerializer, CuratedCourseDetailSerializer


class CuratedCourseListView(APIView):
    """GET /api/curated-courses/ — 추천 코스 전체 목록. 필터링은 프론트에서 한다(지금 목업과 동일)."""

    def get(self, request):
        courses = CuratedCourse.objects.prefetch_related("items")
        return Response(CuratedCourseListSerializer(courses, many=True).data)


class CuratedCourseDetailView(APIView):
    """GET /api/curated-courses/{id}/ — 방문지 목록 포함 상세."""

    def get(self, request, course_id):
        course = get_object_or_404(
            CuratedCourse.objects.prefetch_related("items__place"), id=course_id
        )
        return Response(CuratedCourseDetailSerializer(course).data)
