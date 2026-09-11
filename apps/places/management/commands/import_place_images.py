import re
from pathlib import Path
from django.core.management.base import BaseCommand
from decouple import config
import cloudinary
import cloudinary.uploader

from apps.places.models import Place, PlaceImage, LodgingImage

cloudinary.config(
    cloud_name=config("CLOUDINARY_CLOUD_NAME"),
    api_key=config("CLOUDINARY_API_KEY"),
    api_secret=config("CLOUDINARY_API_SECRET"),
)


class Command(BaseCommand):
    help = "이미지 폴더(하위폴더 포함)를 재귀 탐색해 Cloudinary 업로드 후 DB에 등록"

    def add_arguments(self, parser):
        parser.add_argument("image_dir", type=str)

    def handle(self, *args, **options):
        image_dir = Path(options["image_dir"])
        pattern = re.compile(r"^(\d+)_")

        image_files = (
            list(image_dir.rglob("*.jpg"))
            + list(image_dir.rglob("*.jpeg"))
            + list(image_dir.rglob("*.png"))
        )
        self.stdout.write(f"총 {len(image_files)}개 파일 발견")

        success, skipped, fail = 0, 0, 0

        for file_path in image_files:
            match = pattern.match(file_path.name)
            if not match:
                continue
            content_id = match.group(1)

            # ★ 먼저 관광지(Place)에서 찾아보고
            place = Place.objects.filter(content_id=content_id).first()

            try:
                result = cloudinary.uploader.upload(str(file_path), folder="jeju_places", public_id=file_path.stem)
            except Exception as e:
                self.stdout.write(self.style.WARNING(f"업로드 실패({file_path.name}): {e}"))
                continue

            if place:
                # 관광지 이미지
                order = PlaceImage.objects.filter(place=place).count()
                PlaceImage.objects.create(place=place, image_url=result["secure_url"], order=order)
            else:
                # ★ Place에 없으면 숙박일 가능성 → LodgingImage로 저장
                order = LodgingImage.objects.filter(lodging_content_id=content_id).count()
                LodgingImage.objects.create(lodging_content_id=content_id, image_url=result["secure_url"], order=order)