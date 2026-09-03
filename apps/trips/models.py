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
    user = models.ForeignKey(
        settings.AUTH_USER_MODEL, on_delete=models.CASCADE, related_name="trips",
        null=True, blank=True,  # 로그인 붙기 전까지는 null 허용, 나중에 필수로 전환
    )

    start_datetime = models.DateTimeField()
    end_datetime = models.DateTimeField()

    # ★ 변경: 출발지는 반드시 Place 테이블에 있는 content_id를 참조해야
    # 이동시간 매트릭스 조회가 가능함 (공항 등도 TourAPI POI로 등록돼 있어야 함)
    departure_place_id = models.CharField(max_length=20, blank=True)
    return_to_departure = models.BooleanField(default=False)

    TRANSPORT_CHOICES = [("rental_car", "렌터카"), ("own_car", "자가용"), ("taxi", "택시")]
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

    # ★ 삭제: course_priority — RecommendedCourse.mode로 대체됨

    region_preference = models.CharField(max_length=2, blank=True, help_text="NE/NW/SE/SW")

    mood_tags = models.JSONField(default=list, blank=True)
    include_places = models.JSONField(default=list, blank=True)
    exclude_places = models.JSONField(default=list, blank=True)
    exclude_categories = models.JSONField(default=list, blank=True)
    walk_light = models.BooleanField(default=False)
    indoor_outdoor_pref = models.CharField(max_length=20, blank=True)
    free_text_input = models.TextField(blank=True)

    food_pref_1 = models.CharField(max_length=30, blank=True)
    food_pref_2 = models.CharField(max_length=30, blank=True)
    food_restriction = models.CharField(max_length=30, blank=True)
    food_cafe_balance = models.CharField(max_length=20, blank=True)

    lodging_capacity = models.IntegerField(null=True, blank=True)
    lodging_type = models.CharField(max_length=30, blank=True)
    lodging_conditions = models.JSONField(default=list, blank=True)
    lodging_budget = models.IntegerField(null=True, blank=True)
    lodging_free_text = models.TextField(blank=True)

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