from django.shortcuts import render

# Create your views here.
"""
API 뷰. 알고리즘(engine.py) 호출은 여기서만 하고, 뷰 자체는 최대한 얇게 유지.
"""

from rest_framework.views import APIView
from rest_framework.response import Response
from rest_framework import status
from django.shortcuts import get_object_or_404

from apps.trips.models import TripRequest, RecommendedCourse, ItineraryDay, ItineraryItem
from apps.trips.serializers import (
    TripRequestSerializer, RecommendedCourseSerializer,
    RecommendedCourseSummarySerializer, PlaceSummarySerializer, ModifyRequestSerializer,
)
from apps.recommendation.engine import generate_all_courses
from apps.recommendation.engine_provider import get_routing_engine
from apps.nlp.modification_interpreter import parse_modification_request, generate_result_explanation
from apps.recommendation.course_modifier import (
    recalc_timeline_from, resequence_orders, regenerate_unlocked_segment, recalc_first_item_travel
)

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
            "trip_id": t.id, "start_date": t.start_date, "end_date": t.end_date,
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

        recalc_first_item_travel(course) 

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
        
        locked_names = set(delta.get("locked_place_ids", []))
        removed_names = set(delta.get("removed_place_ids", []))
        scope = delta.get("recompute_scope", "partial")

        before_summary = {"total_places": sum(d.items.count() for d in course.days.all())}
        affected_days = []

        if scope == "full":
            # 전면 재추천 — 기존 것 지우고 generate_one_course 재사용
            from apps.recommendation.engine import generate_one_course
            from apps.recommendation.engine_provider import get_routing_engine
            course.days.all().delete()
            new_course = generate_one_course(course.trip, get_routing_engine(), course.mode)
            course.final_score = new_course.final_score
            new_course.days.all().update(course=course)
            new_course.delete()
            affected_days = [d.day_index for d in course.days.all()]
        else:
            # 부분 재계산 — 이름으로 장소 매칭해서 고정/삭제 표시 후, 미고정 구간만 재탐색
            for day in course.days.all():
                changed = False
                for item in day.items.all():
                    if item.place.title in locked_names and not item.locked:
                        item.locked = True
                        item.save(update_fields=["locked"])
                        changed = True
                    if item.place.title in removed_names:
                        item.delete()
                        changed = True
                if changed:
                    from apps.recommendation.course_modifier import resequence_orders
                    resequence_orders(day)
                    regenerate_unlocked_segment(
                        day, day.course.trip.purpose_main, day.course.trip.purpose_sub, course.mode
                    )
                    affected_days.append(day.day_index)

        after_summary = {"total_places": sum(d.items.count() for d in course.days.all())}
        explanation = generate_result_explanation(raw_message, before_summary, after_summary)

        return Response({
            "log_id": log.id,
            "parsed_delta": delta,
            "affected_days": affected_days,
            "message": explanation,
        }, status=status.HTTP_200_OK)

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

# 코스 수정
class CourseItemReorderView(APIView):
    """POST /api/courses/{course_id}/days/{day_index}/reorder/"""
    def post(self, request, course_id, day_index):
        day = get_object_or_404(ItineraryDay, course_id=course_id, day_index=day_index)
        item_ids = request.data.get("item_ids")
        if not item_ids:
            return Response({"error": "item_ids가 필요합니다."}, status=status.HTTP_400_BAD_REQUEST)

        items = {i.id: i for i in day.items.all()}
        for new_order, item_id in enumerate(item_ids):
            if item_id not in items:
                return Response({"error": f"item_id {item_id}가 이코스에 없습니다."}, status=status.HTTP_400_BAD_REQUEST)    
            items[item_id].order = new_order
            items[item_id].save(update_fields=["order"])

        result = recalc_timeline_from(day, start_order=0)
        return Response({
            "day_index": day_index, "reordered": True,
            "over_budget": result["over_budget"],
            "message": "순서가 변경되어 이동시간과 시각이 재계산되었습니다." + (
                " ⚠ 가용시간을 초과했습니다." if result["over_budget"] else ""
            ),
        })

# 장소 삭제
class CourseItemDeleteView(APIView):
    """DELETE /api/courses/{course_id}/items/{item_id}/"""
    def delete(self, request, course_id, item_id):
        item = get_object_or_404(ItineraryItem, id=item_id, day__course_id=course_id)
        day = item.day
        deleted_order = item.order
        item.delete()

        resequence_orders(day)
        result = recalc_timeline_from(day, start_order=max(deleted_order - 1, 0))
        return Response({
            "deleted": True, "day_index": day.day_index,
            "message": "장소가 삭제되고 이후 일정이 재계산되었습니다.",
        })

# 장소 고정/해제
class CourseItemLockView(APIView): 
    """POST /api/courses/{course_id}/items/{item_id}/lock/  또는 DELETE로 해제"""
    def post(self, request, course_id, item_id):
        item = get_object_or_404(ItineraryItem, id=item_id, day__course_id=course_id)
        item.locked = True
        item.save(update_fields=["locked"])
        return Response({"item_id": item.id, "locked": True})

    def delete(self, request, course_id, item_id):
        item = get_object_or_404(ItineraryItem, id=item_id, day__course_id=course_id)
        item.locked = False
        item.save(update_fields=["locked"])
        return Response({"item_id": item.id, "locked": False})

# 장소 추가
class CourseItemAddView(APIView):
    def post(self, request, course_id, day_index):
        from apps.places.models import Place
        day = get_object_or_404(ItineraryDay, course_id=course_id, day_index=day_index)
        content_id = request.data.get("content_id")
        insert_order = request.data.get("order")
        if content_id is None or insert_order is None:
            return Response({"error": "content_id, order가 필요합니다."}, status=status.HTTP_400_BAD_REQUEST)

        place = get_object_or_404(Place, content_id=content_id)

        routing_engine = get_routing_engine()
        if content_id not in routing_engine._pos:
            return Response({"error": f"이 장소({place.title})는 이동시간 계산이 불가능해 추가할 수 없습니다."},
                             status=status.HTTP_400_BAD_REQUEST)

        for it in list(day.items.filter(order__gte=insert_order).order_by("-order")):   # ★ list()로 감싸서 안전하게
            it.order += 1
            it.save(update_fields=["order"])

        # ★ 수정: trip.start_datetime 대신, 그날 날짜+시작시각 조합으로 임시값 생성 (recalc_timeline_from이 바로 덮어씀)
        target_date = day.course.trip.start_date + timedelta(days=day.day_index - 1)
        temp_dt = datetime(target_date.year, target_date.month, target_date.day,
                            day.avail_start_min // 60, day.avail_start_min % 60, tzinfo=KST)

        ItineraryItem.objects.create(
            day=day, order=insert_order, place=place, slot_type="GENERAL",
            arrive_at=temp_dt, depart_at=temp_dt,
            travel_min_from_prev=0,
        )
        result = recalc_timeline_from(day, start_order=max(insert_order - 1, 0))
        return Response({
            "added": True, "day_index": day_index,
            "over_budget": result["over_budget"],
            "message": "장소가 추가되고 이후 일정이 재계산되었습니다." + (
                " ⚠ 가용시간을 초과했습니다." if result["over_budget"] else ""
            ),
        })

