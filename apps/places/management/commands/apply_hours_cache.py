"""
apply_hours_cache.py — hours_cache.json을 Place에 반영.

⚠️ 팀이 준 예시가 `"open": "09:00"` 한 줄뿐이라, 정확한 전체 구조를 몰라서
아래 3가지 흔한 형태를 전부 시도해보고 맞는 걸 자동으로 골라 처리하도록 짰습니다.
실제 파일 구조를 알려주시면 이 중 필요없는 분기는 지우고 정확하게 다듬을게요.

가능한 구조 A) 요일 무관 단일 시간대:
    {"<content_id>": {"open": "09:00", "close": "18:00", "closed_days": ["화"]}}

가능한 구조 B) 요일별 시간대:
    {"<content_id>": {"월": {"open":"09:00","close":"18:00"}, "화": null, ...}}

가능한 구조 C) status 필드 포함:
    {"<content_id>": {"status": "windows", "open": "09:00", "close": "18:00", ...}}

사용법:
    python manage.py apply_hours_cache data/hours_cache.json
"""

import json
from django.core.management.base import BaseCommand, CommandError
from apps.places.models import Place

ALL_WEEKDAYS = ["월", "화", "수", "목", "금", "토", "일"]


class Command(BaseCommand):
    help = "hours_cache.json을 Place.hours_status/open_windows/closed_weekdays에 반영한다."

    def add_arguments(self, parser):
        parser.add_argument("cache_path", type=str)
        parser.add_argument(
            "--dry-run", action="store_true",
            help="실제 저장 없이 처음 5건만 파싱 결과를 출력 (구조 확인용)"
        )

    def handle(self, *args, **options):
        cache = self._load_cache(options["cache_path"])
        dry_run = options["dry_run"]

        updated, not_found, checked = 0, 0, 0

        for content_id, raw_entry in cache.items():
            normalized = self._normalize_entry(raw_entry)

            if dry_run and checked < 5:
                self.stdout.write(f"{content_id}: {raw_entry} → {normalized}")
                checked += 1
                if checked == 5:
                    self.stdout.write(self.style.WARNING(
                        "--dry-run 모드: 위 5건 결과가 맞는지 확인 후 --dry-run 없이 재실행하세요."
                    ))
                    return

            if dry_run:
                continue

            try:
                place = Place.objects.get(content_id=content_id)
            except Place.DoesNotExist:
                not_found += 1
                continue

            place.hours_status = normalized["hours_status"]
            place.open_windows = normalized["open_windows"]
            place.closed_weekdays = normalized["closed_weekdays"]
            place.hours_uncertain = (normalized["hours_status"] == "uncertain")
            place.save(update_fields=["hours_status", "open_windows", "closed_weekdays", "hours_uncertain"])
            updated += 1

        if not dry_run:
            self.stdout.write(self.style.SUCCESS(f"반영 완료: {updated}건 / 매칭 실패: {not_found}건"))

    def _normalize_entry(self, raw: dict) -> dict:
        """
        실제 hours_cache.json 구조를 정규화하여 
        {"hours_status": ..., "open_windows": ..., "closed_weekdays": ...} 구조로 반환합니다.
        """
        if not raw:
            return {
                "hours_status": "uncertain",
                "open_windows": {},
                "closed_weekdays": []
            }

        # 1. 휴무 요일 추출 (closed_rule.days)
        closed_rule = raw.get("closed_rule", {})
        closed_weekdays = closed_rule.get("days", [])

        # 2. 영업 상태 추출 (hours_rule.type)
        hours_rule = raw.get("hours_rule", {})
        hours_status = hours_rule.get("type", "uncertain")

        # 3. 요일별 open/close 시간대 매핑 (hours_rule.windows)
        # DB의 open_windows 구조에 맞춰 { "월": [["08:00", "21:00"]], ... } 형태로 파싱
        open_windows = {}
        raw_windows = hours_rule.get("windows", [])

        if hours_status == "windows" and raw_windows:
            for window in raw_windows:
                open_time = window.get("open")
                close_time = window.get("close")
                days = window.get("days", [])

                if open_time and close_time:
                    for day in days:
                        # 휴무일로 지정된 요일은 제외
                        if day in closed_weekdays:
                            continue
                        
                        if day not in open_windows:
                            open_windows[day] = []
                        
                        open_windows[day].append([open_time, close_time])

        return {
            "hours_status": hours_status,
            "open_windows": open_windows,
            "closed_weekdays": closed_weekdays,
        }

    @staticmethod
    def _load_cache(path: str) -> dict:
        try:
            with open(path, encoding="utf-8") as f:
                return json.load(f)
        except FileNotFoundError:
            raise CommandError(f"파일을 찾을 수 없습니다: {path}")