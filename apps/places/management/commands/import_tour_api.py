"""
TourAPI CSV(관광지,문화시설,쇼핑,음식점,숙박)을 테이블에 적재

Place 테이블: 관광지, 문화시설, 쇼핑, 음식점
Lodging 테이블: 숙박

사용법:
    python manage.py import_tour_api --type 관광지문화쇼핑 경로/제주_관광지_문화시설_쇼핑_체류시간.csv
    python manage.py import_tour_api --type 음식점 경로/제주_음식점_체류시간.csv
    python manage.py import_tour_api --type 숙박 경로/숙박_tripcom_matched.csv
"""
import csv
from django.core.management.base import BaseCommand, CommandError
from apps.places.models import Place, Lodging
from apps.recommendation.constraints import classify_quadrant

# 음식점 소분류
FOOD_ROLE_MAP = {
    "관광식당": "RESTAURANT", "일식": "RESTAURANT", "서양식": "RESTAURANT",
    "중식": "RESTAURANT", "기타외국식": "RESTAURANT", "퓨전음식": "RESTAURANT",
    "카페": "CAFE", "찻집": "CAFE", "기타음료점": "CAFE", "제과": "CAFE",
    "김밥 분식": "SNACK", "피자, 햄버거, 샌드위치 및 유사음식": "SNACK", "이동음식": "SNACK",
    "기타주점": "BAR",
}

