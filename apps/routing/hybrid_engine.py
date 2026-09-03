"""
설계 v2 Step 3+4 — OSRM 사전계산 매트릭스 + 카카오모빌리티 실시간 보정 통합 엔진.

  mode='osrm'  : 사전 계산 매트릭스에서 O(1) 조회 (일반 다일 코스 추천)
  mode='kakao' : Ripple Effect 발생 시에만 실시간 호출. 매번 새로 호출한다.
                 → 일 한도(10,000건) 초과 시 OSRM 값으로 자동 폴백 + 로그 기록

⚠️ 카카오 응답은 **저장하지 않는다** (2026-08-20 확정 · 운영정책 5조 20호).
   성능 목적의 단기 캐시(구 TTL 30분)도 거부된 선례가 있어 캐시 경로를 비활성화했다.
   캐시 백엔드 구현(MemoryCache/RedisCache)은 OSRM 값 공유용으로 남겨 둔다.
   근거: docs/08.20_이동시간_보정_실행안.md §4·§5

카카오 REST API 키는 코드에 넣지 않고 환경변수 KAKAO_REST_API_KEY 에서 읽는다.
"""

from __future__ import annotations

import json
import logging
import os
import time
from dataclasses import dataclass, field
from datetime import datetime, timezone, timedelta
from pathlib import Path

import numpy as np
import pandas as pd
import requests

PROJECT_DIR = Path(__file__).resolve().parent.parent
DATA_DIR = Path(__file__).resolve().parent / "data"
ENV_FILE = PROJECT_DIR / ".env"
KST = timezone(timedelta(hours=9))

KAKAO_URL = "https://apis-navi.kakaomobility.com/v1/directions"
KAKAO_DAILY_LIMIT = 10_000
CACHE_TTL_SEC = 30 * 60
TIME_BUCKET_MIN = 30

# 카카오 응답의 저장(캐시·파일) 허용 여부. **항상 False 여야 한다.**
# 저장 금지가 확정된 사안이므로 환경변수로 켤 수 없게 상수로 고정한다.
# 이 값을 True 로 되돌리면 응답 보관 금지 정책을 다시 위반한다.
KAKAO_RESPONSE_CACHE_ENABLED = False
KAKAO_RESPONSE_STORAGE_ALLOWED = False

# §5 실주행 보정
#   OSRM 값:  T_actual = T_route × ROUTE_FACTOR + OSRM_TRAFFIC_OFFSET_MIN
#   카카오 값: T_actual = T_kakao                (이미 실측 주행시간이므로 보정 금지)
#
# **2026-08-27 비활성화 (1.11 / 9.6 → 1.0 / 0.0).**
#
# 종전 값은 calibrate_overhead.py 로 적합한 것이었다(표본 300쌍, 2026-08-14 18:09 KST,
# 기울기 1.1107 · 절편 9.58분 · R² 0.900). 그런데 VVMS 실측으로 같은 조건에서 대조하니
# **보정을 하면 오히려 두 배 나빠진다**:
#
#     OSRM 원값          MAE 4.44  편향 -3.25
#     OSRM + 1.11T+9.6   MAE 8.28  편향 +7.99   ← 구간마다 8분씩 과대추정
#
# 거리대별로는 10~20km 에서 편향이 +10.84분까지 벌어진다. 절편 9.6분이 짧은 구간을
# 겨냥해 적합된 값인데 전 구간에 가산되기 때문이다. 08.24 가 23쌍으로 예비 판정했던
# 것(과대추정 6.3~7.8분)이 408관측에서 더 크게 확인됐다.
#
# ⚠️ 원값도 편향 -3.25분(과소추정)이 남는다. 0 으로 되돌린 것은 "보정이 필요 없다"가
#    아니라 **"이 보정식이 안 하느니만 못하다"** 는 뜻이다.
#    같은 표본의 재적합값은 `0.819·T + 5.9`(MAE 3.40 · 편향 0.00)지만 OD 16쌍
#    **in-sample** 이라 채택하지 않았다. 표본이 늘면 이 자리를 다시 볼 것.
#
# 근거: docs/08.27_보정식_비활성화.md · 재현은 아래 한 줄
#   python routing/compare_vvms_nodelink.py --router osrm --osrm-correction \
#          --patch --exclude "" --max-dev 60
ROUTE_FACTOR = 1.0
OSRM_TRAFFIC_OFFSET_MIN = 0.0

