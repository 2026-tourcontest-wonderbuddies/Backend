"""
★ 변경사항 ★
1. User FK 추가 (소셜 로그인 대비) — django.contrib.auth의 기본 User를 우선 사용.
   구글 로그인은 django-allauth로 붙이면 이 FK를 그대로 씀 (아래 안내 참고).
2. TripRequest.course_priority 필드 삭제 — 이제 사용자가 미리 고르지 않고
   3개 다 만들어서 보여준 뒤 고르게 하므로, "요청 시점의 선택"이 아니라
   "결과 중 선택"이 됨. 그래서 이 필드는 RecommendedCourse.mode로 옮겨감.
3. RecommendedCourse 모델 신규 — 코스 3개를 각각 담는 그릇.
   is_selected 필드로 "사용자가 최종 선택한 코스"를 표시(코스 저장 요구사항).
4. ItineraryDay.trip → ItineraryDay.course로 FK 변경 (TripRequest 대신 RecommendedCourse에 속함).
5. ItineraryDay에 lodging_options 추가 — 숙박 후보 top3을 다 저장해서
   나중에 사용자가 다른 숙소로 바꾸고 싶을 때 참고할 수 있게 함.
"""

from django.conf import settings
from django.db import models
from apps.places.models import Place, Lodging


class TripRequest(models.Model):
    user = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.CASCADE,
                              related_name="trips", null=True, blank=True)

    start_datetime = models.DateTimeField(help_text="입국수속 마친 후 실제 활동 시작 시각 (제주공항 기준)")
    end_datetime = models.DateTimeField(help_text="복귀편 출발 위해 공항 도착 필요한 시각")

    # ★ 삭제: departure_place_id, return_to_departure — 공항 암묵 고정이라 불필요

    TRANSPORT_CHOICES = [("rental_car", "렌터카"), ("own_car", "자가용"), ("taxi", "택시")]
    transport_mode = models.CharField(max_length=20, choices=TRANSPORT_CHOICES)

    # ★ 신규: companion_type 삭제하고 이걸로 통일
    guests = models.IntegerField(default=2, help_text="1~20명, 기본 2명 (AI Hub 실측 중앙값)")

    PURPOSE_CHOICES = [
        ("nature", "힐링/자연"), ("food", "음식/카페"), ("photo", "사진/감성"),
        ("culture", "문화/역사"), ("activity", "체험/액티비티"), ("shopping", "쇼핑/시장"),
    ]
    purpose_main = models.CharField(max_length=20, choices=PURPOSE_CHOICES)
    purpose_sub = models.CharField(max_length=20, choices=PURPOSE_CHOICES, blank=True)

    # ★ 변경: "ALL"(제주 전역 무관) 추가돼 5종
    REGION_CHOICES = [("NE", "북동"), ("NW", "북서"), ("SE", "남동"), ("SW", "남서"), ("ALL", "제주 전역 무관")]
    region_preference = models.CharField(max_length=3, choices=REGION_CHOICES, default="ALL")

    exclude_categories = models.JSONField(
        default=list, blank=True,
        help_text="TourAPI 중분류 코드 목록 (기획서 2.5 계층형 체크박스, 5개 대분류 하위 중분류)"
    )
    walk_light = models.BooleanField(default=False)
    indoor_outdoor_pref = models.CharField(max_length=20, blank=True)
    free_text_input = models.TextField(blank=True)

    food_pref_1 = models.CharField(max_length=30, blank=True)
    food_pref_2 = models.CharField(max_length=30, blank=True)
    food_cafe_balance = models.CharField(max_length=20, blank=True)
    # ★ 삭제: food_restriction — 최종 기획서에 명시 없음, 자유입력으로 흡수 추정 (팀 확인 필요)

    # ★ 변경: lodging_capacity 삭제(guests로 통일), lodging_conditions(리스트) → 단일 bool
    lodging_type = models.CharField(max_length=30, blank=True, default="상관없음")
    lodging_need_cooking = models.BooleanField(default=False)
    lodging_free_text = models.TextField(blank=True)
    # ★ 삭제: lodging_budget — 필터에 안 쓰고 문구만이라 accommodations 쪽에서 처리(price_hint)

    created_at = models.DateTimeField(auto_now_add=True)

    def __str__(self):
        return f"Trip#{self.pk} {self.start_datetime}~{self.end_datetime}"


