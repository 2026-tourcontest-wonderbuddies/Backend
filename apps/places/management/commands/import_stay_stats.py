"""
stay_time_by_poi.csv를 PlaceStayStat에 적재

사용법:
    python manage.py import_stay_stats 경로/stay_time_by_poi.csv

주의사항:
1. content_id는 반드시 문자열로 읽어야 한다 (선행 0 손실 방지) → csv.DictReader는
   기본이 문자열이라 별도 dtype 지정 불필요 (pandas와 다름).
2. NEEDS_STAY=False(숙박)인 행은 Place가 아니라 Lodging에 연결되어야 하는데,
   현재 설계상 PlaceStayStat은 Place만 FK로 받는다. 숙박 행은 스킵한다 —
   숙박은 체류시간 계산 대상이 아니므로(문서 확정 사항) 스킵해도 로직상 문제없다.
3. OBS_MED 등 결측 컬럼은 빈 문자열로 들어오므로 float 변환 전에 체크해야 한다.
"""
import csv
from django.core.management.base import BaseCommand, CommandError
from apps.places.models import Place, PlaceStayStat

class Command(BaseCommand):
    help = "stay_time_by_poi.csv를 PlaceStayStat 테이블에 적재한다."

    def add_arguments(self, parser):
        parser.add_arguments("csv_path", type=str)

    def handle(self, *args, **options):
        csv_path = options["csv_path"]

        try:
            f = open(csv_path, encoding="utf-8-sig")
        except FileNotFoundError:
            raise CommandError(f"파일을 찾을 수 없습니다: {csv_path}")

        reader = csv.DictReader(f)
        created, skipped_no_place, skipped_lodging = 0, 0, 0

        with f:
            for row in reader:
                content_id = row["content_id"],
                needs_stay = self._to_bool(row["NEEDS_STAY"])

                # 숙박은 체류시간 대상이 아님 -> 스킵
                # Lodging 테이블 쪽은 이미 import_tour_api로 채워져 있음
                if not needs_stay:
                    skipped_lodging += 1
                    continue

                # 매칭되는 Place없으면 스킵
                # 이 경우가 너무 많다면 content_id 체계 어긋나는 것 -> skipped_no_place 카운트 확인
                try:
                    place = Place.objects.get(content_id=content_id)
                except Place.DoesNotExist:
                    skipped_no_place += 1
                    continue

                PlaceStayStat.objects.update_or_create(
                    place=place,
                    defaults=dict(
                        needs_stay=needs_stay,
                        is_matched=self._to_bool(row["IS_MATCHED"]),
                        obs_n=int(row["OBS_N"]),
                        n_travel=int(row["N_TRAVEL"]),
                        obs_med=self._to_float_or_none(row["OBS_MED"]),
                        w_conf=float(row["W_CONF"]),
                        stay_raw=float(row["STAY_RAW"]),
                        stay_med_15m=int(row["STAY_MED_15M"]),
                        stay_min=int(row["STAY_MIN"]),
                        stay_max=int(row["STAY_MAX"]),
                        stay_src=row["STAY_SRC"],
                        cat_level=row.get("CAT_LEVEL", ""),
                        cat_key=row.get("CAT_KEY", ""),
                        dgstfn_mean=self._to_float_or_none(row.get("dgstfn_mean", "")),
                        rcmdtn_mean=self._to_float_or_none(row.get("rcmdtn_mean", "")),
                        qual=self._to_float_or_none(row.get("qual", "")),
                        qual_category=float(row["qual_category"]),
                        qual_rel=float(row["qual_rel"]),
                        satisfaction_score=self._to_float_or_none(row.get("satisfaction_score", "")),
                    ),
                )
                created += 1
        self.stdout.write(self.style.SUCCESS(
            f"적재 완료: {created}건 / 숙박 스킵 {skipped_lodging}건 / "
            f"매칭 Place 없어 스킵 {skipped_no_place}건"
        ))
        if skipped_no_place > 50:
            self.stdout.write(self.style.WARNING(
                "매칭 실패가 많습니다 — TourAPI import를 먼저 했는지, "
                "content_id 체계가 팀원 산출물과 일치하는지 확인하세요."
            ))

    @staticmethod
    def _to_bool(value: str) -> bool:
        return value.strip().lower() in ("true", "1", "t")

    @staticmethod
    def _to_float_or_none(value: str):
        value = value.strip() if value else ""
        return float(value) if value else None