# 주차·도보(자가용 10분) / 승하차 대기(택시 5분) 오버헤드는 **2026-08-27 팀 합의로 제거**했다.
#
# 팀 협의로 정한 값이었을 뿐 실측 근거가 없었다. OSRM·카카오 어느 응답에도 포함되지 않아
# ROUTE_FACTOR·OSRM_TRAFFIC_OFFSET_MIN 과 같은 방식으로는 측정할 수 없고, AI Hub 이동내역도
# 시각 해상도가 30분이라 10분짜리 상수를 잴 수 없다(설계서 §5-B). 구간당 상수 19.6분의
# 51%를 차지해 민감도가 컸던 것이 제거 사유다.
#
# ⚠️ 0 은 "주차·도보에 시간이 안 든다"는 뜻이 아니라 **모델링하지 않는다**는 뜻이다.
#    일정이 그만큼 낙관적으로 나오므로(하루 5구간이면 50분) 하루 방문 수를 이 값으로
#    판단하지 말 것. 실측이 생기면 여기만 되돌리면 된다 — 순수 가산 상수라 다른 계수에
#    영향을 주지 않는다.
#
# vehicle 은 계속 받는다. 값 검증에 쓰이고, 되돌릴 자리를 남겨 둔다.
OVERHEAD_MIN = {"car": 0, "rental": 0, "taxi": 0}

# 실주행 보정이 필요한 출처 (카카오 응답은 제외 — 이중 계산 방지)
_NEEDS_TRAFFIC_CORRECTION = {"osrm", "osrm_fallback"}

logger = logging.getLogger("hybrid_routing")


def load_api_key() -> str | None:
    """환경변수 우선, 없으면 프로젝트 루트의 .env(gitignore 대상)에서 읽는다."""
    key = os.environ.get("KAKAO_REST_API_KEY")
    if key:
        return key.strip()
    if ENV_FILE.exists():
        for line in ENV_FILE.read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if line.startswith("#") or "=" not in line:
                continue
            name, _, value = line.partition("=")
            if name.strip() == "KAKAO_REST_API_KEY":
                return value.strip().strip("'\"")
    return None


@dataclass
class _CacheEntry:
    duration_min: float
    distance_m: float
    stored_at: float = field(default_factory=time.monotonic)

    def expired(self) -> bool:
        return time.monotonic() - self.stored_at > CACHE_TTL_SEC


class MemoryCache:
    """단일 프로세스용 기본 캐시. 워커마다 분리되므로 다중 워커 배포에는 부적합하다."""

    backend = "memory"

    def __init__(self) -> None:
        self._store: dict[str, _CacheEntry] = {}

    def get(self, key: str) -> tuple[float, float] | None:
        entry = self._store.get(key)
        if entry is None:
            return None
        if entry.expired():
            self._store.pop(key, None)
            return None
        return entry.duration_min, entry.distance_m

    def set(self, key: str, duration_min: float, distance_m: float) -> None:
        self._store[key] = _CacheEntry(duration_min, distance_m)

    def size(self) -> int:
        return sum(1 for e in self._store.values() if not e.expired())

    def clear(self) -> None:
        self._store.clear()


