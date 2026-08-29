from django.db import models

"""
TourAPI(관광지/문화시설/쇼핑/음식점) + 숙박 + Ai Hub 파생 데이터
"""

# Create your models here.
class Place(models.Model):
    # 식별 기본 정보 (TourApi 원본 그대로)
    content_id = models.CharField(
        primary_key=True, max_length=20, help_text="TourAPI 고유 ID. 반드시 문자열로 다룰것(앞자리 0손실 방지)"
    )
    content_type_id = models.CharField(max_length=10)
    content_type_name = models.CharField(max_length=20, db_index=True) #관광지 / 문화시설 / 쇼핑 / 음식점
    title = models.CharField(max_length=200)
    address = models.CharField(max_length=300)
    zipcode = models.CharField(max_length=10, blank=True)
    longitude = models.FloatField()
    latitude = models.FloatField()
    overview = models.TextField( blank=True) # 장소 소개글. LLM 목적태깅(DescriptionFit)과 자유입력 임베딩 매칭에 씀
    homepage = models.URLField(blank=True)
    contact = models.CharField(max_length=100, blank=True)

    # 운영시간, 휴무일 (원본 텍스트 + 파싱 결과 분리 저장)
    hours_raw = models.CharField(max_length=100, blank=True) # 원문 그대로(예: '11:20~30 (마지막 주문 19:50)). 파싱 실패 시 대조용 남겨둠
    closed_days_raw = models.CharField(max_length=200, blank=True)

    HOURS_STATUS_CHOICES = [
        ("always", "상시영업"), ("windows", "시간대 확정"), ("uncertain", "확인불가"),
    ]

    hours_status = models.CharField(
        max_length=10, choices=HOURS_STATUS_CHOICES, default="uncertain"
    ) # build_hours_cache.py 결과. always/windows면 open_windows 참고
    open_windows = models.JSONField(
        default=dict, blank=True
    ) # 요일별 [[시작,종료], ...] 형태. 예: {'월':[['09:00','18:00']], ...}. ", "hours_status='always'면 빈 dict
    closed_weekdays = models.JSONField(default=list, blank=True, help_text="예: ['화']")
    hours_uncertain = models.BooleanField(default=False) # True면 확인필요, 하드필터에서 제외하지 않고 통과시킴

    #관광지/문화시설 공통
    fees = models.CharField(max_length=500, blank=True)
    parking = models.CharField(max_length=20, blank=True)
    restroom = models.CharField(max_length=50, blank=True)
    credit_card = models.CharField(max_length=20, blank=True)
    detail_information = models.TextField(blank=True)

    # --- 쇼핑 전용 ---
    market_days = models.CharField(max_length=100, blank=True)
    sale_items = models.CharField(max_length=300, blank=True)

    # --- 음식점 전용 ---
    featured_menu = models.CharField(max_length=300, blank=True)
    menu = models.TextField(blank=True)
    food_role = models.CharField(
        max_length=20, blank=True, db_index=True,
        help_text="RESTAURANT/CAFE/SNACK/BAR — small_category_name 기반 import 시 계산"
    )
    food_tags = models.JSONField(
        default=list, blank=True,
        help_text="LLM 다중라벨 10종 (제주향토음식/고기구이/해산물요리/회물회초밥/한식/"
                   "면요리/분식간편식/일식/중식/양식세계음식). CSV의 콤마구분 문자열을 리스트로 변환해 저장"
    )

    # --- 지역/카테고리 (필터링·상속 로직에서 사용) ---
    region_code = models.CharField(max_length=10)
    signgu_code = models.CharField(max_length=10, help_text="희망권역 필터에 사용")
    large_category_code = models.CharField(max_length=10)
    large_category_name = models.CharField(max_length=50)
    middle_category_code = models.CharField(max_length=10)
    middle_category_name = models.CharField(max_length=50)
    small_category_code = models.CharField(max_length=10)
    small_category_name = models.CharField(
        max_length=50,
        help_text="카테고리 중앙값 상속(1순위)의 기준 키. 음식점 food_role 매핑에도 사용"
    )

    # 권역 4분면
    QUADRANT_CHOICES = [("NE", "북동"), ("NW", "북서"), ("SE", "남동"), ("SW", "남서")]
    quadrant = models.CharField(
        max_length=2, choices=QUADRANT_CHOICES, db_index=True, default="SW"
    ) # 한라산 정상(33.3617,126.5292) 기준 위경도 비교로 계산


    # 목적 태그 함수
    score_nature = models.IntegerField(default=0)
    score_food = models.IntegerField(default=0)
    score_photo = models.IntegerField(default=0)
    score_culture = models.IntegerField(default=0)
    score_activity = models.IntegerField(default=0)
    score_shopping = models.IntegerField(default=0)

    # 체류시간, 품질
    stay_time_minutes = models.IntegerField(
        null=True,
        blank=True,
        help_text="대표 체류시간(15분격자). CSV의 stay_time_minutes"
    )
    stay_min = models.IntegerField(
        null=True,
        blank=True,
        help_text="밴드 하한"
    )
    stay_max = models.IntegerField(
        null=True,
        blank=True,
        help_text="밴드 상한"
    )
    stay_src = models.CharField(max_length=20, blank=True)

    qual = models.FloatField(null=True, blank=True)
    qual_rel = models.FloatField(null=True, blank=True)
    qual_category = models.FloatField(null=True, blank=True)
    satisfaction_score = models.FloatField(
        null=True, blank=True, help_text="★=AdjustedQual_k. Micro 수식에 바로 대입"
    )
    satisfaction_src = models.CharField(max_length=20, blank=True)

    class Meta:
        indexes = [
            models.Index(fields=["quadrant"]),
            models.Index(fields=["content_type_name", "small_category_name"]),
        ]

    def get_purpose_score(self, purpose_key: str) -> int:
        """scoring.py에서 사용자 목적('nature'/'food'/...)으로 바로 조회하는 헬퍼."""
        return {
            "nature": self.score_nature, "food": self.score_food,
            "photo": self.score_photo, "culture": self.score_culture,
            "activity": self.score_activity, "shopping": self.score_shopping,
        }.get(purpose_key, 0)

    def __str__(self):
        return f"{self.title} ({self.content_type_name})"

