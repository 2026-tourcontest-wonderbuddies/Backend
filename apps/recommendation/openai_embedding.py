from decouple import config
from openai import OpenAI
import numpy as np

_client = None
EMBEDDING_MODEL = "text-embedding-3-small"


def _get_client():
    global _client
    if _client is None:
        _client = OpenAI(api_key=config("OPENAI_API_KEY"))
    return _client


def embed_texts(texts: list[str]) -> np.ndarray:
    """텍스트 리스트 → (n, d) 벡터 행렬. 배치 스크립트, 실시간 조회 양쪽에서 재사용."""
    if not texts:
        return np.zeros((0, 1536), dtype=np.float32)
    response = _get_client().embeddings.create(model=EMBEDDING_MODEL, input=texts)
    vectors = np.array([d.embedding for d in response.data], dtype=np.float32)
    return vectors


def embed_one(text: str) -> np.ndarray:
    """단일 텍스트 → (d,) 벡터. 사용자 자유입력 실시간 계산용."""
    return embed_texts([text])[0]