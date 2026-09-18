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
import math
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

# §5 오버헤드 보정
#   OSRM 값:  T_actual = T_route × ROUTE_FACTOR + OSRM_TRAFFIC_OFFSET_MIN
#   카카오 값: T_actual = T_kakao              (이미 실측 주행시간이므로 보정 금지)
#   ※ 오버헤드 가산항은 2026-08-29 제거됐다 (아래 SUPPORTED_VEHICLES 주석 참조).
#
# ROUTE_FACTOR / OSRM_TRAFFIC_OFFSET_MIN 은 calibrate_overhead.py 로 적합한 값이다.
# 표본 300쌍, 2026-08-14 18:09 KST 호출 기준: 기울기 1.1107, 절편 9.58분, R² 0.900.
# 초안의 1.1 은 기울기로는 정확했으나 상수항이 누락되어 구간당 평균 9.85분을 과소 추정했다.
# ※ 단일 시간대(퇴근 무렵) 표본이므로 절편은 상한에 가깝다. 시간대별 재적합 필요.
# ⚠️ **2026-08-29 교체** (팀 승인). 근거: docs/08.26_표준노드링크_지오메트리_실행계획.md §6-1 ①
#   ⚠️ 2026-08-29 재적합 (부속도서 페리 제외 후). 첫 적합값(α=0.9025 · β=3.75)은
#      **페리 경로에 오염**돼 있었다 — 60분+ 층의 16.8%가 페리 쌍이었고 그 층이 기울기를
#      지배한다. 제외 후 α 가 0.90 → 1.00 으로 올라갔다.
#   이전 값 ROUTE_FACTOR=1.11 · OSRM_TRAFFIC_OFFSET_MIN=9.6 은 **카카오 300쌍을 2026-08-14
#   18:09(퇴근)에 한 번 재서 얻은 값**이라 상수 셋이 전부 혼잡 시간대 값이었다. 모든 시각에
#   그대로 적용해 자유주행 시간대를 구간당 약 9.5분 과대추정했다.
#   새 값은 ITS 라벨 2,991쌍 × 48셀에서 자유주행(평일 04시)만 떼어 적합한 것이다.
ROUTE_FACTOR = 0.9991          # α — 자유주행 배율. 95% CI 0.9934–1.0048
FREEFLOW_OFFSET_MIN = 1.55     # β — 자유주행 고정지연 (측정값)

# ⚠️ **미검증 상수** (설계 원칙 5). ITS 링크 통행시간에는 교차로 **노드 대기**가 없다.
# 카카오와 겹치는 셀에서만 잴 수 있는데, **기울기가 맞는 셀에서만** 절편차를 읽어야 한다
# (기울기가 다르면 절편이 서로 상쇄된다). 13시 두 셀(일 n=199 · 화 n=200)이 그 조건을
# 만족하고, 예측차가 구간 길이 10~60분에서 −4.9 ~ −4.0분으로 **평평하다**(폭 0.6분) —
# 덧셈항의 서명이다. 18시 셀은 기울기가 0.13 어긋나 읽지 않는다.
# 운영 도착시각 피드백으로 **이 값만** 조정하면 되고, 0 으로 내리면 순수 측정값(1.55)이 된다.
NODE_DELAY_MIN = 4.43

# 자유주행 고정항. k 와 함께 곱해진다(아래 apply_correction 참조).
FIXED_MIN = FREEFLOW_OFFSET_MIN + NODE_DELAY_MIN   # 5.98

# 시간대 혼잡지수 `k(t)` 산출물. **경로 기준**(time_index_route.csv)이 정본이고,
# 없으면 링크 기준(time_index.csv)으로, 그것도 없으면 k≡1 로 떨어진다.
# k≡1 이면 산식이 (OSRM×α + 7.05) 로 자유주행 값을 그대로 낸다 — 설계 §4.5 항등성.
TIME_INDEX_FILES = ("time_index_route.csv", "time_index.csv")
PLACE_ZONE_FILE = "place_zone.csv"