class Lodging(models.Model):
    """ 
    숙박 전용 테이블. 빔서치 후보X, 코스 완성 후 별도로 매칭
    """
    content_id = models.CharField(primary_key=True, max_length=20)
    title = models.CharField(max_length=200)
    address = models.CharField(max_length=300)
    longitude = models.FloatField()
    latitude = models.FloatField()
    overview = models.TextField(blank=True)
    contact = models.CharField(max_length=100, blank=True)

    # 호텔/리조트·콘도/펜션·민박/게스트하우스 — 숙박 유형 필터
    small_category_name = models.CharField(max_length=50, blank=True)
    signgu_code = models.CharField(max_length=10, blank=True)
    quadrant = models.CharField(max_length=2, blank=True, default="SW")

    check_in_time = models.CharField(max_length=20, blank=True)
    check_out_time = models.CharField(max_length=20, blank=True)
    capacity = models.CharField(max_length=50, blank=True,)
    room_count = models.CharField(max_length=20, blank=True)
    room_type = models.CharField(max_length=200, blank=True)
    room_capacity_summary = models.CharField(max_length=200, blank=True)
    room_options = models.CharField(max_length=300, blank=True)
    rooms_json = models.JSONField(default=list, blank=True) # 객실별 상세정보 원본(가격/시설 등)

    parking = models.CharField(max_length=20, blank=True)
    cooking = models.CharField(max_length=20, blank=True) # 취사 가능 여부
    facilities = models.TextField(blank=True) # 부대시설 선호 검색
    pickup = models.CharField(max_length=20, blank=True)
    food_place = models.CharField(max_length=200, blank=True)    
    refund_policy = models.TextField(blank=True)
    min_room_price = models.CharField(max_length=50, blank=True, help_text="51.9%만 유효, 실시간 아님. 참고가로만 노출")
    reservation_info = models.TextField(blank=True)
    reservation_url = models.URLField(blank=True)

    pet_allowed_type = models.CharField(max_length=100, blank=True)
    pet_allowed_animals = models.CharField(max_length=100, blank=True)
    pet_requirements = models.CharField(max_length=300, blank=True)
    pet_extra_info = models.TextField(blank=True)

    # !!!!!!확인 필요!!!!!!!!!!
    tripcom_hotel_id = models.CharField(max_length=50, blank=True)

    def __str__(self):
        return self.title


class PlaceEmbedding(models.Model):
    """
    자유입력(NLP) 매칭 + 음식점 QueryFit용 임베딩.
    실제 벡터 컬럼은 pgvector 확장 필요 — django-pgvector 등의 VectorField로
    나중에 교체 가능하도록 지금은 search_text만 우선 저장해도 무방하다.
    """

    place = models.OneToOneField(Place, primary_key=True, on_delete=models.CASCADE)
    search_text = models.TextField(
        help_text="임베딩에 사용된 원문 (title+overview+분류명(+메뉴/food_tags))"
    )
    # embedding = VectorField(dimensions=768)  # pgvector 세팅 후 주석 해제, KURE-v1은 1024차원(주의: 이전 안내 768과 다름)


class LodgingEmbedding(models.Model):
    """숙박 자유입력(QueryFit)용 임베딩."""

    lodging = models.OneToOneField(Lodging, primary_key=True, on_delete=models.CASCADE)
    search_text = models.TextField()
    # embedding = VectorField(dimensions=768)