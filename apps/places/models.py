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
    content_type_name = models.CharField(
        max_length=20, db_index=True,
        help_text="관광지 / 문화시설 / 쇼핑 / 음식점"
    )
    title = models.CharField(max_length=200)
    address = models.CharField(max_length=300)
    zipcode = models.CharField(max_length=10, blank=True)
    longitude = models.FloatField()
    latitude = models.FloatField()
    overview = models.TextField(
        blank=True,
        help_text="장소 소개글. LLM 목적태깅(DescriptionFit)과 자유입력 임베딩 매칭에 씀"
    )
    homepage = models.URLField(blank=True)
    contact = models.CharField(max_length=100, blank=True)

    # 운영시간 (원본 텍스트 + 파싱 결과 분리 저장)
    hours_raw = models.CharField(
        max_length=100, blank=True, help_text="원문 그대로(예: '11:20~30 (마지막 주문 19:50)). 파싱 실패 시 대조용 남겨둠"
    )
    open_time = models.TimeField(
        null=True, blank=True, help_text="hours_raw를 파싱해서 채우는 필드. import 스크립트가 채움"
    )
    close_time = models.TimeField(null=True, blank=True)
    closed_days_raw = models.CharField(max_length=200, blank=True)
    closed_weekdays = models.JSONField(
        default=list, blank=True,
        help_text="파싱된 휴무 요일 배열 (예: ['화']). 하드필터에서 이 필드로 비교"
    )

    #관광지/문화시설 공통
    fees = models.CharField(max_length=500, blank=True)
    parking = models.CharField(
        max_length=20, blank=True,
        help_text="'가능'/'불가능'/'Unknown' — 렌터카 이용 시 하드필터에 사용"
    )
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
        help_text=(
            "음식점 로직 문서 기준 소분류→역할 매핑 결과. "
            "RESTAURANT / CAFE / SNACK / BAR 중 하나. import 시 small_category_name으로 계산."
        )
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

    class Meta:
        indexes = [
            models.Index(fields=["signgu_code"]),
            models.Index(fields=["content_type_name", "small_category_name"]),
        ]

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

    # 100% 유효 — 필수 필터 가능
    small_category_name = models.CharField(
        max_length=50, blank=True,
        help_text="호텔/리조트·콘도/펜션·민박/게스트하우스 — 숙박 유형 필터"
    )
    signgu_code = models.CharField(max_length=10, blank=True)

    # 74~98% 유효 — 조건부 필터/참고
    room_capacity_summary = models.CharField(
        max_length=200, blank=True,
        help_text="정확한 숙박 인원 정보. 74.1%만 유효 — 없으면 Unknown 처리 (2.2절 규칙)"
    )
    capacity = models.CharField(
        max_length=50, blank=True,
        help_text="전체 수용 인원. 11.8%만 유효 — 사실상 참고용"
    )
    room_type = models.CharField(max_length=200, blank=True)
    room_count = models.CharField(max_length=20, blank=True)
    room_options = models.CharField(max_length=300, blank=True)
    parking = models.CharField(max_length=20, blank=True, help_text="94.8% 유효 — 자동차 이용 시 필터")
    cooking = models.CharField(max_length=20, blank=True, help_text="68.4% 유효 — 취사가능 조건 필터")
    facilities = models.TextField(blank=True, help_text="98.6% 유효 — 부대시설 선호 검색")
    check_in_time = models.CharField(max_length=20, blank=True)
    check_out_time = models.CharField(max_length=20, blank=True)

    # 결측 심함 — 참고 정보로만 사용, 필터에 쓰지 않음
    min_room_price = models.CharField(max_length=50, blank=True, help_text="51.9%만 유효, 실시간 아님. 참고가로만 노출")
    pickup = models.CharField(max_length=20, blank=True)
    food_place = models.CharField(max_length=200, blank=True)
    pet_allowed_type = models.CharField(max_length=100, blank=True)
    pet_allowed_animals = models.CharField(max_length=100, blank=True)
    pet_requirements = models.CharField(max_length=300, blank=True)
    reservation_info = models.TextField(blank=True)
    reservation_url = models.URLField(blank=True)
    refund_policy = models.TextField(blank=True)

    class Meta:
        indexes = [models.Index(fields=["signgu_code"])]

    def __str__(self):
        return self.title

