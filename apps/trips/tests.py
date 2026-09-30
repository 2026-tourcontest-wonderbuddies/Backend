from datetime import date, datetime
from unittest import mock

from django.test import TestCase

from apps.places.models import Place
from apps.recommendation import course_modifier
from apps.recommendation.course_modifier import KST, recalc_timeline_from
from apps.trips.models import ItineraryDay, ItineraryItem, RecommendedCourse, TripRequest


class _Routing:
    """장소 간 10분, 'far' → 숙소 'L'만 40분."""

    _pos = {"near": 0, "far": 1, "L": 2}

    def get_travel_time(self, origin, dest, **kwargs):
        return {"duration_min_adjusted": 40 if (origin, dest) == ("far", "L") else 10}


def _place(cid, stay):
    return Place.objects.create(
        content_id=cid, content_type_id="12", content_type_name="관광지", title=cid, address="",
        longitude=126.5, latitude=33.4, region_code="39", signgu_code="", quadrant="SW",
        stay_time_minutes=stay, stay_min=stay, stay_max=stay, hours_status="always",
        satisfaction_score=0.5, popularity_score=0.0,
    )


@mock.patch.object(course_modifier, "get_routing_engine", _Routing)
class RecalcTimelineTests(TestCase):
    """코스 편집(추가·삭제·순서 변경)이 공통으로 타는 recalc_timeline_from."""

    def setUp(self):
        trip = TripRequest.objects.create(start_date=date(2026, 10, 1), end_date=date(2026, 10, 2), purpose_main="nature")
        course = RecommendedCourse.objects.create(trip=trip, mode="pref")
        self.day = ItineraryDay.objects.create(
            course=course, day_index=1, day_case="A", avail_hours=12, target_slots=2,
            avail_start_min=540, lodging_snapshot={"content_id": "L", "lat": 33.4, "lon": 126.5},
            travel_to_next_min=15,
        )
        # 다음 날이 있어야 DAY 1의 끝이 공항이 아니라 숙소가 된다.
        ItineraryDay.objects.create(course=course, day_index=2, day_case="C", avail_hours=9, target_slots=1)
        t = datetime(2026, 10, 1, 9, 0, tzinfo=KST)
        ItineraryItem.objects.create(day=self.day, order=0, place=_place("near", 60), arrive_at=t, depart_at=t)

    def _add(self, cid, stay):
        t = datetime(2026, 10, 1, 9, 0, tzinfo=KST)
        ItineraryItem.objects.create(day=self.day, order=1, place=_place(cid, stay), arrive_at=t, depart_at=t)
        return recalc_timeline_from(self.day, start_order=0)

    def test_lodging_travel_follows_new_last_place(self):
        self._add("far", 60)
        self.day.refresh_from_db()
        self.assertEqual(self.day.travel_to_next_min, 40)

    def test_over_budget_detected_past_midnight(self):
        # 09:00 시작 + 12시간 가용 → 21:00 끝. 체류 15시간이면 다음 날 01:20에 끝나 초과여야 한다.
        self.assertTrue(self._add("far", 15 * 60)["over_budget"])

    def test_within_budget(self):
        self.assertFalse(self._add("far", 60)["over_budget"])