class Command(BaseCommand):
    help = "최종 CSV(관광지문화쇼핑/음식점/숙박)를 Place/Lodging에 적재한다."

    def add_arguments(self, parser):
        parser.add_argument("csv_path", type=str)
        parser.add_argument(
            "--type", required=True,
            choices=["관광지문화쇼핑", "음식점", "숙박"],
        )

    def handle(self, *args, **options):
        csv_path = options["csv_path"]
        data_type = options["type"]

        try:
            f = open(csv_path, encoding="utf-8-sig")
        except FileNotFoundError:
            raise CommandError(f"파일을 찾을 수 없습니다: {csv_path}")

        reader = csv.DictReader(f)
        count = 0

        with f:
            for row in reader:
                if data_type == "숙박":
                    self._import_lodging_row(row)
                else:
                    self._import_place_row(row, is_food=(data_type == "음식점"))
                count += 1

        self.stdout.write(self.style.SUCCESS(f"[{data_type}] {count}건 적재 완료"))
        if data_type != "숙박":
            self.stdout.write(self.style.WARNING(
                "hours_status는 전부 'uncertain'으로 들어갔습니다. "
                "팀의 hours_cache.json을 받으면 apply_hours_cache 커맨드로 갱신하세요."
            ))

    def _import_place_row(self, row: dict, is_food: bool):
        content_id = row["content_id"]
        lat = float(row["latitude"])
        lng = float(row["longitude"])
        small_cat = row.get("small_category_name", "")

        food_tags = []
        if is_food and row.get("food_tags"):
            food_tags = [t.strip() for t in row["food_tags"].split(",") if t.strip()]

        food_role = FOOD_ROLE_MAP.get(small_cat, "") if is_food else ""

        Place.objects.update_or_create(
            content_id=content_id,
            defaults=dict(
                content_type_id=row.get("content_type_id", ""),
                content_type_name=row.get("content_type_name", ""),
                title=row.get("title", ""),
                address=row.get("address", ""),
                zipcode=row.get("zipcode", ""),
                longitude=lng,
                latitude=lat,
                overview=row.get("overview", ""),
                homepage=self._clean(row.get("homepage", "")),
                contact=row.get("contact", ""),

                hours_raw=row.get("hours", ""),
                closed_days_raw=row.get("closed_days", ""),
                hours_status="uncertain",   # 팀 캐시 도착 전 임시값
                open_windows={},
                closed_weekdays=[],
                hours_uncertain=True,

                fees=row.get("fees", ""),
                parking=row.get("parking", ""),
                restroom=row.get("restroom", ""),
                credit_card=row.get("credit_card", ""),
                detail_information=row.get("detail_information", ""),

                market_days=row.get("market_days", ""),
                sale_items=row.get("sale_items", ""),

                featured_menu=row.get("featured_menu", ""),
                menu=row.get("menu", ""),
                food_role=food_role,
                food_tags=food_tags,

                region_code=row.get("region_code", ""),
                signgu_code=row.get("signgu_code", ""),
                large_category_code=row.get("large_category_code", ""),
                large_category_name=row.get("large_category_name", ""),
                middle_category_code=row.get("middle_category_code", ""),
                middle_category_name=row.get("middle_category_name", ""),
                small_category_code=row.get("small_category_code", ""),
                small_category_name=small_cat,

                quadrant=classify_quadrant(lat, lng),

                # LLM 목적 태그 (CSV의 Score_* 컬럼, 대문자 그대로임에 주의)
                score_nature=int(row.get("Score_Nature", 0) or 0),
                score_food=int(row.get("Score_Food", 0) or 0),
                score_photo=int(row.get("Score_Photo", 0) or 0),
                score_culture=int(row.get("Score_Culture", 0) or 0),
                score_activity=int(row.get("Score_Activity", 0) or 0),
                score_shopping=int(row.get("Score_Shopping", 0) or 0),

                # 체류시간·품질 (컬럼명이 CSV마다 stay_time_minutes로 통일돼있음)
                stay_time_minutes=int(row["stay_time_minutes"]),
                stay_min=int(row["stay_min"]),
                stay_max=int(row["stay_max"]),
                stay_src=row.get("stay_src", ""),
                qual=self._to_float_or_none(row.get("qual", "")),
                qual_rel=self._to_float_or_none(row.get("qual_rel", "")),
                qual_category=self._to_float_or_none(row.get("qual_category", "")),
                satisfaction_score=self._to_float_or_none(row.get("satisfaction_score", "")),
                satisfaction_src=row.get("satisfaction_src", ""),
            ),
        )

    def _import_lodging_row(self, row: dict):
        import json
        content_id = row["content_id"]
        lat = float(row["latitude"])
        lng = float(row["longitude"])

        rooms_json = []
        raw_rooms = row.get("rooms_json", "")
        if raw_rooms:
            try:
                rooms_json = json.loads(raw_rooms)
            except (json.JSONDecodeError, TypeError):
                rooms_json = []  # 파싱 실패해도 적재는 계속 진행

        Lodging.objects.update_or_create(
            content_id=content_id,
            defaults=dict(
                title=row.get("title", ""),
                address=row.get("address", ""),
                longitude=lng,
                latitude=lat,
                overview=row.get("overview", ""),
                contact=row.get("contact", ""),
                small_category_name=row.get("small_category_name", ""),
                signgu_code=row.get("signgu_code", ""),
                quadrant=classify_quadrant(lat, lng),

                check_in_time=row.get("check_in_time", ""),
                check_out_time=row.get("check_out_time", ""),
                capacity=self._clean(row.get("capacity", "")),
                room_count=self._clean(row.get("room_count", "")),
                room_type=self._clean(row.get("room_type", "")),
                room_capacity_summary=self._clean(row.get("room_capacity_summary", "")),
                room_options=self._clean(row.get("room_options", "")),
                rooms_json=rooms_json,

                parking=row.get("parking", ""),
                cooking=self._clean(row.get("cooking", "")),
                facilities=self._clean(row.get("facilities", "")),
                pickup=self._clean(row.get("pickup", "")),
                food_place=self._clean(row.get("food_place", "")),
                refund_policy=self._clean(row.get("refund_policy", "")),
                min_room_price=self._clean(row.get("min_room_price", "")),
                reservation_info=self._clean(row.get("reservation_info", "")),
                reservation_url=self._clean(row.get("reservation_url", "")),

                pet_allowed_type=self._clean(row.get("pet_allowed_type", "")),
                pet_allowed_animals=self._clean(row.get("pet_allowed_animals", "")),
                pet_requirements=self._clean(row.get("pet_requirements", "")),
                pet_extra_info=self._clean(row.get("pet_extra_info", "")),

                tripcom_hotel_id="",  # 현재 CSV에 컬럼 없음 — 팀 확인 필요
            ),
        )

    @staticmethod
    def _clean(value: str) -> str:
        return "" if value in ("", "Unknown") else value

    @staticmethod
    def _to_float_or_none(value: str):
        value = (value or "").strip()
        return float(value) if value else None