class RedisCache:
    """다중 워커가 공유하는 캐시. TTL 은 Redis 가 직접 만료시킨다."""

    backend = "redis"

    def __init__(self, url: str, ttl: int = CACHE_TTL_SEC, prefix: str = "hybrid_routing:"):
        import redis  # 선택적 의존성

        self.client = redis.Redis.from_url(url, decode_responses=True)
        self.client.ping()
        self.ttl = ttl
        self.prefix = prefix

    def get(self, key: str) -> tuple[float, float] | None:
        raw = self.client.get(self.prefix + key)
        if not raw:
            return None
        duration, distance = raw.split("|")
        return float(duration), float(distance)

    def set(self, key: str, duration_min: float, distance_m: float) -> None:
        self.client.setex(self.prefix + key, self.ttl, f"{duration_min}|{distance_m}")

    def size(self) -> int:
        return sum(1 for _ in self.client.scan_iter(match=self.prefix + "*"))

    def clear(self) -> None:
        for key in self.client.scan_iter(match=self.prefix + "*"):
            self.client.delete(key)


def make_cache(url: str | None = None):
    """REDIS_URL 이 설정되어 있으면 공유 캐시, 아니면 프로세스 메모리 캐시를 쓴다."""
    url = url or os.environ.get("REDIS_URL")
    if not url:
        return MemoryCache()
    try:
        return RedisCache(url)
    except Exception as exc:  # 서버 미기동·모듈 미설치 등
        logger.warning("Redis 캐시 초기화 실패 (%s) — 메모리 캐시로 대체합니다", exc)
        return MemoryCache()


class QuotaExceeded(RuntimeError):
    pass


def refuse_response_storage(script: str, purpose: str) -> int:
    """카카오 응답을 파일로 남기는 스크립트의 공용 차단 가드.

    `KAKAO_RESPONSE_STORAGE_ALLOWED` 가 False 인 동안 실행을 막고 종료코드 2를 돌려준다.
    스크립트를 지우지 않고 남겨 둔 것은 계수 산출 방법론의 기록이자,
    §4 ②안(제주 교통정보 API/AI Hub 라벨로 재적합) 때 재사용할 로직이기 때문이다.
    """
    if KAKAO_RESPONSE_STORAGE_ALLOWED:
        return 0
    print(
        f"""[차단] {script} 는 실행할 수 없습니다.
  이 스크립트는 {purpose}
  카카오모빌리티 응답의 저장은 2026-08-20 자로 금지가 확정됐습니다
  (운영정책 5조 20호 + devtalk 3건). 성능용 단기 캐시도 거부된 선례가 있습니다.

  대체 경로 — docs/08.20_이동시간_보정_실행안.md §3
    제주특별자치도 교통정보 API(1시간 통계)로 시간대별 보정계수를 산출한다.
    이용허락범위 제한이 없어 저장·가공·재사용이 가능하다.

  카카오는 3단계에서 '실시간 표시 후 미저장' 용도로만 남는다 (mode='kakao').
  실행이 정말 필요하면 hybrid_engine.KAKAO_RESPONSE_STORAGE_ALLOWED 의 근거부터 뒤집을 것."""
    )
    return 2


