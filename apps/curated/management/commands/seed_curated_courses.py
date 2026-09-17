"""
추천 코스 10개(4권역×2 + 전역×2) 시드 데이터.
AI Hub 실제 방문기록(같은 여행 안에서 실제로 짝지어 방문된 빈도) + 권역별 방문 순위를
근거로 구성했고, 모든 장소는 content_id로 DB 존재를 직접 확인했다.
재실행해도 안전하게(title 기준으로) 갱신되도록 짰다.
"""
from django.core.management.base import BaseCommand
from apps.curated.models import CuratedCourse, CuratedCourseItem

COURSES = [
    {
        "title": "성산일출봉과 우도, 제주 동쪽 일출 코스",
        "region": "NE", "region_label": "서귀포시 · 성산·우도",
        "time_of_day": ["새벽", "낮"], "meta_chips": ["🌌 새벽", "☀️ 낮", "5곳"],
        "gradient": "linear-gradient(135deg,var(--dawn),var(--midday))",
        "place_ids": [126435, 2564158, 127813, 126465, 598558],
    },
    {
        "title": "함덕·월정 해변과 비자림 숲길",
        "region": "NE", "region_label": "제주시 · 조천·구좌",
        "time_of_day": ["낮", "노을"], "meta_chips": ["☀️ 낮", "🌇 노을", "4곳"],
        "gradient": "linear-gradient(135deg,var(--midday),var(--sunset))",
        "place_ids": [126451, 2738701, 126472, 129400],
    },
    {
        "title": "동문재래시장에서 용두암 해안도로까지",
        "region": "NW", "region_label": "제주시 · 원도심",
        "time_of_day": ["낮", "노을"], "meta_chips": ["☀️ 낮", "🌇 노을", "3곳"],
        "gradient": "linear-gradient(135deg,var(--sunset),var(--morning))",
        "place_ids": [1013246, 2899545, 228853],
    },
    {
        "title": "한림·애월 해변 나들이",
        "region": "NW", "region_label": "제주시 · 한림·애월",
        "time_of_day": ["낮"], "meta_chips": ["☀️ 낮", "5곳"],
        "gradient": "linear-gradient(135deg,var(--morning),var(--midday))",
        "place_ids": [127490, 127870, 591866, 2785869, 2714241],
    },
    {
        "title": "서귀포 3대 폭포와 매일올레시장",
        "region": "SE", "region_label": "서귀포시 · 원도심",
        "time_of_day": ["낮"], "meta_chips": ["☀️ 낮", "3곳"],
        "gradient": "linear-gradient(135deg,var(--midday),var(--night))",
        "place_ids": [126438, 126437, 1013258],
    },
    {
        "title": "쇠소깍과 표선 해변",
        "region": "SE", "region_label": "서귀포시 · 남원·표선",
        "time_of_day": ["낮", "노을"], "meta_chips": ["☀️ 낮", "🌇 노을", "3곳"],
        "gradient": "linear-gradient(135deg,var(--dawn),var(--sunset))",
        "place_ids": [129617, 128794, 1013258],
    },
    {
        "title": "안덕 오름과 해안 드라이브",
        "region": "SW", "region_label": "서귀포시 · 안덕·대정",
        "time_of_day": ["낮", "노을"], "meta_chips": ["☀️ 낮", "🌇 노을", "4곳"],
        "gradient": "linear-gradient(135deg,var(--sunset),var(--night))",
        "place_ids": [2901520, 129699, 2715650, 228854],
    },
    {
        "title": "중문 관광단지 코스",
        "region": "SW", "region_label": "서귀포시 · 중문",
        "time_of_day": ["낮"], "meta_chips": ["☀️ 낮", "3곳"],
        "gradient": "linear-gradient(135deg,var(--night),var(--dawn))",
        "place_ids": [126439, 126449, 741109],
    },
    {
        "title": "제주 서부 해안 종주, 한림에서 안덕까지",
        "region": "ALL", "region_label": "제주 서부 · 한림~안덕",
        "time_of_day": ["낮", "노을"], "meta_chips": ["☀️ 낮", "🌇 노을", "2~3권역"],
        "gradient": "linear-gradient(135deg,var(--midday),var(--sunset))",
        "place_ids": [127490, 591866, 2901520, 129699],
    },
    {
        "title": "서귀포 남부 해안 종주, 시내에서 중문까지",
        "region": "ALL", "region_label": "서귀포 남부 해안",
        "time_of_day": ["낮"], "meta_chips": ["☀️ 낮", "2~3권역"],
        "gradient": "linear-gradient(135deg,var(--dawn),var(--night))",
        "place_ids": [126438, 126437, 129617, 126449, 126439],
    },
]


class Command(BaseCommand):
    help = "추천 코스 10개 시드 데이터를 넣는다 (재실행 시 title 기준으로 갱신)."

    def handle(self, *args, **options):
        for i, data in enumerate(COURSES):
            place_ids = data.pop("place_ids")
            n = len(place_ids)
            course, created = CuratedCourse.objects.update_or_create(
                title=data["title"],
                defaults={
                    **data,
                    "duration": "당일코스",
                    "badge": f"당일코스 · {n}곳",
                    "order": i,
                },
            )
            course.items.all().delete()
            for order, cid in enumerate(place_ids):
                CuratedCourseItem.objects.create(course=course, place_id=str(cid), order=order)
            status = "생성" if created else "갱신"
            self.stdout.write(f"[{status}] {course.title} ({n}곳)")

        self.stdout.write(self.style.SUCCESS(f"완료: 총 {len(COURSES)}개 코스"))
