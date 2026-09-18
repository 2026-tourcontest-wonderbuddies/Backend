from django.core.management.base import BaseCommand
from apps.places.models import Place
from apps.recommendation.openai_embedding import embed_texts


class Command(BaseCommand):
    help = "전체 Place의 search_text를 OpenAI로 임베딩해서 embedding_vector에 저장"

    def handle(self, *args, **options):
        targets = Place.objects.filter(embedding_vector__isnull=True)
        total = targets.count()
        self.stdout.write(f"대상 {total}건 임베딩 시작...")

        batch_size = 100  # OpenAI는 한 번에 여러 개 보내는 게 효율적
        places = list(targets)

        for i in range(0, len(places), batch_size):
            batch = places[i:i + batch_size]
            texts = [
                f"{p.title} {p.small_category_name or ''} {p.overview or ''}"[:2000]
                for p in batch
            ]
            try:
                vectors = embed_texts(texts)
            except Exception as e:
                self.stdout.write(self.style.WARNING(f"배치 실패({i}): {e}"))
                continue

            for place, vec in zip(batch, vectors):
                place.embedding_vector = vec.tolist()
                place.save(update_fields=["embedding_vector"])

            self.stdout.write(f"{min(i+batch_size, len(places))}/{total} 완료")

        self.stdout.write(self.style.SUCCESS("전체 임베딩 완료"))