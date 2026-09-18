from django.core.management.base import BaseCommand
from apps.places.models import Place
import numpy as np
from pathlib import Path


class Command(BaseCommand):
    help = "DB에 저장된 Place.embedding_vector를 npz 파일로 내보내기 (빠른 조회용)"

    def handle(self, *args, **options):
        places = list(Place.objects.exclude(embedding_vector__isnull=True).values_list("content_id", "embedding_vector"))
        content_ids = np.array([cid for cid, _ in places])
        vectors = np.array([vec for _, vec in places], dtype=np.float32)

        out_dir = Path("apps/recommendation/data")
        out_dir.mkdir(parents=True, exist_ok=True)
        np.savez_compressed(out_dir / "place_vectors.npz", content_ids=content_ids, vectors=vectors)

        self.stdout.write(self.style.SUCCESS(f"{len(content_ids)}건 저장 완료: {out_dir / 'place_vectors.npz'}"))