class PlaceStayStat(models.Model):
    """
    pipeline 1.2 최종 산출물 - stay_time_by_poi.csv를 그대로 적재하는 테이블
    직접 계산하지 X
    """

    place = models.OneToOneField(
        Place, primary_key=True, on_delete=models.CASCADE, related_name="stay_stat"
    )

    # 상태 플래그
    needs_stay = models.BooleanField(help_text="False면 숙박(체류시간 계산 대상 아님)")
    is_matched = models.BooleanField(help_text="AI Hub 관측이 붙었는지")

    # 관측 통계 (미매칭이면 전부 null)
    obs_n = models.IntegerField(help_text="OBS_N — 관측 건수")
    n_travel = models.IntegerField(default=0)
    obs_med = models.FloatField(null=True, blank=True)
    w_conf = models.FloatField(help_text="W_CONF = min(OBS_N/3, 1.0)")

    # 최종 체류시간 — 알고리즘이 실제로 쓰는 값
    stay_raw = models.FloatField(help_text="격자 올리기 전 원값 (감사용)")
    stay_med_15m = models.IntegerField(
        help_text="대표값. ItineraryItem 배치 시 StayTime_k로 바로 사용"
    )
    stay_min = models.IntegerField(help_text="밴드 하한 (대표값 아님! 이름 주의)")
    stay_max = models.IntegerField(help_text="밴드 상한")
    stay_src = models.CharField(
        max_length=20,
        help_text="observed / blend / category / manual_override / lodging_anchor"
    )

    # 카테고리 상속 근거
    cat_level = models.CharField(max_length=10, blank=True)
    cat_key = models.CharField(max_length=100, blank=True)

    # 만족도 블록 — AdjustedQual_k 계산에 직접 쓰는 값들
    dgstfn_mean = models.FloatField(null=True, blank=True)
    rcmdtn_mean = models.FloatField(null=True, blank=True)
    qual = models.FloatField(null=True, blank=True, help_text="Qual_k (0~1 정규화)")
    qual_category = models.FloatField(help_text="Qual_category(k) — 카테고리 상속값")
    qual_rel = models.FloatField(help_text="Rel_k — 관측 신뢰도")
    satisfaction_score = models.FloatField(
        null=True, blank=True,
        help_text="★ = AdjustedQual_k. scoring.py의 Micro 수식에 바로 대입"
    )

    def __str__(self):
        return f"{self.place_id} stay={self.stay_med_15m}min src={self.stay_src}"

class PlaceTagScore(models.Model):
    """
    Pipeline 1.3 — LLM이 사전 태깅한 6개 목적 태그 점수 (0~100, 10점 단위).
    아직 팀 산출물이 도착 안 했다면 본인이 배치를 돌려야 할 수도 있는 테이블.
    Pref_k 계산(취향 매칭)의 CategoryFit/DescriptionFit 혼합 결과가 여기 저장됨.
    """

    place = models.OneToOneField(Place, primary_key=True, on_delete=models.CASCADE)
    score_nature = models.IntegerField(help_text="힐링/자연")
    score_food = models.IntegerField(help_text="식당/카페")
    score_photo = models.IntegerField(help_text="사진/감성")
    score_culture = models.IntegerField(help_text="문화/역사")
    score_activity = models.IntegerField(help_text="체험/액티비티")
    score_shopping = models.IntegerField(help_text="쇼핑/시장")

    def get_score(self, purpose_key: str) -> int:
        """
        purpose_key(예: 'nature', 'food')를 받아 해당 점수를 반환하는 헬퍼.
        scoring.py에서 사용자 목적 문자열과 매핑할 때 씀.
        """
        mapping = {
            "nature": self.score_nature,
            "food": self.score_food,
            "photo": self.score_photo,
            "culture": self.score_culture,
            "activity": self.score_activity,
            "shopping": self.score_shopping,
        }
        return mapping.get(purpose_key, 0)


class PlaceEmbedding(models.Model):
    """
    자유입력(NLP) 매칭 + 음식점 QueryFit용 임베딩.
    실제 벡터 컬럼은 pgvector 확장 필요 — django-pgvector 등의 VectorField로
    나중에 교체 가능하도록 지금은 search_text만 우선 저장해도 무방하다.
    """

    place = models.OneToOneField(Place, primary_key=True, on_delete=models.CASCADE)
    search_text = models.TextField(
        help_text="title + small_category_name + featured_menu + menu + overview 조합 원문"
    )
    # embedding = VectorField(dimensions=768)  # pgvector 세팅 후 주석 해제


class LodgingEmbedding(models.Model):
    """숙박 자유입력(QueryFit)용 임베딩."""

    lodging = models.OneToOneField(Lodging, primary_key=True, on_delete=models.CASCADE)
    search_text = models.TextField(
        help_text="title+address+overview+room_type+parking+cooking+food_place+facilities 등 조합"
    )
    # embedding = VectorField(dimensions=768)