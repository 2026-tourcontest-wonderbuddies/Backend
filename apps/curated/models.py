"""
추천 코스(큐레이션 코스) — "나만의 코스 만들기"(TripRequest→RecommendedCourse, 알고리즘이
그때그때 생성)와는 완전히 별개다. 여기 모델들은 미리 골라둔 장소 조합을 그냥 보여주는
용도라서, 이동시간·체류시간·숙박 등 알고리즘 쪽 로직을 전혀 갖지 않는다.
"""
from django.db import models
from apps.places.models import Place


class CuratedCourse(models.Model):
    REGION_CHOICES = [
        ("NE", "제주시 동부"), ("NW", "제주시 서부"),
        ("SE", "서귀포 동부"), ("SW", "서귀포 서부"), ("ALL", "제주 전역"),
    ]

    title = models.CharField(max_length=200)
    region = models.CharField(max_length=3, choices=REGION_CHOICES)
    region_label = models.CharField(max_length=100, help_text="화면 표시용, 예: '서귀포시 · 성산·표선'")
    duration = models.CharField(max_length=20, default="당일코스", help_text="필터용 코드, 예: '당일코스'")
    badge = models.CharField(max_length=50, help_text="카드 배지, 예: '당일코스 · 9시간'")
    time_of_day = models.JSONField(default=list, help_text="예: ['낮','노을']")
    meta_chips = models.JSONField(default=list, help_text="예: ['☀️ 낮','🌇 노을']")
    gradient = models.CharField(max_length=200, blank=True, help_text="카드 배경 그라데이션 CSS 값")
    order = models.IntegerField(default=0)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ["order", "id"]

    def __str__(self):
        return self.title


class CuratedCourseItem(models.Model):
    course = models.ForeignKey(CuratedCourse, related_name="items", on_delete=models.CASCADE)
    place = models.ForeignKey(Place, on_delete=models.PROTECT)
    order = models.IntegerField()

    class Meta:
        ordering = ["order"]

    def __str__(self):
        return f"{self.course_id}-{self.order}: {self.place.title}"
