import csv
from django.core.management.base import BaseCommand
from apps.places.models import Place


class Command(BaseCommand):
    help = "CSV의 breakfast_suitable 컬럼(Y/N/빈값)을 Place.breakfast_suitable(True/False/None)에 반영"

    def add_arguments(self, parser):
        parser.add_argument("csv_path", type=str)

    def handle(self, *args, **options):
        csv_path = options["csv_path"]
        updated, skipped, not_found = 0, 0, 0

        with open(csv_path, encoding="utf-8-sig") as f:
            reader = csv.DictReader(f)
            for row in reader:
                content_id = row.get("content_id")
                raw_value = (row.get("breakfast_suitable") or "").strip()

                if raw_value == "Y":
                    value = True
                elif raw_value == "N":
                    value = False
                else:
                    value = None

                try:
                    place = Place.objects.get(content_id=content_id)
                except Place.DoesNotExist:
                    not_found += 1
                    continue

                place.breakfast_suitable = value
                place.save(update_fields=["breakfast_suitable"])
                updated += 1

        self.stdout.write(self.style.SUCCESS(
            f"완료: 업데이트 {updated}건, 못찾음 {not_found}건"
        ))