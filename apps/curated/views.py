from rest_framework.views import APIView
from rest_framework.permissions import IsAuthenticated
from rest_framework.response import Response
from django.shortcuts import get_object_or_404
from apps.curated.models import CuratedCourse, SavedCuratedCourse
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


class CuratedCourseSaveView(APIView):
    """POST /api/curated-courses/{course_id}/save/ — 저장  /  DELETE — 저장 취소"""
    permission_classes = [IsAuthenticated]

    def post(self, request, course_id):
        course = get_object_or_404(CuratedCourse, id=course_id)
        SavedCuratedCourse.objects.get_or_create(user=request.user, course=course)
        return Response({"course_id": course_id, "is_saved": True})

    def delete(self, request, course_id):
        SavedCuratedCourse.objects.filter(user=request.user, course_id=course_id).delete()
        return Response({"course_id": course_id, "is_saved": False})


class SavedCuratedCourseListView(APIView):
    """GET /api/curated-courses/saved/ — 내가 저장한 추천 코스 목록(최근 저장순)"""
    permission_classes = [IsAuthenticated]

    def get(self, request):
        saved = SavedCuratedCourse.objects.filter(user=request.user).select_related("course")
        return Response(CuratedCourseListSerializer([s.course for s in saved], many=True).data)
