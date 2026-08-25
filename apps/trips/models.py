from django.db import models
from apps.places.models import Place, Lodging

"""
사용자 입력과 생성된 코스 결과를 저장
"""

# Create your models here.
class TripRequest(models.Model):
    """1차 필수 입력(Hard) + 2차 선택 입력(Soft) + 음식/숙박 입력을 한 요청 단위로 저장."""

    # --- 1차 필수 입력 (Hard Constraints) ---
    start_datetime = models.DateTimeField(help_text="여행 시작 일시 (제주 도착 시각, 집 아님)")
    end_datetime = models.DateTimeField(help_text="여행 종료 일시")
    departure_place_id = models.CharField(
        max_length=20, blank=True,
        help_text="출발지 (공항 등). AVAIL_HOURS Case A 계산의 T_start 기준점"
    )
    return_to_departure = models.BooleanField(
        default=False, help_text="최종 도착지를 출발지로 복귀 설정했는지"
    )

    TRANSPORT_CHOICES = [
        ("rental_car", "렌터카"),
        ("own_car", "자가용"),
        ("taxi", "택시"),
        # 대중교통은 routing 엔진 MVP 범위 밖 (팀 라우팅 문서 §7 확인)
    ]
    transport_mode = models.CharField(max_length=20, choices=TRANSPORT_CHOICES)

    COMPANION_CHOICES = [
        ("alone", "혼자"), ("couple", "연인/배우자"), ("friend", "친구"),
        ("family_kids", "아이와 가족"), ("parents", "부모님"), ("group", "단체"),
    ]
    companion_type = models.CharField(max_length=20, choices=COMPANION_CHOICES)

    PURPOSE_CHOICES = [
        ("nature", "힐링/자연"), ("food", "식당/카페"), ("photo", "사진/감성"),
        ("culture", "문화/역사"), ("activity", "체험/액티비티"), ("shopping", "쇼핑/시장"),
    ]
    purpose_main = models.CharField(max_length=20, choices=PURPOSE_CHOICES)
    purpose_sub = models.CharField(max_length=20, choices=PURPOSE_CHOICES, blank=True)

    PRIORITY_CHOICES = [
        ("dist", "이동 최소 코스"), ("pref", "취향 중심 코스"), ("relax", "여유로운 코스"),
    ]
    course_priority = models.CharField(max_length=20, choices=PRIORITY_CHOICES)

    region_preference = models.CharField(max_length=50, blank=True, help_text="희망권역")

    # --- 2차 선택 입력 (Soft Constraints) ---
    mood_tags = models.JSONField(default=list, blank=True, help_text="조용한/활기찬/야경 등 최대 3개")
    include_places = models.JSONField(default=list, blank=True, help_text="가고 싶은 장소 content_id 목록")
    exclude_places = models.JSONField(default=list, blank=True)
    exclude_categories = models.JSONField(default=list, blank=True, help_text="예: ['박물관','액티비티']")
    walk_light = models.BooleanField(default=False, help_text="많이 걷지 않는 코스 선호")
    indoor_outdoor_pref = models.CharField(
        max_length=20, blank=True,
        help_text="상관없음/실내중심/야외중심/적절히섞기"
    )

    # --- 3차 자유 텍스트 ---
    free_text_input = models.TextField(blank=True, help_text="SBERT 임베딩으로 변환해 Pref 계산에 반영")

    # --- 음식 관련 입력 (음식점 로직 §입력값) ---
    food_pref_1 = models.CharField(max_length=30, blank=True)
    food_pref_2 = models.CharField(max_length=30, blank=True)
    food_restriction = models.CharField(
        max_length=30, blank=True,
        help_text="없음/비건/육류제외/해산물제외/알레르기·기타"
    )
    food_cafe_balance = models.CharField(
        max_length=20, blank=True,
        help_text="음식점중심/카페중심/둘다 — purpose_main 또는 sub가 'food'일 때만 유효"
    )

    # --- 숙박 관련 입력 (숙박 로직 §입력값, 다일 여행에서만 필요) ---
    lodging_capacity = models.IntegerField(null=True, blank=True)
    lodging_type = models.CharField(
        max_length=30, blank=True,
        help_text="호텔/리조트·콘도/펜션·민박/게스트하우스/상관없음"
    )
    lodging_conditions = models.JSONField(
        default=list, blank=True, help_text="예: ['주차가능','취사가능']"
    )
    lodging_budget = models.IntegerField(null=True, blank=True, help_text="1박 예산, 참고용")
    lodging_free_text = models.TextField(blank=True)

    created_at = models.DateTimeField(auto_now_add=True)

    def __str__(self):
        return f"Trip#{self.pk} {self.start_datetime}~{self.end_datetime}"


