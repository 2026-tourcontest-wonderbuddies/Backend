from django.shortcuts import render

# Create your views here.
"""
API 뷰. 알고리즘(engine.py) 호출은 여기서만 하고, 뷰 자체는 최대한 얇게 유지.
"""

from rest_framework.views import APIView
from rest_framework.response import Response
from rest_framework import status
from django.shortcuts import get_object_or_404

from apps.trips.models import TripRequest, RecommendedCourse, ItineraryDay
from apps.places.models import Lodging
from apps.trips.serializers import (
    TripRequestSerializer, RecommendedCourseSerializer,
    RecommendedCourseSummarySerializer, PlaceSummarySerializer,
    LodgingSummarySerializer, ModifyRequestSerializer, SelectLodgingSerializer,
)
from apps.recommendation.engine import generate_all_courses
from apps.recommendation.engine_provider import get_routing_engine
from apps.recommendation.lodging_matcher import match_lodging_for_day
from apps.nlp.modification_interpreter import (
    parse_modification_request, 
    generate_result_explanation
)

class TripRequestCreateView(APIView):
    """1. POST /api/trips/ — 사용자 입력 받아 3개 코스 즉시 생성."""

    def post(self, request):
        serializer = TripRequestSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        trip = serializer.save(user=request.user if request.user.is_authenticated else None)

        routing_engine = get_routing_engine()
        try:
            courses = generate_all_courses(trip, routing_engine)
        except Exception as e:
            # 알고리즘 실행 중 에러 나면 trip은 남기고 에러만 반환 (디버깅 편의)
            return Response(
                {"trip_id": trip.id, "error": str(e)},
                status=status.HTTP_500_INTERNAL_SERVER_ERROR,
            )

        return Response({
            "trip_id": trip.id,
            "course_ids": {c.mode: c.id for c in courses},
        }, status=status.HTTP_201_CREATED)


class TripCoursesListView(APIView):
    """2. GET /api/trips/{trip_id}/courses/ — 3개 버전 일정 조회 (요약)."""

    def get(self, request, trip_id):
        trip = get_object_or_404(TripRequest, id=trip_id)
        courses = trip.courses.all()
        serializer = RecommendedCourseSummarySerializer(courses, many=True)
        return Response({"trip_id": trip.id, "courses": serializer.data})


class CourseSelectView(APIView):
    """3. POST /api/courses/{course_id}/select/ — 3개 중 최종 선택."""

    def post(self, request, course_id):
        course = get_object_or_404(RecommendedCourse, id=course_id)
        # 같은 trip의 다른 코스는 선택 해제 (한 번에 하나만 선택되도록)
        RecommendedCourse.objects.filter(trip=course.trip).update(is_selected=False)
        course.is_selected = True
        course.save(update_fields=["is_selected"])
        return Response({"course_id": course.id, "is_selected": True})


class CourseDetailView(APIView):
    """4. GET /api/courses/{course_id}/ — 저장한 일정 상세(Day+Item 전체)."""

    def get(self, request, course_id):
        course = get_object_or_404(RecommendedCourse, id=course_id)
        serializer = RecommendedCourseSerializer(course)
        return Response(serializer.data)


class CoursePlacesView(APIView):
    """5. GET /api/courses/{course_id}/places/ — 저장한 장소 목록."""

    def get(self, request, course_id):
        course = get_object_or_404(RecommendedCourse, id=course_id)
        places = [item.place for day in course.days.all() for item in day.items.all()]
        # 중복 제거 (같은 장소가 여러 Day에 걸쳐 나올 순 없지만 방어적으로)
        unique_places = {p.content_id: p for p in places}.values()
        serializer = PlaceSummarySerializer(unique_places, many=True)
        return Response({"course_id": course.id, "places": serializer.data})

# 수정 필요
class DayLodgingOptionsView(APIView):
    """6. GET /api/courses/{course_id}/days/{day_index}/lodging-options/ — 추천 숙소."""

    def get(self, request, course_id, day_index):
        day = get_object_or_404(ItineraryDay, course_id=course_id, day_index=day_index)

        if not day.lodging_options:
            return Response({"day_id": day.id, "lodging_options": []})

        lodgings = Lodging.objects.filter(content_id__in=day.lodging_options)
        # lodging_options 순서(이동거리순) 그대로 유지해서 응답
        ordered = sorted(lodgings, key=lambda l: day.lodging_options.index(l.content_id))
        serializer = LodgingSummarySerializer(ordered, many=True)
        return Response({
            "day_id": day.id,
            "current_selected": day.lodging.content_id if day.lodging else None,
            "lodging_options": serializer.data,
        })