class RecommendedCourse(models.Model):
    """
    ★ 신규 ★ — 코스 3개(dist/pref/relax) 중 하나를 담는 컨테이너.
    한 TripRequest당 이 레코드가 항상 3개 생긴다 (요청 시점에 3개 다 생성).
    """
    trip = models.ForeignKey(TripRequest, related_name="courses", on_delete=models.CASCADE)

    MODE_CHOICES = [("dist", "이동 최소 코스"), ("pref", "취향 중심 코스"), ("relax", "여유로운 코스")]
    mode = models.CharField(max_length=20, choices=MODE_CHOICES)

    is_selected = models.BooleanField(
        default=False, help_text="사용자가 3개 중 최종 선택한 코스인지 (선택 UI 액션으로 갱신)"
    )
    final_score = models.FloatField(null=True, blank=True, help_text="course_builder의 Macro 최종점수, 감사용")
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        unique_together = ("trip", "mode")

    def __str__(self):
        return f"{self.trip_id} - {self.mode}"


class ItineraryDay(models.Model):
    course = models.ForeignKey(RecommendedCourse, related_name="days", on_delete=models.CASCADE)  # ★ 변경: trip→course
    day_index = models.IntegerField()

    DAY_CASE_CHOICES = [("A", "입도일"), ("B", "중간일차"), ("C", "출도일"), ("D", "당일치기")]
    day_case = models.CharField(max_length=1, choices=DAY_CASE_CHOICES)

    avail_hours = models.FloatField()
    target_slots = models.IntegerField()
    need_lunch = models.BooleanField(default=False)
    need_dinner = models.BooleanField(default=False)
    need_night_spot = models.BooleanField(default=False)

    lodging = models.ForeignKey(
        Lodging, null=True, blank=True, on_delete=models.SET_NULL,
        related_name="+", help_text="자동 선택된(top1) 숙소. 다음날 출발지로 사용됨"
    )
    lodging_options = models.JSONField(
        default=list, blank=True,
        help_text="숙박 후보 top3의 content_id 목록. 사용자가 다른 숙소로 바꿀 때 이 중에서 고름"
    )

    lodging_snapshot = models.JSONField(
        null=True, blank=True,
        help_text="선택된 숙소의 LodgingCard.to_dict() 스냅샷 (별도 테이블 없이 통째 저장)"
    )
    lodging_options_snapshot = models.JSONField(
        default=list, blank=True, help_text="추천된 top3 LodgingCard.to_dict() 리스트"
    )

    class Meta:
        ordering = ["day_index"]
        unique_together = ("course", "day_index")

    def __str__(self):
        return f"{self.course_id} Day{self.day_index} ({self.day_case})"


class ItineraryItem(models.Model):
    day = models.ForeignKey(ItineraryDay, related_name="items", on_delete=models.CASCADE)
    order = models.IntegerField()
    place = models.ForeignKey(Place, on_delete=models.PROTECT)

    SLOT_TYPE_CHOICES = [("GENERAL", "일반"), ("RESTAURANT", "식당"), ("CAFE", "카페"), ("SNACK", "간식")]
    slot_type = models.CharField(max_length=20, choices=SLOT_TYPE_CHOICES, default="GENERAL")

    arrive_at = models.DateTimeField()
    depart_at = models.DateTimeField()
    travel_min_from_prev = models.IntegerField(null=True, blank=True)
    locked = models.BooleanField(default=False)
    hours_uncertain = models.BooleanField(default=False, help_text="영업시간 확인 불가 장소인지 (⚠표시용)")

    pref_score = models.FloatField(null=True, blank=True)
    adjusted_qual = models.FloatField(null=True, blank=True)
    cost_move = models.FloatField(null=True, blank=True)

    class Meta:
        ordering = ["day", "order"]

    def __str__(self):
        return f"{self.day_id}-{self.order}: {self.place.title}"


class ModificationLog(models.Model):
    """★ 변경: trip이 아니라 course에 귀속 — 코스별로 따로 수정하니까."""
    course = models.ForeignKey(RecommendedCourse, related_name="modification_logs", on_delete=models.CASCADE)
    raw_message = models.TextField()
    parsed_delta = models.JSONField()
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ["created_at"]