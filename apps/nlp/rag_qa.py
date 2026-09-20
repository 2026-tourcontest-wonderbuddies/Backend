from datetime import datetime
from zoneinfo import ZoneInfo

from apps.places.models import Place
from apps.nlp.modification_interpreter import _get_client   # OpenAI 클라이언트 재사용
from typing import Optional

KST = ZoneInfo("Asia/Seoul")

SYSTEM_PROMPT = """당신은 제주 관광지 정보를 안내하는 도우미입니다.
아래 제공된 장소 정보에 없는 내용은 "정보에 없어 답변드리기 어려워요"라고
솔직히 말하세요. 정보를 지어내지 마세요."""


def answer_place_question(content_id: str, question: str) -> str:
    place = Place.objects.get(content_id=content_id)

    context = (
        f"장소명: {place.title}\n"
        f"주소: {place.address}\n"
        f"분류: {place.content_type_name} / {place.small_category_name}\n"
        f"소개: {place.overview}\n"
        f"운영시간: {place.hours_raw}\n"
        f"휴무일: {place.closed_days_raw}\n"
        f"요금: {place.fees}\n"
        f"주차: {place.parking}\n"
        f"화장실: {place.restroom}\n"
        f"연락처: {place.contact}\n\n"
        f"홈페이지 주소: {place.homepage}\n"
        f"카드 사용 가능 여부: {place.credit_card}\n"
        f"상세 정보: {place.detail_information}\n"
        f"소개글: {place.overview}\n"
        f"장 서는 날: {place.market_days}\n"
        f"판매 물품: {place.sale_items}\n"
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

REASON_PROMPT = """장소가 "왜 이 자리에 추천됐는지"를 아주 짧게(한 문장, 20자
내외) 설명하세요.

- 추천 근거를 그대로 나열하거나 "~에 적합도가 높아", "~목적으로 선택되어",
  "~을 고려해" 같은 기계적인 표현으로 옮기지 마세요.
- 아래 "추천 근거 후보" 중 하나는 문장 안에 반드시 드러나야 합니다(예: 시간대,
  인기, 이동, 선호 태그, 음식/카페 비중, 자유 입력 등 중 하나). 근거를 전혀
  반영하지 않고 장소 특징만 설명하는 문장은 안 됩니다.
- 그 근거 하나를, 소개글에 있는 이 장소만의 구체적인 특징(메뉴, 공간, 활동,
  풍경 등) 하나와 엮어서 한 문장으로 쓰세요. 특징을 여러 개 나열하지 말고
  하나로 압축하세요.
- 장소 이름으로 문장을 시작하지 마세요.
- 문장은 항상 "-습니다/-입니다"체 높임말로 끝내세요. "~하다", "~있다"처럼 끝나는
  평서체는 쓰지 마세요.
- 반드시 문장 하나로만, 최대한 짧게 끝내세요. "또한", "그리고" 등으로 이어서 두 번째 문장을
  새로 만들지 마세요. 마침표는 문장 끝에 한 번만 나와야 합니다."""


def _time_bucket(arrive_at: Optional[datetime]) -> Optional[str]:
    if arrive_at is None:
        return None
    hour = arrive_at.astimezone(KST).hour
    if 5 <= hour < 9:
        return "이른 아침 시간대"
    if 9 <= hour < 11:
        return "오전 시간대"
    if 11 <= hour < 14:
        return "점심 시간대"
    if 14 <= hour < 17:
        return "오후 시간대"
    if 17 <= hour < 20:
        return "저녁 시간대"
    if 20 <= hour < 23:
        return "밤 시간대"
    return "심야/새벽 시간대"


def _matched_food_tag(place, food_pref_1: str, food_pref_2: str, is_relaxed: bool) -> Optional[str]:
    if is_relaxed:
        return None
    from apps.recommendation.food_scoring import FOOD_PREF_TO_TAG
    prefs = [p for p in (food_pref_1, food_pref_2) if p]
    target_tags = {FOOD_PREF_TO_TAG[p] for p in prefs if p in FOOD_PREF_TO_TAG}
    matched = target_tags & set(getattr(place, "food_tags", None) or [])
    return next(iter(matched), None)


def generate_place_recommend_reason(
    place, purpose_main: str, purpose_sub: Optional[str],
    arrive_at: Optional[datetime] = None, travel_min: Optional[float] = None,
    is_relaxed: bool = False, food_pref_1: str = "", food_pref_2: str = "",
    free_text: str = "", cafe_balance: str = "",
) -> str:
    """
    "왜 이 자리에 이 장소가 추천됐는지"를 근거(선호태그 매칭/시간대/인기/동선/
    음식·카페 비중/자유입력) 후보로 모아 제시하고, 소개글의 구체적인 특징과
    자연스럽게 엮어서 한 문장으로 쓰게 한다(근거를 그대로 나열하지 않도록).
    """
    from apps.recommendation.scoring import is_popular_place

    reasons = []

    matched_tag = _matched_food_tag(place, food_pref_1, food_pref_2, is_relaxed)
    if matched_tag:
        reasons.append(f"선택한 선호 음식 태그({matched_tag})와 일치")

    if getattr(place, "food_role", None) and cafe_balance:
        if cafe_balance == "카페중심":
            reasons.append("카페 위주로 둘러보고 싶다는 선택과 어울림")
        elif cafe_balance == "음식점중심":
            reasons.append("제대로 된 식사 위주로 둘러보고 싶다는 선택과 어울림")

    time_bucket = _time_bucket(arrive_at)
    if time_bucket:
        reasons.append(f"도착 예정 시간이 {time_bucket}")

    if is_popular_place(place):
        reasons.append("방문객들에게 인기 있는 곳")

    if travel_min is not None and travel_min <= 15:
        reasons.append(f"직전 장소에서 이동시간이 {int(travel_min)}분으로 짧음")

    if free_text:
        reasons.append(f'사용자 자유 입력("{free_text}")과 관련')

    if not reasons:
        main_score = getattr(place, PURPOSE_TO_SCORE_FIELD[purpose_main], 0) or 0
        sub_score = getattr(place, PURPOSE_TO_SCORE_FIELD[purpose_sub], 0) or 0 if purpose_sub else 0
        chosen_purpose = purpose_main if main_score >= sub_score else purpose_sub
        reasons.append(f"{PURPOSE_LABELS[chosen_purpose]} 목적과 어울림")

    context = (
        f"장소명: {place.title}\n"
        f"소개: {place.overview_summary or place.overview}\n"
        f"추천 근거 후보: {'; '.join(reasons)}\n"
    )

    response = _get_client().chat.completions.create(
        model="gpt-4o-mini", max_tokens=150,
        messages=[
            {"role": "system", "content": REASON_PROMPT},
            {"role": "user", "content": context},
        ],
    )
    return response.choices[0].message.content