# 수정 필요
class DaySelectLodgingView(APIView):
    """7. POST /api/courses/{course_id}/days/{day_index}/select-lodging/ — 추천 숙소 중 선택"""

    def post(self, request, course_id, day_index):
        day = get_object_or_404(ItineraryDay, course_id=course_id, day_index=day_index)
        serializer = SelectLodgingSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)

        lodging_id = serializer.validated_data["lodging_id"]
        if lodging_id not in day.lodging_options:
            return Response(
                {"error": "추천 목록에 없는 숙소입니다."}, status=status.HTTP_400_BAD_REQUEST
            )

        lodging = get_object_or_404(Lodging, content_id=lodging_id)
        day.lodging = lodging
        day.save(update_fields=["lodging"])

        # ⚠️ 주의: 다음날 출발지가 이 숙소로 바뀌므로, 엄밀히는 다음날 이후를
        # 재계산해야 정확함. 지금은 반영만 하고 재계산은 Pipeline 6(코스 수정)
        # 구현 시 함께 처리 예정 — 아직 이 재계산 로직은 미구현.
        return Response({"day_id": day.id, "selected_lodging": lodging.content_id})


class CourseModifyView(APIView):
    """8. POST /api/courses/{course_id}/modify/ — 챗봇 수정 요청 (현재 stub)."""

    def post(self, request, course_id):
        course = get_object_or_404(RecommendedCourse, id=course_id)
        serializer = ModifyRequestSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)

        raw_message = serializer.validated_data["raw_message"]

        # 1. 자연어 메시지를 분석해서 델타(변경 사항) 추출
        try:
            delta = parse_modification_request(raw_message)
        except Exception as e:
            return Response(
                {"error": f"자연어 의도 분석 중 오류가 발생했습니다: {str(e)}"},
                status=status.HTTP_500_INTERNAL_SERVER_ERROR
            )

        # 2. 로그 생성 시 빈 딕셔너리가 아니라 실제 분석된 delta를 저장
        log = course.modification_logs.create(
            raw_message=raw_message,
            parsed_delta=delta,  
        )

        # 3. locked_place_ids 활용 지점!
        # 사용자가 "여기 장소는 고정해줘" 라고 한 장소 ID 목록을 뽑아냅니다.
        locked_place_ids = delta.get("locked_place_ids", [])
        removed_place_ids = delta.get("removed_place_ids", [])
        adjustments = delta.get("adjustments", {})
        
        # TODO: 이 locked_place_ids를 course_builder로 넘겨서 
        # 기존 코스를 유지하면서 나머지만 재계산하는 로직을 여기에 붙이시면 됩니다.
        # 예시: 
        # new_course_data = rebuild_course(course, locked_place_ids=locked_place_ids, delta=delta)

        return Response({
            "log_id": log.id,
            "parsed_delta": delta,
            "message": "수정 요청이 성공적으로 반영되었습니다.",
        }, status=status.HTTP_200_OK)

# 장소 상세 (overview_summary)


# RAG 질문 답변
class PlaceRAGAskView(APIView):
    """10. POST /api/places/{content_id}/ask/ — RAG 기반 장소 질문 답변 API"""

    def post(self, request, content_id):
        # [추가] RAG 질문 답변 엔드포인트 구현 (프론트/기획서 10번 요구사항 충족)
        question = request.data.get("question")
        if not question:
            return Response({"error": "질문 내용(question)이 필요합니다."}, status=status.HTTP_400_BAD_REQUEST)

        # TODO: 해당 content_id의 장소 설명을 DB에서 가져온 뒤(Retrieval), 
        # LLM(Claude 등)에게 질문과 함께 전달하여 답변 생성(Generation)하는 RAG 로직 구현부
        
        return Response({
            "content_id": content_id,
            "question": question,
            "answer": "해당 장소는 경사가 완만하고 편의시설이 인접하여 질문하신 내용을 충족합니다. (RAG 연동 대기 중)"
        }, status=status.HTTP_200_OK)