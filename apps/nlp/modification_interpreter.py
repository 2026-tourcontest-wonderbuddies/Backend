"""
apps/nlp/modification_interpreter.py — 신규

Pipeline 6.2: 자유 문장 수정 요청을 구조화된 delta로 변환.
"실제 장소 선정과 시간 계산은 추천 로직이 수행하고, LLM은 의도 구조화와 결과 설명만
담당"(기획서 명시) — 이 파일은 장소를 절대 직접 고르지 않는다.
"""

import json
from anthropic import Anthropic  # 또는 OpenAI

client = Anthropic()

SYSTEM_PROMPT = """당신은 여행 코스 수정 요청을 구조화하는 도우미입니다.
사용자의 자유 문장을 읽고 아래 JSON 스키마로만 응답하세요.

{
  "locked_place_ids": [],       // 고정(유지)할 장소명이 언급되면 그 이름을 그대로 배열에 넣음
  "removed_place_ids": [],      // 명시적으로 빼달라고 한 장소
  "adjustments": {
    "walk_light": null,         // true/false/null — "덜 걷는", "많이 안 걷는" 언급 시 true
    "indoor_preference": null,  // "실내로" 언급 시 true
    "add_cafe": false,          // "카페 추가해줘" 등
    "reduce_places": false      // "장소 하나 줄여줘" 등
  },
  "recompute_scope": "full" | "partial"  // 전체 재추천인지, 고정 장소 외만 바꾸는지
}

장소명은 절대 지어내지 말고, 사용자 문장에 나온 이름만 그대로 쓰세요.
"""


def parse_modification_request(raw_message: str) -> dict:
    """
    Pipeline 6.2 의도 구조화. course_builder 재실행 시 이 delta를 넘겨서
    locked=True 처리, exclude_places에 추가, walk_light 플래그 갱신 등에 활용.
    """
    response = client.messages.create(
        model="claude-haiku-4-5-20251001",  # 의도 분류는 가벼운 모델로 충분
        max_tokens=500,
        system=SYSTEM_PROMPT,
        messages=[{"role": "user", "content": raw_message}],
    )
    text = response.content[0].text
    return json.loads(text)


def generate_result_explanation(raw_message: str, before_summary: dict, after_summary: dict) -> str:
    """
    수정 결과를 자연어로 설명. 실제 계산된 수치(이동시간 변화, 여유시간 변화 등)를
    프롬프트에 그대로 넣어서 LLM이 숫자를 지어내지 않게 강제.
    """
    prompt = (
        f"사용자 요청: {raw_message}\n"
        f"변경 전: {json.dumps(before_summary, ensure_ascii=False)}\n"
        f"변경 후: {json.dumps(after_summary, ensure_ascii=False)}\n"
        f"위 변경사항을 자연스러운 한 문장으로 사용자에게 안내해줘."
    )
    response = client.messages.create(
        model="claude-haiku-4-5-20251001",
        max_tokens=200,
        messages=[{"role": "user", "content": prompt}],
    )
    return response.content[0].text