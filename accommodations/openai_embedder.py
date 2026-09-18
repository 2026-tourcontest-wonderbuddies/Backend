from __future__ import annotations
from typing import Sequence
import numpy as np

from apps.recommendation.openai_embedding import embed_texts


class OpenAIEmbedder:
    """`query_fit.Embedder` 프로토콜 구현체. OpenAI API 기반, 서버 메모리 부담 없음."""

    def encode(self, texts: Sequence[str]) -> np.ndarray:
        return embed_texts(list(texts))