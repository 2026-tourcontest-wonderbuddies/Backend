from django.core.management.base import BaseCommand
from apps.places.models import Place
from openai import OpenAI
from decouple import config

_client = None

def _get_client():
    global _client
    if _client is None:
        _client = OpenAI(api_key=config("OPENAI_API_KEY"))
    return _client


SYSTEM_PROMPT = "장소 소개글을 2~3줄로 자연스럽게 요약하세요. 새로운 정보를 추가하지 마세요."


class Command(BaseCommand):
    help = "전체 Place의 overview를 요약해 overview_summary에 저장한다."

    def handle(self, *args, **options):
        # overview가 있고, 아직 요약이 안 된 것만 대상
        targets = Place.objects.exclude(overview="").filter(overview_summary="")
        total = targets.count()
        self.stdout.write(f"대상 {total}건 요약 시작...")

        for i, place in enumerate(targets, 1):
            # ★ 문장 3개 미만이면 요약 자체를 안 하고 원본을 그대로 씀 (API 호출 절약)
            sentence_count = place.overview.count(".") + place.overview.count("!") + place.overview.count("?")
            if sentence_count < 3:
                place.overview_summary = place.overview
            else:
                try:
                    response = _get_client().chat.completions.create(
                        model="gpt-4o-mini",
                        max_tokens=200,
                        messages=[
                            {"role": "system", "content": SYSTEM_PROMPT},
                            {"role": "user", "content": place.overview},
                        ],
                    )
                    place.overview_summary = response.choices[0].message.content
                except Exception as e:
                    self.stdout.write(self.style.WARNING(f"{place.content_id} 실패: {e}"))
                    continue

            place.save(update_fields=["overview_summary"])
            if i % 50 == 0:
                self.stdout.write(f"{i}/{total} 완료")

        self.stdout.write(self.style.SUCCESS("전체 요약 완료"))