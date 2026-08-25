"""
TourAPI CSV(관광지,문화시설,쇼핑,음식점,숙박)을 테이블에 적재

Place 테이블: 관광지, 문화시설, 쇼핑, 음식점
Lodging 테이블: 숙박

사용법:
    python manage.py import_tour_api --type 관광지 경로/관광지.csv
    python manage.py import_tour_api --type 음식점 경로/음식점.csv
    python manage.py import_tour_api --type 숙박 경로/숙박.csv
"""
import csv
from django.core.management.base import BaseCommand, CommandError
from apps.places.models import Place, Lodging

# 음식점 소분류
FOOD_ROLE_MAP = {
    "관광식당": "RESTAURANT", "일식": "RESTAURANT", "서양식": "RESTAURANT",
    "중식": "RESTAURANT", "기타외국식": "RESTAURANT", "퓨전음식": "RESTAURANT",
    "카페": "CAFE", "찻집": "CAFE", "기타음료점": "CAFE", "제과": "CAFE",
    "김밥 분식": "SNACK", "피자, 햄버거, 샌드위치 및 유사음식": "SNACK", "이동음식": "SNACK",
    "기타주점": "BAR",
}

class Command(BaseCommand):
     help = "TourAPI CSV를 Place 또는 Lodging테이블에 적재"

     def add_arguments(self, parser):
        parser.add_arguments("csv_path", type=str, help="CSV 파일 경로")
        parser.add_arguments(
            "--type", required=True,
            choices=["관광지", "문화시설", "쇼핑", "음식점", "숙박"],
            help="이 CSV가 어떤 콘텐츠 타입인지 명시"
        )

    def handle(self, *args, **options):
        csv_path = options["csv_path"]
        content_type = options["type"]

        try:
            f = open(csv_path, encoding='UTF-8-sig')
        except FileNotFoundError:
            raise CommandError(f"파일을 찾을 수 없습니다: {csv_path}")

        reader = csv.DictReader(f)
        count = 0

        with f:
            for row in reader:
                if content_type == "숙박":
                    self._import_lodging_row(row)
                else:
                    self._import_place_row(row, content_type)
                count += 1

        self.stdout.write(self.style.SUCCESS(f"[{content_type}] {count}건 적재 완료"))

    def _import_place_row(self, row:dict, content_type: str):
        # 관광지,문화시설,쇼핑,음식점 공통 처리
        # 컬럼 없는 타임은 .get()으로 안전하게 스킵

        small_cat = row.get("small_category_name", "")
        food_role = FOOD_ROLE_MAP.get(small_cat, "") if content_type == "음식점" else ""

        # Place테이블에 적재
        Place.objects.update_or_create(
            content_id=row["content_id"],
            defaults=dict(
                content_type_id=row.get("content_type_id", ""),
                content_type_name=row.get("content_type_name", content_type),
                title=row.get("title", ""),
                address=row.get("address", ""),
                zipcode=row.get("zipcode", ""),
                longitude=float(row["longitude"]) if row.get("longitude") else 0.0,
                latitude=float(row["latitude"]) if row.get("latitude") else 0.0,
                overview=row.get("overview", ""),
                homepage=self._clean_unknown(row.get("homepage", "")),
                contact=row.get("contact", ""),
                hours_raw=row.get("hours", ""),
                closed_days_raw=row.get("closed_days", ""),
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
                region_code=row.get("region_code", ""),
                signgu_code=row.get("signgu_code", ""),
                large_category_code=row.get("large_category_code", ""),
                large_category_name=row.get("large_category_name", ""),
                middle_category_code=row.get("middle_category_code", ""),
                middle_category_name=row.get("middle_category_name", ""),
                small_category_code=row.get("small_category_code", ""),
                small_category_name=small_cat,
            ),
        )

    def _import_lodging_row(self, row:dict):
        # 숙박 전용 컬럼 처리
        Lodging.objects.update_or_create(
            content_id=row["content_id"],
            defaults=dict(
                title=row.get("title", ""),
                address=row.get("address", ""),
                longitude=float(row["longitude"]) if row.get("longitude") else 0.0,
                latitude=float(row["latitude"]) if row.get("latitude") else 0.0,
                overview=row.get("overview", ""),
                contact=row.get("contact", ""),
                small_category_name=row.get("small_category_name", ""),
                signgu_code=row.get("signgu_code", ""),
                room_capacity_summary=self._clean_unknown(row.get("room_capacity_summary", "")),
                capacity=self._clean_unknown(row.get("capacity", "")),
                room_type=self._clean_unknown(row.get("room_type", "")),
                room_count=self._clean_unknown(row.get("room_count", "")),
                room_options=self._clean_unknown(row.get("room_options", "")),
                parking=row.get("parking", ""),
                cooking=self._clean_unknown(row.get("cooking", "")),
                facilities=self._clean_unknown(row.get("facilities", "")),
                check_in_time=row.get("check_in_time", ""),
                check_out_time=row.get("check_out_time", ""),
                min_room_price=self._clean_unknown(row.get("min_room_price", "")),
                pickup=self._clean_unknown(row.get("pickup", "")),
                food_place=self._clean_unknown(row.get("food_place", "")),
                pet_allowed_type=self._clean_unknown(row.get("pet_allowed_type", "")),
                pet_allowed_animals=self._clean_unknown(row.get("pet_allowed_animals", "")),
                pet_requirements=self._clean_unknown(row.get("pet_requirements", "")),
                reservation_info=self._clean_unknown(row.get("reservation_info", "")),
                reservation_url=self._clean_unknown(row.get("reservation_url", "")),
                refund_policy=self._clean_unknown(row.get("refund_policy", "")),
            ),
        )
    
    @staticmethod
    def _clean_unknown(values: str) -> str:
        return value if value != "Unknown" else "Unknown"