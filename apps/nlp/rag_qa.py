from apps.places.models import Place
from apps.nlp.modification_interpreter import _get_client   # OpenAI 클라이언트 재사용

SYSTEM_PROMPT = """당신은 제주 관광지 정보를 안내하는 도우미입니다.
아래 제공된 장소 정보에 없는 내용은 "정보에 없어 답변드리기 어려워요"라고
솔직히 말하세요. 정보를 지어내지 마세요."""


def answer_place_question(content_id: str, question: str) -> str:
    place = Place.objects.get(content_id=content_id)

    context = (
        f"장소명: {place.title}\n"
        f"분류: {place.content_type_name} / {place.small_category_name}\n"
        f"소개: {place.overview}\n"
        f"운영시간: {place.hours_raw}\n"
        f"휴무일: {place.closed_days_raw}\n"
        f"요금: {place.fees}\n"
        f"주차: {place.parking}\n"
        f"연락처: {place.contact}\n\n"
        f"질문: {question}"
    )

    response = _get_client().chat.completions.create(
        model="gpt-4o-mini",
        max_tokens=400,
        messages=[
            {"role": "system", "content": SYSTEM_PROMPT},
            {"role": "user", "content": context},
        ],
    )
    return response.choices[0].message.content