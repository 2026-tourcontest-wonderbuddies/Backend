"""
QueryFit — 사용자 자유 입력과 숙소 `search_text` 의 유사도를 0~100 점수로 변환한다.

노션 04-1 숙박 로직 3.2 "자유 입력이 있는 경우" 에서 쓰는 점수다. 음식점 로직의
QueryFit 과 같은 계산 구조(임베딩 → 코사인 유사도 → 0~100)를 그대로 따른다.

임베딩 모델은 **주입식**이다. 프로젝트에 임베딩 인프라가 아직 없어서 외부 의존성 없이
동작하는 `CharNgramEmbedder`(문자 2-gram TF-IDF)를 기본값으로 두었다.
문장 임베딩 모델이 준비되면 `Embedder` 프로토콜(`encode(list[str]) -> np.ndarray`)을
만족하는 객체를 넘기기만 하면 되고, 이 모듈의 나머지 코드는 바꿀 필요가 없다.

    index = QueryFitIndex.build(lodgings)                 # 폴백(의존성 없음)
    index = QueryFitIndex.build(lodgings, embedder=my_st) # 실제 임베딩 모델

⚠️ 폴백 임베더의 한계 — 문자 n-gram 은 표기가 겹칠 때만 점수를 준다.
"오션뷰" ↔ "바다 전망" 처럼 뜻은 같고 글자가 다른 쌍은 못 잡는다. 데모까지는 쓸 수 있어도
"의미 검색" 이라고 설명하면 안 된다. 제출본에는 문장 임베딩 모델을 붙이는 것이 맞다.
"""

from __future__ import annotations

import logging
import math
from collections import Counter
from dataclasses import dataclass
from pathlib import Path
from typing import Protocol, Sequence

import numpy as np

log = logging.getLogger(__name__)

# 코사인 유사도 → QueryFit(0~100) 변환. 음수 코사인은 0 으로 눌러 둔다.
# 선형 변환을 쓰는 이유는 절대값 자체가 아니라 **후보 간 순위**만 쓰기 때문이다
# (상위 10개 선별 + 동률 시 비교). 모델을 바꾸면 점수 분포는 달라지지만 순위는 보존된다.
QUERY_FIT_MAX = 100.0

# 자유 입력 상위 후보군 크기 (노션 3.2 "QueryFit 이 높은 숙소를 최대 10개까지").
PREFERRED_POOL_SIZE = 10


class Embedder(Protocol):
    """텍스트 리스트 → (n, d) 실수 행렬. 정규화 여부는 이 모듈이 알아서 처리한다."""

    def encode(self, texts: Sequence[str]) -> np.ndarray: ...


def _char_ngrams(text: str, n: int = 2) -> list[str]:
    """공백을 지운 문자열의 n-gram. 한국어는 어절 토크나이저 없이도 이 정도면 붙는다."""
    squeezed = "".join(text.split())
    if len(squeezed) < n:
        return [squeezed] if squeezed else []
    return [squeezed[i : i + n] for i in range(len(squeezed) - n + 1)]


class CharNgramEmbedder:
    """문자 2-gram TF-IDF 임베더 (외부 의존성 없는 기본값).

    코퍼스(숙소 `search_text` 212건)로 어휘와 IDF 를 고정한 뒤, 질의도 같은 어휘로 벡터화한다.
    어휘에 없는 질의 n-gram 은 무시된다 — 코퍼스가 212건뿐이라 생기는 구조적 한계다.
    """

    def __init__(self, n: int = 2):
        self.n = n
        self.vocab: dict[str, int] = {}
        self.idf: np.ndarray = np.zeros(0)
        self._fitted = False

    def fit(self, corpus: Sequence[str]) -> "CharNgramEmbedder":
        doc_freq: Counter[str] = Counter()
        grams_per_doc = [set(_char_ngrams(text, self.n)) for text in corpus]
        for grams in grams_per_doc:
            doc_freq.update(grams)

        self.vocab = {gram: i for i, gram in enumerate(sorted(doc_freq))}
        n_docs = max(len(corpus), 1)
        idf = np.zeros(len(self.vocab), dtype=np.float32)
        for gram, index in self.vocab.items():
            idf[index] = math.log((1 + n_docs) / (1 + doc_freq[gram])) + 1.0
        self.idf = idf
        self._fitted = True
        if not self.vocab:
            # 코퍼스에 텍스트가 하나도 없는 퇴화 상황(테스트용 더미 등).
            # 예외를 던지는 대신 전 후보 QueryFit 0 으로 동작시킨다 —
            # 자유 입력이 있어도 순위가 이동시간 기준으로 떨어질 뿐 추천은 계속된다.
            log.warning("코퍼스에서 n-gram 을 뽑지 못했다. QueryFit 은 전부 0 이 된다.")
        return self

    def encode(self, texts: Sequence[str]) -> np.ndarray:
        if not self._fitted:
            raise RuntimeError("fit() 을 먼저 호출해야 한다")
        if not self.vocab:
            return np.zeros((len(texts), 1), dtype=np.float32)
        out = np.zeros((len(texts), len(self.vocab)), dtype=np.float32)
        for row, text in enumerate(texts):
            counts = Counter(_char_ngrams(text, self.n))
            for gram, count in counts.items():
                index = self.vocab.get(gram)
                if index is not None:
                    out[row, index] = (1.0 + math.log(count)) * self.idf[index]
        return out


