from apps.places.models import Place
from apps.nlp.modification_interpreter import _get_client   # OpenAI 클라이언트 재사용
from typing import Optional

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


PURPOSE_LABELS = {
    "nature": "힐링/자연", "food": "음식/카페", "photo": "사진/감성",
    "culture": "문화/역사", "activity": "체험/액티비티", "shopping": "쇼핑/시장",
}
PURPOSE_TO_SCORE_FIELD = {
    "nature": "score_nature", "food": "score_food", "photo": "score_photo",
    "culture": "score_culture", "activity": "score_activity", "shopping": "score_shopping",
}

REASON_PROMPT = """장소의 특징 점수와 소개글을 참고해서, 왜 이 장소가 추천됐는지
자연스러운 한 문장으로 설명하세요.

- 장소 이름으로 문장을 시작하지 마세요. 그 장소의 특징이나 매력 포인트로 문장을 시작하세요.
- "~로 추천됩니다"처럼 끝내지 마세요. "~하기 좋은 곳입니다", "~을 만끽할 수 있습니다"처럼
  자연스럽게 마무리하세요.
- 특징 한 가지만 나열하지 말고, 두 가지를 자연스럽게 이어서 설명하세요.
  예: "~을 즐길 수 있어, ~도 함께 느낄 수 있는 곳입니다", "~이 있는 곳으로, ~도 인상적입니다".
- 반드시 문장 하나로만 끝내세요. "또한", "그리고" 등으로 이어서 두 번째 문장을 새로 만들지
  마세요. 마침표는 문장 끝에 한 번만 나와야 합니다."""

def generate_place_recommend_reason(place, purpose_main: str, purpose_sub: Optional[str]) -> str:
    """
    purpose_main/sub 중 이 장소 점수가 더 높은 쪽을 골라서 이유를 만듦.
    """
    main_score = getattr(place, PURPOSE_TO_SCORE_FIELD[purpose_main], 0) or 0
    if purpose_sub:
        sub_score = getattr(place, PURPOSE_TO_SCORE_FIELD[purpose_sub], 0) or 0
        chosen_purpose, chosen_score = (
            (purpose_main, main_score) if main_score >= sub_score else (purpose_sub, sub_score)
        )
    else:
        chosen_purpose, chosen_score = purpose_main, main_score

    context = (
        f"장소명: {place.title}\n"
        f"목적: {PURPOSE_LABELS[chosen_purpose]} (적합도 {chosen_score}점/100점)\n"
        f"소개: {place.overview_summary or place.overview}\n"
    )

    from apps.nlp.modification_interpreter import _get_client
    response = _get_client().chat.completions.create(
        model="gpt-4o-mini", max_tokens=150,
        messages=[
            {"role": "system", "content": REASON_PROMPT},
            {"role": "user", "content": context},
        ],
    )
    return response.choices[0].message.content