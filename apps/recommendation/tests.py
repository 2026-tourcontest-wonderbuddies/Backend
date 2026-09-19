from datetime import datetime
from types import SimpleNamespace

from datetime import date
from pathlib import Path
from unittest import mock

from django.test import SimpleTestCase, TestCase

from apps.recommendation.constraints import (
    classify_quadrant,
    calc_avail_hours_from_schedule,
    calc_target_slots,
    check_night_spot_flag,
)
import pandas as pd

from apps.places.management.commands.import_tour_api import FOOD_ROLE_MAP
from apps.places.models import Place
from apps.recommendation import engine
from apps.recommendation.engine import _night_pool
from apps.routing.hybrid_engine import HybridRoutingEngine
from apps.trips.models import TripRequest


class NightSpotFlagTests(SimpleTestCase):
    def test_threshold_is_strictly_after_2100(self):
        self.assertFalse(check_night_spot_flag(20 * 60))
        self.assertFalse(check_night_spot_flag(21 * 60))
        self.assertTrue(check_night_spot_flag(21 * 60 + 15))

    def test_night_spot_adds_one_slot(self):
        self.assertEqual(
            calc_target_slots(12.0, "dist", need_night_spot=True),
            calc_target_slots(12.0, "dist") + 1,
        )

    def test_last_day_never_gets_night_spot(self):
        def need_night(day_index, total_days):
            return calc_avail_hours_from_schedule(
                day_index, total_days, datetime(2026, 9, 1, 9, 0), datetime(2026, 9, 1, 23, 59)
            ).need_night_spot

        self.assertTrue(need_night(1, 3))   # 입도일
        self.assertTrue(need_night(2, 3))   # 중간일
        self.assertFalse(need_night(3, 3))  # 출도일
        self.assertFalse(need_night(1, 1))  # 당일치기


class NightPoolTests(SimpleTestCase):
    def _places(self):
        def p(cid, quad, night=True):
            return SimpleNamespace(content_id=cid, quadrant=quad, is_night_spot=night)
        return [p("sw", "SW"), p("nw", "NW"), p("se", "SE"), p("ne", "NE"), p("no", "SW", night=False)]

    def test_same_quadrant_first(self):
        ids = {p.content_id for p in _night_pool(self._places(), "SW", set())}
        self.assertEqual(ids, {"sw"})

    def test_expands_to_adjacent_only_when_exhausted(self):
        ids = {p.content_id for p in _night_pool(self._places(), "SW", {"sw"})}
        self.assertEqual(ids, {"nw", "se"})   # 대각선 NE는 제외

    def test_all_region_and_empty(self):
        self.assertEqual(len(_night_pool(self._places(), None, set())), 4)
        self.assertEqual(_night_pool(self._places(), "SW", {"sw", "nw", "se"}), [])


class _FakeRouting:
    def __init__(self, ids):
        self._pos = {i: 0 for i in ids}

    def get_travel_time(self, origin, dest, **kwargs):
        return {"duration_min_adjusted": 10}


class NightSpotPlacementTests(TestCase):
    """need_night_spot인 날의 마지막(저녁 이후) 구간에 야간 명소가 배치되는지."""

    def _place(self, cid, quad, kind="관광지", night=False):
        return Place.objects.create(
            content_id=cid, content_type_id="12", content_type_name=kind, title=cid, address="",
            longitude=126.5, latitude=33.4, region_code="39", signgu_code="", quadrant=quad,
            stay_time_minutes=60, stay_min=30, stay_max=90, hours_status="always",
            satisfaction_score=0.5, popularity_score=0.0, is_night_spot=night,
            food_role="RESTAURANT" if kind == "음식점" else "",
        )

    def _generate(self, days, region="SW", with_night=True):
        for i in range(8):
            self._place(f"g{i}", "SW")
        for i in range(3):
            self._place(f"f{i}", "SW", kind="음식점")
        if with_night:
            self._place("night_sw", "SW", night=True)
            self._place("night_nw", "NW", night=True)
            self._place("night_ne", "NE", night=True)
        trip = TripRequest.objects.create(
            start_date=date(2026, 10, 1), end_date=date(2026, 10, 3), purpose_main="nature",
            region_preference=region,
            day_schedules=[{"day_index": i + 1, "start_time": "09:00", "end_time": e} for i, e in enumerate(days)],
        )
        engine._place_cache.update(general=None, food=None)
        ids = set(Place.objects.values_list("content_id", flat=True))
        with mock.patch.object(engine, "get_lodging_anchor", return_value=[]):
            return engine.generate_one_course(trip, _FakeRouting(ids), "pref")

    def test_last_item_is_night_spot_then_adjacent_when_exhausted(self):
        course = self._generate(["22:00", "22:00", "18:00"])
        d1, d2, d3 = course.days.order_by("day_index")
        self.assertTrue(d1.need_night_spot and d2.need_night_spot)
        self.assertFalse(d3.need_night_spot)   # 출도일
        self.assertEqual(d1.items.last().place_id, "night_sw")
        self.assertGreaterEqual(d1.items.last().arrive_at.astimezone(engine.KST).hour, 20)   # 저녁 식사 이후 구간
        self.assertEqual(d2.items.last().place_id, "night_nw")   # SW 소진 -> 인접, 대각선 NE 제외

    def test_no_candidate_drops_night_slot(self):
        course = self._generate(["22:00", "18:00"], with_night=False)
        d1 = course.days.get(day_index=1)
        self.assertFalse(d1.need_night_spot)
        self.assertEqual(d1.target_slots, calc_target_slots(d1.avail_hours, "pref"))   # +1 취소