class ItineraryDay(models.Model):
    """하루 단위 일정. Pipeline 3(AVAIL_HOURS, 플래그, TargetSlots) 계산 결과를 저장."""

    trip = models.ForeignKey(TripRequest, related_name="days", on_delete=models.CASCADE)
    day_index = models.IntegerField(help_text="1부터 시작하는 일차")

    DAY_CASE_CHOICES = [
        ("A", "입도일"), ("B", "중간일차"), ("C", "출도일"), ("D", "당일치기"),
    ]
    day_case = models.CharField(max_length=1, choices=DAY_CASE_CHOICES)

    avail_hours = models.FloatField(help_text="constraints.py가 계산한 가용시간")
    target_slots = models.IntegerField(help_text="TargetSlots(d) 계산 결과")

    need_lunch = models.BooleanField(default=False)
    need_dinner = models.BooleanField(default=False)
    need_night_spot = models.BooleanField(default=False)

    # 다일 여행 시 일자별 설정 오버라이드 (없으면 TripRequest의 전체값 사용)
    override_purpose_main = models.CharField(max_length=20, blank=True)
    override_purpose_sub = models.CharField(max_length=20, blank=True)
    override_priority = models.CharField(max_length=20, blank=True)
    override_region = models.CharField(max_length=50, blank=True)

    lodging = models.ForeignKey(
        Lodging, null=True, blank=True, on_delete=models.SET_NULL,
        help_text="이 날 숙박할 곳 — 코스 완성 후 lodging_matcher가 채움"
    )

    class Meta:
        ordering = ["day_index"]
        unique_together = ("trip", "day_index")

    def __str__(self):
        return f"{self.trip_id} Day{self.day_index} ({self.day_case})"


class ItineraryItem(models.Model):
    """하루 안의 개별 방문 슬롯 (관광지/문화시설/쇼핑/음식점 공용)."""

    day = models.ForeignKey(ItineraryDay, related_name="items", on_delete=models.CASCADE)
    order = models.IntegerField(help_text="방문 순서 (0부터)")
    place = models.ForeignKey(Place, on_delete=models.PROTECT)

    SLOT_TYPE_CHOICES = [
        ("GENERAL", "일반"), ("RESTAURANT", "식당"),
        ("CAFE", "카페"), ("SNACK", "간식"),
    ]
    slot_type = models.CharField(max_length=20, choices=SLOT_TYPE_CHOICES, default="GENERAL")

    arrive_at = models.DateTimeField()
    depart_at = models.DateTimeField()
    travel_min_from_prev = models.IntegerField(
        null=True, blank=True,
        help_text="이전 장소로부터의 이동시간 (5분 격자 스냅된 값, routing 엔진 duration_adjusted)"
    )
    locked = models.BooleanField(default=False, help_text="사용자가 '고정'한 장소인지")

    # Micro 평가 시 계산된 점수를 감사(audit) 목적으로 남겨둠 — 디버깅에 유용
    pref_score = models.FloatField(null=True, blank=True)
    adjusted_qual = models.FloatField(null=True, blank=True)
    cost_move = models.FloatField(null=True, blank=True)

    class Meta:
        ordering = ["day", "order"]

    def __str__(self):
        return f"{self.day_id}-{self.order}: {self.place.title}"


class ModificationLog(models.Model):
    """Pipeline 6 챗봇 수정 이력. LLM이 구조화한 delta를 저장해 재실행에 사용."""

    trip = models.ForeignKey(TripRequest, related_name="modification_logs", on_delete=models.CASCADE)
    raw_message = models.TextField(help_text="사용자가 입력한 원문 (예: '카페 하나 더')")
    parsed_delta = models.JSONField(
        help_text="LLM이 구조화한 결과. 예: {'lock':['성산일출봉'], 'walk_light': true}"
    )
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ["created_at"]