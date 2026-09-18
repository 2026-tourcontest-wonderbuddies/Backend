"""
Pipeline 2.6 — 자유입력 임베딩 매칭. LLM이 아니라 KURE-v1(팀이 이미 1,775건
전량 사전계산해서 place_embeddings.npz로 줌) 기반 코사인 유사도 계산.

이 모듈은 apps/nlp가 아니라 apps/recommendation에 둔다 — LLM 호출이 없고
scoring.py의 Pref_k 계산에 바로 쓰이는 "추천 로직의 일부"이기 때문.
"""

import numpy as np
from pathlib import Path
from apps.recommendation.openai_embedding import embed_one

_VECTOR_CACHE = None

def _load_vectors():
    global _VECTOR_CACHE
    if _VECTOR_CACHE is None:
        path = Path("apps/recommendation/data/place_vectors.npz")
        data = np.load(path)
        _VECTOR_CACHE = (data["content_ids"], data["vectors"].astype(np.float32))
    return _VECTOR_CACHE


def calc_nlp_match_scores(free_text: str) -> dict[str, float]:
    if not free_text:
        return {}

    query_vec = embed_one(free_text)
    query_norm = np.linalg.norm(query_vec)
    if query_norm == 0:
        return {}

    content_ids, vectors = _load_vectors()   # ★ 파일에서 읽기 (첫 호출만 느림, 이후 캐시)

    norms = np.linalg.norm(vectors, axis=1)
    norms[norms == 0] = 1.0

    cosine_sims = (vectors @ query_vec) / (norms * query_norm)
    cosine_sims = np.maximum(cosine_sims, 0.0)

    return dict(zip(content_ids.tolist(), cosine_sims.tolist()))