DATA_DIR = Path(__file__).resolve().parent.parent.parent / "data"


class RealRoutingNightTests(TestCase):
    """실제 장소 CSV + 실제 이동시간 행렬로, 야간 구간이 비거나 낭비되지 않는지."""

    @classmethod
    def setUpTestData(cls):
        cls.routing = HybridRoutingEngine()
        rows = []
        for fname, is_food in (("jeju_places_stay_time.csv", False), ("jeju_restaurants_stay_time.csv", True)):
            df = pd.read_csv(DATA_DIR / fname, encoding="utf-8-sig", dtype={"content_id": str}, keep_default_na=False)
            for r in df.to_dict("records"):
                if r["content_id"] not in cls.routing._pos:
                    continue
                rows.append(Place(
                    content_id=r["content_id"], content_type_id=str(r["content_type_id"]),
                    content_type_name=r["content_type_name"], title=r["title"], address=r["address"],
                    longitude=float(r["longitude"]), latitude=float(r["latitude"]),
                    region_code=str(r["region_code"]), signgu_code=str(r["signgu_code"]),
                    small_category_name=r["small_category_name"], middle_category_name=r["middle_category_name"],
                    quadrant=classify_quadrant(float(r["latitude"]), float(r["longitude"])),
                    score_nature=int(r["Score_Nature"] or 0), score_food=int(r["Score_Food"] or 0),
                    score_photo=int(r["Score_Photo"] or 0), score_culture=int(r["Score_Culture"] or 0),
                    score_activity=int(r["Score_Activity"] or 0), score_shopping=int(r["Score_Shopping"] or 0),
                    stay_time_minutes=int(r["stay_time_minutes"]), stay_min=int(r["stay_min"]), stay_max=int(r["stay_max"]),
                    satisfaction_score=float(r["satisfaction_score"] or 0.5),
                    popularity_score=float(r["popularity_score"] or 0.0),
                    is_night_spot=str(r.get("is_night_spot", "")).lower() == "true",
                    food_role=FOOD_ROLE_MAP.get(r["small_category_name"], "") if is_food else "",
                ))
        Place.objects.bulk_create(rows)

    def _day1(self, end_time, region="SW"):
        trip = TripRequest.objects.create(
            start_date=date(2026, 10, 1), end_date=date(2026, 10, 2), purpose_main="nature", region_preference=region,
            day_schedules=[
                {"day_index": 1, "start_time": "09:00", "end_time": end_time},
                {"day_index": 2, "start_time": "09:00", "end_time": "18:00"},
            ],
        )
        engine._place_cache.update(general=None, food=None)
        with mock.patch.object(engine, "get_lodging_anchor", return_value=[]):
            course = engine.generate_one_course(trip, self.routing, "pref")
        day = course.days.get(day_index=1)
        after_dinner = [
            i for i in day.items.all() if i.arrive_at.astimezone(engine.KST).hour >= 20
        ]
        return day, after_dinner

    def test_short_night_segment_is_not_left_empty(self):
        # SW 야간 명소(루나폴 체류 90분·불란지야시장 60분)는 75분 구간에 거의 안 들어간다 -> 폴백이 채워야 한다.
        day, after_dinner = self._day1("21:15")
        self.assertTrue(day.need_night_spot)
        self.assertGreaterEqual(len(after_dinner), 1)

    def test_long_night_segment_is_filled_after_night_spot(self):
        # 종료 23:59면 구간이 239분 — 야간 명소 1곳(30~90분) 뒤 남는 시간도 일반 후보로 채운다.
        day, after_dinner = self._day1("23:59")
        self.assertGreaterEqual(len(after_dinner), 2)
        self.assertLessEqual(max(i.depart_at for i in after_dinner).astimezone(engine.KST).hour, 23)