# 쌍별 혼잡 민감도 θ. `build_route_theta.py` 산출물이고 **선택 자산**이다.
#   k_route(쌍, 셀) ≈ 1 + θ(쌍) · ( k_global(셀) − 1 )
# 파일이 없거나 그 쌍이 SENTINEL 이면 권역/전역 곡선으로 조용히 떨어진다.
# 시간 분할 검증에서 전역 대비 MAE −1.008분, θ 근사로 −0.978분(97% 회수)이었다.
# 근거: routing/eval_route_k.py · docs/08.26_..._실행계획.md §8
ROUTE_THETA_FILE = "route_theta.npy"
THETA_SCALE = 10_000
THETA_SENTINEL = 65535

# ⚠️ **오버헤드 상수항(주차·도보 10분 / 승하차 5분)은 제거됐다** (2026-08-29 팀 결정).
# 실측 근거가 끝내 없었고, 측정 수단도 없었다 — OSRM·카카오 어느 응답에도 들어 있지 않고
# AI Hub 이동내역은 시각 해상도가 30분이라 10분짜리 상수를 잴 수 없다(설계서 §5-B).
#
# 제거로 열린 것이 하나 있다. 오버헤드가 빠지면 카카오 절편과 ITS 절편이 **같은 것을 재는 값**이
# 된다(둘 다 주차·도보 없는 문앞-문앞 주행시간). 그 차이 약 3.3분이 ITS 링크속도에 없는
# 교차로 노드 대기이며, 이것이 재적합의 근거가 됐다 —
# docs/08.26_표준노드링크_지오메트리_실행계획.md §6-1 ①.
SUPPORTED_VEHICLES = ("car", "rental", "taxi")

# ponytail: OSRM nearest API로 실측 스냅 거리를 재는 대신, 이미 로드된 OSRM
# 도로거리 ÷ 직선거리 비율로 근사한다. 페리로만 닿는 부속도서 등에서 비율이
# 비정상적으로 커진다. 오탐이 잦으면 OSRM nearest 연동으로 교체.
KAKAO_ROUTABLE_MAX_RATIO = 5.0

# 실주행 보정이 필요한 출처 (카카오 응답은 제외 — 이중 계산 방지)
_NEEDS_TRAFFIC_CORRECTION = {"osrm", "osrm_fallback"}


def _haversine_m(a: tuple[float, float], b: tuple[float, float]) -> float:
    """두 (lon, lat) 사이 직선거리(m)."""
    lon1, lat1 = a
    lon2, lat2 = b
    dlat = math.radians(lat2 - lat1)
    dlon = math.radians(lon2 - lon1)
    h = (math.sin(dlat / 2) ** 2
         + math.cos(math.radians(lat1)) * math.cos(math.radians(lat2)) * math.sin(dlon / 2) ** 2)
    return 6_371_000.0 * 2 * math.asin(math.sqrt(h))


def _load_holidays(data_dir: Path) -> set:
    """`holidays.csv` 의 날짜 집합. 대체공휴일까지 넣어야 한다.

    ⚠️ 요일만으로 가르면 대체공휴일을 평일로 넣는다. 2026-08-17(광복절 대체)이 실측으로
    **주말 곡선에 3.2배 가깝다**(EDA §6). 파일이 없으면 토·일만 weekend 로 분류한다.
    """
    path = data_dir / "holidays.csv"
    if not path.exists():
        logger.warning("공휴일 목록이 없습니다(holidays.csv) — 토·일만 weekend 로 봅니다")
        return set()
    col = pd.read_csv(path, dtype=str)
    name = "date" if "date" in col.columns else col.columns[0]
    return {datetime.strptime(v.strip(), "%Y-%m-%d").date() for v in col[name].dropna()}


