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

class TripRequestCreateView(APIView):
    def post(self, request):
        serializer = TripRequestSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        trip = serializer.save(user=request.user if request.user.is_authenticated else None)
        routing_engine = get_routing_engine()
        try:
            courses = generate_all_courses(trip, routing_engine)
        except Exception as e:
            return Response({"trip_id": trip.id, "error": str(e)}, status=status.HTTP_500_INTERNAL_SERVER_ERROR)
        return Response({"trip_id": trip.id, "course_ids": {c.mode: c.id for c in courses}},
                         status=status.HTTP_201_CREATED)

    def get(self, request):
        """GET /api/trips/ — 내가 지금까지 만든 여행 요청 목록 (지난 추천 코스 이력)"""
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
        serializer = RecommendedCourseSummarySerializer(trip.courses.all(), many=True)
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
        course = get_object_or_404(RecommendedCourse, id=course_id)
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
                day.lodging_snapshot = match
                day.save(update_fields=["lodging_snapshot"])
                updated += 1
        if not updated:
            return Response({"error": "추천 목록에 없는 숙소입니다."}, status=status.HTTP_400_BAD_REQUEST)
        return Response({"course_id": course.id, "selected_content_id": content_id, "days_updated": updated})


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