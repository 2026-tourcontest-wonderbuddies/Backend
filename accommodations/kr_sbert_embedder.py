"""KR-SBERT 실 임베딩 모델 — `query_fit.CharNgramEmbedder` 폴백을 대체한다.

`query_fit.py`는 numpy만 있으면 동작해야 하므로(테스트가 무거운 의존성 없이 빠르게 돎),
`sentence-transformers` 의존성은 이 애드온 파일에만 둔다. `query_fit.Embedder` 프로토콜
(`encode(list[str]) -> ndarray`)만 만족하면 되므로 이 파일을 아예 안 쓰면 그쪽 모듈은
전과 동일하게 의존성 없이 돈다.

    from kr_sbert_embedder import KrSbertEmbedder
    recommender = LodgingRecommender(embedder=KrSbertEmbedder())

최초 생성 시 HuggingFace 에서 모델을 내려받는다(인터넷 필요). 이후엔 로컬 캐시
(`~/.cache/huggingface`)로 오프라인 동작한다.
"""

from __future__ import annotations

from typing import Sequence

import numpy as np
from sentence_transformers import SentenceTransformer

# SNU NLP Lab 의 한국어 SBERT — KLUE-NLI + STS 증강으로 학습됨.
MODEL_NAME = "snunlp/KR-SBERT-V40K-klueNLI-augSTS"


class KrSbertEmbedder:
    """`query_fit.Embedder` 프로토콜 구현체. 문장 단위 한국어 임베딩."""

    def __init__(self, model_name: str = MODEL_NAME):
        self._model = SentenceTransformer(model_name)

    def encode(self, texts: Sequence[str]) -> np.ndarray:
        return self._model.encode(list(texts), convert_to_numpy=True, show_progress_bar=False)