def _load_time_index(data_dir: Path) -> dict[tuple[str, str, int], float]:
    """`(zone, daytype, hour) → k`. 채택 표시가 있는 행만 싣는다.

    `source` 가 `reference_route` 인 행은 **산출은 됐지만 채택되지 않은** 곡선이다
    (§S5 — 서귀포·권역간은 홀드아웃에서 전역 곡선이 더 나았다). 실으면 안 된다.
    """
    for name in TIME_INDEX_FILES:
        path = data_dir / name
        if not path.exists():
            continue
        table = pd.read_csv(path)
        if "source" in table.columns:
            table = table[table["source"].isin({"fitted", "fitted_route", "merged"})]
        if "zone" not in table.columns:
            table = table.assign(zone="ALL")
        logger.info("시간대 지수 로드: %s (%d행)", name, len(table))
        return {
            (str(z), str(d), int(h)): float(k)
            for z, d, h, k in zip(table["zone"], table["daytype"], table["hour"], table["k"])
        }
    logger.warning("시간대 지수 파일이 없어 k≡1 로 동작합니다 (설계 §4.5 항등성)")
    return {}

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

        # 시간대 혼잡지수와 권역. 둘 다 **선택 자산**이다 — 없으면 k≡1 로 떨어진다.
        self._k_index = _load_time_index(self.data_dir)
        zone_path = self.data_dir / PLACE_ZONE_FILE
        self._zone_of: dict[str, str] = {}
        if zone_path.exists():
            zt = pd.read_csv(zone_path, dtype={"content_id": str})
            self._zone_of = dict(zip(zt["content_id"], zt["zone"]))
        self._holidays = _load_holidays(self.data_dir)

        theta_path = self.data_dir / ROUTE_THETA_FILE
        self._theta = np.load(theta_path) if theta_path.exists() else None
        if self._theta is not None:
            filled = int((self._theta != THETA_SENTINEL).sum())
            logger.info("쌍별 θ 로드: %s (%d쌍)", ROUTE_THETA_FILE, filled)

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
            k, zone = (self.congestion_factor(origin_id, destination_id, depart_at)
                       if result["source"] in _NEEDS_TRAFFIC_CORRECTION else (1.0, "ALL"))
            result["duration_min_adjusted"] = self.apply_correction(
                result["duration_min"], vehicle, source=result["source"], k=k
            )
            result["vehicle"] = vehicle
            result["k"] = round(k, 4)
            result["zone"] = zone
        return result

    def congestion_factor(
        self, origin_id: str, destination_id: str, depart_at: datetime | None
    ) -> tuple[float, str]:
        """`k(출발시각, …)` 과 그 값이 어디서 왔는지. 모르면 (1.0, "ALL").

        조회는 **3단**이고 정밀한 쪽이 먼저다 — `pair`(쌍별 θ) → 권역 → `ALL`.
        각 단계의 자산이 없으면 다음으로 조용히 떨어진다.

        **출발 시각 기준이다** (설계 §10 ⑥). 전방 패스에서 이미 확정된 유일한 값이라서다.
        권역은 양 끝 POI 가 **같은 권역일 때만** 그 권역 곡선을 쓰고, 다르면 `ALL` 로 간다 —
        권역 간 이동은 쌍마다 두 권역을 지나는 비율이 달라 단일 곡선으로 뭉갤 수 없다(§S5).
        """
        if not self._k_index or depart_at is None:
            return 1.0, "ALL"
        stamp = depart_at.astimezone(KST) if depart_at.tzinfo else depart_at
        daytype = ("weekend"
                   if stamp.weekday() >= 5 or stamp.date() in self._holidays
                   else "weekday")

        k_all = self._k_index.get(("ALL", daytype, stamp.hour))

        # ① 쌍별 θ — 가장 정밀하다. 사전계산된 쌍에서만 있다.
        if self._theta is not None and k_all is not None:
            raw = int(self._theta[self._pos[origin_id], self._pos[destination_id]])
            if raw != THETA_SENTINEL:
                theta = raw / THETA_SCALE
                return 1.0 + theta * (k_all - 1.0), "pair"

        # ② 권역 — 양 끝 POI 가 **같은 권역일 때만**. 권역 간 이동은 쌍마다 두 권역을 지나는
        #    비율이 달라 단일 곡선으로 뭉갤 수 없다(§S5).
        zo, zd = self._zone_of.get(origin_id), self._zone_of.get(destination_id)
        zone = zo if (zo is not None and zo == zd) else "ALL"
        for candidate in (zone, "ALL"):
            k = self._k_index.get((candidate, daytype, stamp.hour))
            if k is not None:
                return float(k), candidate
        return 1.0, "ALL"

    @staticmethod
    def apply_correction(duration_min: float, vehicle: str, source: str = "osrm",
                         k: float = 1.0) -> float:
        """§5 보정. 출처가 OSRM 계열일 때만 실주행 보정을 적용한다.

            T = OSRM × α × k(t)  +  β + NODE_DELAY

        **고정항은 `k` 밖에 둔다**(설계 §4.1 원안 = 구조 B). 2026-08-29 부속도서 제외 전
        데이터로는 구조 C 가 나아 보였으나, 페리 경로가 회귀의 긴 쪽을 끌어내린 결과였다.
        깨끗한 라벨로 다시 재면 카카오 3셀 평균 절대오차가 **B 1.28분 vs C 1.91분** 이다.
        근거: docs/08.26_..._실행계획.md §6-1 ①

        카카오 응답은 이미 실시간 교통이 반영된 값이라 α·β·k 를 **적용하지 않는다** —
        하면 이중 계산이 된다.

        `vehicle` 은 **검증만 하고 값에는 쓰지 않는다** — 오버헤드 제거(2026-08-29) 이후
        차종별로 갈리는 항이 없어졌다. 호출부 계약을 유지하려고 인자는 남겨 둔다.
        """
        if vehicle not in SUPPORTED_VEHICLES:
            raise ValueError(f"지원하지 않는 vehicle: {vehicle!r} ({'|'.join(SUPPORTED_VEHICLES)})")
        if source in _NEEDS_TRAFFIC_CORRECTION:
            driving_min = duration_min * ROUTE_FACTOR * k + FIXED_MIN
        else:
            driving_min = duration_min
        return round(driving_min, 1)

    def kakao_routable(self, origin_id: str, destination_id: str) -> bool:
        """목적지가 카카오로 경로 탐색이 될 만한 곳인지 사전 판단.

        OSRM 도로거리 ÷ 직선거리 비율이 `KAKAO_ROUTABLE_MAX_RATIO` 를 넘으면
        페리로만 닿는 부속도서 등 도로로 이어지지 않는 구간으로 보고 False —
        실패할 게 뻔한 카카오 호출과 쿼터 소모를 미리 피한다.
        """
        straight_m = _haversine_m(self._coords[origin_id], self._coords[destination_id])
        if straight_m < 50:  # 같은 지점 근방
            return True
        i, j = self._pos[origin_id], self._pos[destination_id]
        road_m = float(self.distances_m[i, j])
        return road_m / straight_m <= KAKAO_ROUTABLE_MAX_RATIO

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

        if not self.kakao_routable(origin_id, destination_id):
            return self._fallback(origin_id, destination_id, "not_routable")

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


def _demo() -> None:
    """ponytail self-check: _haversine_m 및 routable 임계값 로직만 검증한다."""
    seoul, busan = (126.9780, 37.5665), (129.0756, 35.1796)
    d = _haversine_m(seoul, busan)
    assert 320_000 < d < 330_000, f"직선거리 계산이 어긋남: {d}"

    assert (150_000 / 100_000) <= KAKAO_ROUTABLE_MAX_RATIO  # 일반적인 도로 우회
    assert (600_000 / 100_000) > KAKAO_ROUTABLE_MAX_RATIO   # 페리급 비정상 우회
    print("hybrid_engine self-check OK")


if __name__ == "__main__":
    _demo()