def _l2_normalize(matrix: np.ndarray) -> np.ndarray:
    norms = np.linalg.norm(matrix, axis=1, keepdims=True)
    norms[norms == 0] = 1.0
    return matrix / norms


@dataclass
class QueryFitIndex:
    """숙소별 `search_text` 임베딩을 미리 계산해 들고 있는 인덱스.

    노션 로직의 "숙소별 search_text 임베딩은 사전에 한 번 생성해 저장한다" 에 해당한다.
    추천 요청마다 다시 만들지 말고 프로세스당 1회 만들어 재사용할 것.
    """

    content_ids: list[str]
    vectors: np.ndarray  # (n, d), L2 정규화 완료
    embedder: Embedder

    @classmethod
    def build(cls, lodgings, embedder: Embedder | None = None) -> "QueryFitIndex":
        texts = [lodging.search_text for lodging in lodgings]
        if embedder is None:
            embedder = CharNgramEmbedder().fit(texts)
        vectors = _l2_normalize(np.asarray(embedder.encode(texts), dtype=np.float32))
        return cls([lodging.content_id for lodging in lodgings], vectors, embedder)

    def score(self, query: str) -> dict[str, float]:
        """자유 입력 → `{content_id: QueryFit(0~100)}`.

        빈 입력이면 빈 dict 를 돌려준다 — 호출부는 이걸로 "자유 입력 없음" 분기를 판단하면 된다.
        """
        query = (query or "").strip()
        if not query:
            return {}
        vector = _l2_normalize(np.asarray(self.embedder.encode([query]), dtype=np.float32))[0]
        cosine = self.vectors @ vector
        scores = np.clip(cosine, 0.0, 1.0) * QUERY_FIT_MAX
        return {cid: round(float(s), 2) for cid, s in zip(self.content_ids, scores)}

    def save(self, path: Path | str) -> None:
        """임베딩 캐시 저장. 임베더 자체는 저장하지 않으므로 질의 시 같은 임베더가 필요하다."""
        np.savez_compressed(
            Path(path), content_ids=np.array(self.content_ids), vectors=self.vectors
        )

    @classmethod
    def load(cls, path: Path | str, embedder: Embedder) -> "QueryFitIndex":
        data = np.load(Path(path), allow_pickle=False)
        return cls([str(c) for c in data["content_ids"]], data["vectors"], embedder)


def top_query_fit(
    scores: dict[str, float], content_ids: Sequence[str], pool_size: int = PREFERRED_POOL_SIZE
) -> set[str]:
    """주어진 후보 중 QueryFit 상위 `pool_size` 개의 content_id 집합.

    노션 04-1 §3.2 "QueryFit 이 높은 숙소를 최대 10개까지 선호 후보군으로 선정한다" 그대로,
    **점수가 0 인 후보도 넣는다.** 기획안이 하한을 두지 않았다.

    ⚠️ 그 대가로 자유 입력과 접점이 전혀 없는 숙소가 "선호 후보군" 에 들어간다 — 점수 0 인
    후보끼리는 `content_id` 순으로 잘리므로, 1등급이 2등급보다 덜 맞는 역전이 생길 수 있다.
    한때 `> 0` 하한을 뒀다가 기획안 대조(2026-08-28)에서 되돌렸다. 하한을 되살리려면
    기획안 §3.2 부터 고칠 것(README §4).
    """
    ranked = sorted(content_ids, key=lambda cid: (-scores.get(cid, 0.0), cid))
    return set(ranked[:pool_size])