class _DailyQuota:
    """일 단위 카카오 호출 카운터. 프로세스 재기동에도 유지되도록 파일에 보존한다.

    파일 스키마는 kakao_client.QuotaState 와 동일한 {"day", "used"} 를 쓴다.
    두 구현이 같은 파일을 공유하므로 스키마가 어긋나면 서로의 카운터를 0으로
    리셋해 일 한도 추적이 무력화된다. 과거 {"date", "count"} 형식도 읽어준다.
    """

    def __init__(self, path: Path, limit: int = KAKAO_DAILY_LIMIT):
        self.path = path
        self.limit = limit
        self._date, self._count = self._load()

    def _today(self) -> str:
        return datetime.now(KST).strftime("%Y-%m-%d")

    def _load(self) -> tuple[str, int]:
        if self.path.exists():
            try:
                raw = json.loads(self.path.read_text(encoding="utf-8"))
                day = raw.get("day", raw.get("date"))
                used = raw.get("used", raw.get("count"))
                if day == self._today() and used is not None:
                    return day, int(used)
            except (json.JSONDecodeError, ValueError):
                logger.warning("쿼터 파일이 손상되어 초기화합니다: %s", self.path)
        return self._today(), 0

    def _flush(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.path.write_text(
            json.dumps({"day": self._date, "used": self._count}), encoding="utf-8"
        )

    @property
    def remaining(self) -> int:
        self._roll()
        return max(0, self.limit - self._count)

    def _roll(self) -> None:
        today = self._today()
        if today != self._date:  # 자정 경과 시 리셋
            self._date, self._count = today, 0
            self._flush()

    def consume(self) -> None:
        self._roll()
        if self._count >= self.limit:
            raise QuotaExceeded(f"카카오 일 한도 {self.limit}건 소진")
        self._count += 1
        self._flush()


class HybridRoutingEngine:
    def __init__(
        self,
        data_dir: Path = DATA_DIR,
        api_key: str | None = None,
        session: requests.Session | None = None,
        cache=None,
    ):
        self.data_dir = Path(data_dir)

        index = pd.read_parquet(self.data_dir / "place_index.parquet")
        matrix = np.load(self.data_dir / "travel_matrix.npz", allow_pickle=True)
        self.durations_sec = matrix["durations_sec"]
        self.distances_m = matrix["distances_m"]

        self._index = index.set_index("content_id")
        self._pos = {cid: int(i) for cid, i in zip(index["content_id"], index["matrix_idx"])}
        self._coords = {
            cid: (float(lon), float(lat))
            for cid, lon, lat in zip(index["content_id"], index["lon"], index["lat"])
        }

        # 키를 인자로 넘기지 않으면 환경변수 또는 .env 에서 읽는다
        self.api_key = api_key or load_api_key()
        self.session = session or requests.Session()
        self.quota = _DailyQuota(self.data_dir / "kakao_quota.json")
        self.cache = cache if cache is not None else make_cache()
        self.cache_hits = 0
        self.cache_misses = 0

    # --- 공개 인터페이스 -------------------------------------------------

    def get_travel_time(
        self,
        origin_id: str,
        destination_id: str,
        mode: str = "osrm",
        vehicle: str | None = None,
        depart_at: datetime | None = None,
    ) -> dict:
        """
        mode='osrm' : 사전 계산된 매트릭스에서 O(1) 조회 (일반 다일 코스 추천)
        mode='kakao': 실시간 재탐색/챗봇 장소 변경 시에만 호출
        반환: {"duration_min": ..., "distance_m": ..., "source": "osrm"|"kakao"|...}
        vehicle 지정 시 §5 실주행 보정값(duration_min_adjusted)을 함께 반환한다.
        (주차·도보 오버헤드는 2026-08-27 제거 — OVERHEAD_MIN 주석 참조)
        """
        origin_id, destination_id = str(origin_id), str(destination_id)
        for cid in (origin_id, destination_id):
            if cid not in self._pos:
                raise KeyError(f"매트릭스에 없는 content_id: {cid}")

        if mode == "osrm":
            result = self._from_matrix(origin_id, destination_id)
        elif mode == "kakao":
            result = self._from_kakao(origin_id, destination_id, depart_at)
        else:
            raise ValueError(f"지원하지 않는 mode: {mode!r} (osrm|kakao)")

        if vehicle is not None:
            result["duration_min_adjusted"] = self.apply_overhead(
                result["duration_min"], vehicle, source=result["source"]
            )
            result["vehicle"] = vehicle
        return result

    @staticmethod
    def apply_overhead(duration_min: float, vehicle: str, source: str = "osrm") -> float:
        """§5 보정. 출처가 OSRM 계열일 때만 실주행 보정(기울기·절편)을 적용한다.

        카카오 응답은 이미 실시간 교통이 반영된 값이라 보정하면 이중 계산이 된다.

        2026-08-27 부터 주차·도보 오버헤드는 더하지 않는다(`OVERHEAD_MIN` 주석 참조).
        이름은 호출부 호환을 위해 유지한다 — 지금 하는 일은 실주행 보정뿐이다.
        """
        if vehicle not in OVERHEAD_MIN:
            raise ValueError(f"지원하지 않는 vehicle: {vehicle!r} ({'|'.join(OVERHEAD_MIN)})")
        if source in _NEEDS_TRAFFIC_CORRECTION:
            driving_min = duration_min * ROUTE_FACTOR + OSRM_TRAFFIC_OFFSET_MIN
        else:
            driving_min = duration_min
        return round(driving_min + OVERHEAD_MIN[vehicle], 1)

    def place(self, content_id: str) -> pd.Series:
        return self._index.loc[str(content_id)]

    @property
    def cache_size(self) -> int:
        return self.cache.size()

    # --- 내부 구현 -------------------------------------------------------

    def _from_matrix(self, origin_id: str, destination_id: str, source: str = "osrm") -> dict:
        i, j = self._pos[origin_id], self._pos[destination_id]
        return {
            "duration_min": round(float(self.durations_sec[i, j]) / 60, 1),
            "distance_m": round(float(self.distances_m[i, j]), 1),
            "source": source,
        }

    @staticmethod
    def _time_bucket(depart_at: datetime | None) -> str:
        now = depart_at or datetime.now(KST)
        bucket = now.minute // TIME_BUCKET_MIN * TIME_BUCKET_MIN
        return now.strftime(f"%Y%m%dT%H{bucket:02d}")

    @staticmethod
    def cache_key(origin_id: str, destination_id: str, bucket: str) -> str:
        """캐시 키: (출발지 ID, 도착지 ID, 시간대 버킷).

        카카오 경로에서는 더 이상 쓰지 않는다(응답 보관 금지). 3단계에서 OSRM 값
        공유 캐시로 용도를 바꿀 때 재사용할 수 있도록 남겨 둔 헬퍼다.
        """
        return f"{origin_id}|{destination_id}|{bucket}"

    def _fallback(self, origin_id: str, destination_id: str, reason: str) -> dict:
        # 심사 설명 근거로 활용할 폴백 로그
        logger.warning(
            "카카오 폴백 → OSRM 사전계산값 사용 (origin=%s dest=%s reason=%s)",
            origin_id,
            destination_id,
            reason,
        )
        result = self._from_matrix(origin_id, destination_id, source="osrm_fallback")
        result["fallback_reason"] = reason
        return result

    def _from_kakao(self, origin_id: str, destination_id: str, depart_at: datetime | None) -> dict:
        # 캐시 조회·저장 없음 — 카카오 응답 보관 금지(2026-08-20). 매 호출이 곧 실시간 값이다.
        # 그 대가로 동일 구간 반복 조회가 전부 쿼터를 소모하므로, 호출 트리거를
        # "사용자 확정 코스"로 좁히는 작업이 3단계(itinerary.py)에 남아 있다.
        self.cache_misses += 1

        if not self.api_key:
            return self._fallback(origin_id, destination_id, "no_api_key")

        try:
            self.quota.consume()
        except QuotaExceeded:
            return self._fallback(origin_id, destination_id, "daily_limit_exceeded")

        o_lon, o_lat = self._coords[origin_id]
        d_lon, d_lat = self._coords[destination_id]
        try:
            resp = self.session.get(
                KAKAO_URL,
                headers={"Authorization": f"KakaoAK {self.api_key}"},
                params={
                    "origin": f"{o_lon},{o_lat}",
                    "destination": f"{d_lon},{d_lat}",
                    "priority": "RECOMMEND",
                    "car_fuel": "GASOLINE",
                },
                timeout=10,
            )
            resp.raise_for_status()
            payload = resp.json()
        except requests.RequestException as exc:
            return self._fallback(origin_id, destination_id, f"request_error:{type(exc).__name__}")

        routes = payload.get("routes") or []
        if not routes or routes[0].get("result_code") != 0:
            code = routes[0].get("result_code") if routes else payload.get("code")
            msg = routes[0].get("result_msg") if routes else None
            return self._fallback(origin_id, destination_id, f"kakao_result_code:{code}:{msg}")

        summary = routes[0]["summary"]
        duration_min = round(summary["duration"] / 60, 1)
        distance_m = round(float(summary["distance"]), 1)
        # 여기서 cache.set() 을 부르면 응답 보관 금지 위반이다. 반환만 하고 버린다.
        return {"duration_min": duration_min, "distance_m": distance_m, "source": "kakao"}
