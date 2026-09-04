"""
Pipeline 2.6 — 자유입력 임베딩 매칭. LLM이 아니라 KURE-v1(팀이 이미 1,775건
전량 사전계산해서 place_embeddings.npz로 줌) 기반 코사인 유사도 계산.

이 모듈은 apps/nlp가 아니라 apps/recommendation에 둔다 — LLM 호출이 없고
scoring.py의 Pref_k 계산에 바로 쓰이는 "추천 로직의 일부"이기 때문.
"""

import numpy as np
from sentence_transformers import SentenceTransformer

_model = None
_place_ids = None
_place_vectors = None


def _load_model():
    global _model
    if _model is None:
        _model = SentenceTransformer("nlpai-lab/KURE-v1")  # 팀이 쓴 것과 동일 모델
    return _model


def load_place_embeddings(npz_path: str):
    """
    서버 기동 시 1회만 호출 (apps/recommendation/apps.py의 ready()에서).
    팀이 준 place_embeddings.npz(관광지·문화시설·쇼핑 + 음식점 전체 1,775건)를 메모리에 로드.
    """
    global _place_ids, _place_vectors
    data = np.load(npz_path, allow_pickle=True)
    _place_ids = data["content_ids"]      # 팀 npz 실제 키 이름 확인 필요
    _place_vectors = data["vectors"]       # (1775, 1024) — L2 정규화 여부도 확인 필요


def calc_nlp_match_scores(free_text: str) -> dict[str, float]:
    """
    자유입력 문장 하나를 받아, 전체 장소에 대한 0~1 유사도 점수를 반환.
    scoring.py의 calc_pref()에서 place.content_id로 조회해서 Match_NLP로 사용.

    Returns: {content_id: 0~1 유사도, ...}
    """
    if not free_text or _place_vectors is None:
        return {}

    model = _load_model()
    query_vector = model.encode([free_text], normalize_embeddings=True)[0]

    # 코사인 유사도 (place_vectors가 이미 정규화돼 있다는 전제, 팀 확인 필요)
    similarities = _place_vectors @ query_vector  # (1775,) 벡터

    return {pid: float(sim) for pid, sim in zip(_place_ids, similarities)}