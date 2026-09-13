from django.shortcuts import render

# Create your views here.
"""
API 뷰. 알고리즘(engine.py) 호출은 여기서만 하고, 뷰 자체는 최대한 얇게 유지.
"""

from rest_framework.views import APIView
from rest_framework.response import Response
from rest_framework import status
from django.shortcuts import get_object_or_404

from apps.trips.models import TripRequest, RecommendedCourse
from apps.trips.serializers import (
    TripRequestSerializer, RecommendedCourseSerializer,
    RecommendedCourseSummarySerializer, PlaceSummarySerializer, ModifyRequestSerializer,
)
from apps.recommendation.engine import generate_all_courses
from apps.recommendation.engine_provider import get_routing_engine
from apps.nlp.modification_interpreter import parse_modification_request
from allauth.socialaccount.providers.google.views import GoogleOAuth2Adapter
from allauth.socialaccount.providers.oauth2.client import OAuth2Client
from dj_rest_auth.registration.views import SocialLoginView
from django.db import transaction

class TripRequestCreateView(APIView):
    def post(self, request):
        serializer = TripRequestSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)

        try:
            trip = serializer.save(user=request.user if request.user.is_authenticated else None)
            routing_engine = get_routing_engine()
            courses = generate_all_courses(trip, routing_engine)
        except Exception as e:
            return Response({"trip_id": trip.id, "error": str(e)}, status=status.HTTP_500_INTERNAL_SERVER_ERROR)
        return Response({"trip_id": trip.id, "course_ids": {c.mode: c.id for c in courses}},
                         status=status.HTTP_201_CREATED)

    def get(self, request):
        """GET /api/trips/ — 내가 지금까지 만든 여행 요청 목록 (지난 추천 코스 이력)"""
        if not request.user.is_authenticated: 
            return Response([])
        
        trips = TripRequest.objects.filter(user=request.user).order_by("-created_at")
        result = [{
            "trip_id": t.id, "start_datetime": t.start_datetime, "end_datetime": t.end_datetime,
            "created_at": t.created_at,
            "courses": RecommendedCourseSummarySerializer(t.courses.all(), many=True).data,
        } for t in trips]
        return Response(result)


class TripCoursesListView(APIView):
    def get(self, request, trip_id):
        trip = get_object_or_404(TripRequest, id=trip_id)
        courses = trip.courses.all().prefetch_related("days__items")
        serializer = RecommendedCourseSummarySerializer(courses, many=True)
        return Response({"trip_id": trip.id, "courses": serializer.data})


class CourseSelectView(APIView):
    def post(self, request, course_id):
        course = get_object_or_404(RecommendedCourse, id=course_id)
        RecommendedCourse.objects.filter(trip=course.trip).update(is_selected=False)
        course.is_selected = True
        course.save(update_fields=["is_selected"])
        return Response({"course_id": course.id, "is_selected": True})


class CourseDetailView(APIView):
    def get(self, request, course_id):
        # 시리얼라이저가 trip.start_datetime/end_datetime을 타므로 같이 당겨온다.
        course = get_object_or_404(RecommendedCourse.objects.select_related("trip"), id=course_id)
        return Response(RecommendedCourseSerializer(course).data)


class CoursePlacesView(APIView):
    def get(self, request, course_id):
        course = get_object_or_404(RecommendedCourse, id=course_id)
        places = [item.place for day in course.days.all() for item in day.items.all()]
        unique_places = {p.content_id: p for p in places}.values()
        return Response({"course_id": course.id, "places": PlaceSummarySerializer(unique_places, many=True).data})


class CourseLodgingOptionsView(APIView):
    """★ 수정: Day 단위 → 코스(여행) 전체 단위. Lodging 모델 없이 스냅샷 JSON만 반환."""
    def get(self, request, course_id):
        course = get_object_or_404(RecommendedCourse, id=course_id)
        first_day = course.days.exclude(lodging_options_snapshot=[]).first()
        if not first_day:
            return Response({"lodging_options": []})
        return Response({
            "current_selected": first_day.lodging_snapshot,
            "lodging_options": first_day.lodging_options_snapshot,
        })

# 선택된 숙소
class CourseSelectLodgingView(APIView):
    """★ 수정: lodging_id(Django PK) → content_id(accommodations 카드 식별자) 기반."""
    def post(self, request, course_id):
        course = get_object_or_404(RecommendedCourse, id=course_id)
        content_id = request.data.get("content_id")
        if not content_id:
            return Response({"error": "content_id가 필요합니다."}, status=status.HTTP_400_BAD_REQUEST)

        updated = 0
        for day in course.days.all():
            match = next((c for c in day.lodging_options_snapshot if c.get("content_id") == content_id), None)
            if match:
                day.lodging_snapshot = match   # ← 이게 "숙소가 선택됐다"는 표시 그 자체
                day.save(update_fields=["lodging_snapshot"])
                updated += 1

        if not updated:
            return Response({"error": "추천 목록에 없는 숙소입니다."}, status=status.HTTP_400_BAD_REQUEST)

        # ★ 신규: 숙소 선택 = 이 코스가 최종 확정됐다는 뜻이므로, 코스도 함께 선택 처리
        RecommendedCourse.objects.filter(trip=course.trip).update(is_selected=False)
        course.is_selected = True
        course.save(update_fields=["is_selected"])

        return Response({
            "course_id": course.id,
            "is_selected": True,
            "selected_content_id": content_id,
            "days_updated": updated,
        })


class CourseModifyView(APIView):
    def post(self, request, course_id):
        course = get_object_or_404(RecommendedCourse, id=course_id)
        serializer = ModifyRequestSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        raw_message = serializer.validated_data["raw_message"]
        delta = parse_modification_request(raw_message)
        log = course.modification_logs.create(raw_message=raw_message, parsed_delta=delta)
        return Response({"log_id": log.id, "parsed_delta": delta,
                          "message": "수정 요청이 저장되었습니다. 재계산은 준비 중입니다."},
                         status=status.HTTP_202_ACCEPTED)

class GoogleLoginView(SocialLoginView):
    adapter_class = GoogleOAuth2Adapter
    client_class = OAuth2Client
    # React 개발 서버 주소. 배포 후에는 실제 배포된 프론트 주소로 바꿔야 함
    callback_url = "http://localhost:3000"

# 코스 저장/삭제
class CourseSaveView(APIView):
    """POST /api/courses/{course_id}/save  DELETE"""

    def post(self, request, course_id):
        course = get_object_or_404(RecommendedCourse, id=course_id)
        course.is_saved = True     
        course.save(update_fields=["is_saved"])  
        return Response({"course_id": course.id, "is_saved": True})

    def delete(self, request, course_id):
        course = get_object_or_404(RecommendedCourse, id=course_id)
        course.is_saved = False
        course.save(update_fields=["is_saved"])
        return Response({"course_id": course.id, "is_saved": False})

# 코스 저장 조회
class SavedCourseListView(APIView):
    """GET /api/courses/saved/ — 저장함 목록"""

    def get(self, request):
        if not request.user.is_authenticated:
            return Response([])
        courses = RecommendedCourse.objects.filter(
            trip__user=request.user, is_saved=True
        ).select_related("trip").prefetch_related("days__items")
        return Response(RecommendedCourseSummarySerializer(courses, many=True).data)
