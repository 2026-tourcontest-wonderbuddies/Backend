"""
설계 v2 §4 — 카카오모빌리티 길찾기 연동 (실시간 보정 레이어).

핵심 정책:
  - Ripple Effect(장소 변경/현재 위치 재탐색) 발생 시에만 호출
  - 응답은 저장하지 않는다 (2026-08-20 확정 · 운영정책 5조 20호)
  - 일 10,000건 한도 소진 시 None 반환 → 호출부가 OSRM 사전 계산값으로 폴백
  - 폴백/한도초과/오류는 모두 로그로 남겨 심사 근거 자료로 활용

⚠️ 이 모듈은 `hybrid_engine.HybridRoutingEngine` 이전 세대의 클라이언트로, 현재
   호출부는 `verify_kakao.py` 뿐이며 그 스크립트는 이미 옛 엔진 API 를 참조해 동작하지
   않는다. 0단계(08.20)에서는 여기 남아 있던 TTL 30분 응답 캐시만 비활성화했다.
   근거: docs/08.20_이동시간_보정_실행안.md §4

API 키는 코드에 넣지 않고 환경변수 KAKAO_REST_API_KEY 또는 routing/.env 에서 읽는다.
"""

from __future__ import annotations

import json
import logging
import os
import threading
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

import requests

log = logging.getLogger("routing.kakao")

KST = timezone(timedelta(hours=9))
API_URL = "https://apis-navi.kakaomobility.com/v1/directions"

DAILY_LIMIT = 10_000
CACHE_TTL_SEC = 1800  # 30분 (캐시 비활성이라 현재 미사용)
TIME_BUCKET_MIN = 30  # 시간대 버킷 폭

# 카카오 응답 캐시 저장 허용 여부. **항상 False 여야 한다.**
# hybrid_engine.KAKAO_RESPONSE_CACHE_ENABLED 와 같은 정책이며, 이 모듈이 독립적으로
# 응답을 들고 있지 않도록 여기서도 따로 끈다.
RESPONSE_CACHE_ENABLED = False


def load_api_key(env_path: Path | None = None) -> str | None:
    """환경변수 우선, 없으면 routing/.env 에서 KAKAO_REST_API_KEY 를 읽는다."""
    key = os.environ.get("KAKAO_REST_API_KEY")
    if key:
        return key.strip()

    env_path = env_path or Path(__file__).resolve().parent / ".env"
    if not env_path.exists():
        return None
    for line in env_path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        name, _, value = line.partition("=")
        if name.strip() == "KAKAO_REST_API_KEY":
            return value.strip().strip('"').strip("'")
    return None


def time_bucket(when: datetime | None = None) -> str:
    """시간대 버킷 문자열. 캐시 비활성 이후로는 로그·디버깅 용도로만 남는다."""
    when = (when or datetime.now(KST)).astimezone(KST)
    return f"{when:%Y%m%d}-{when.hour:02d}{(when.minute // TIME_BUCKET_MIN) * TIME_BUCKET_MIN:02d}"


@dataclass
class _CacheEntry:
    value: dict
    expires_at: float


@dataclass
class QuotaState:
    """일 호출 한도 카운터. 프로세스 재시작에도 유지되도록 파일에 기록한다."""

    path: Path
    limit: int = DAILY_LIMIT
    day: str = field(default_factory=lambda: date.today().isoformat())
    used: int = 0

    def __post_init__(self) -> None:
        if self.path.exists():
            try:
                saved = json.loads(self.path.read_text(encoding="utf-8"))
                if saved.get("day") == self._today():
                    self.day, self.used = saved["day"], int(saved["used"])
            except (ValueError, KeyError, OSError) as exc:
                log.warning("쿼터 상태 파일 읽기 실패, 0에서 시작: %s", exc)

    @staticmethod
    def _today() -> str:
        return datetime.now(KST).date().isoformat()

    def _roll_over(self) -> None:
        today = self._today()
        if self.day != today:
            log.info("카카오 일 한도 리셋 (%s → %s, 전일 사용 %d건)", self.day, today, self.used)
            self.day, self.used = today, 0

    @property
    def remaining(self) -> int:
        self._roll_over()
        return max(self.limit - self.used, 0)

    def consume(self) -> bool:
        self._roll_over()
        if self.used >= self.limit:
            return False
        self.used += 1
        self._flush()
        return True

    def _flush(self) -> None:
        try:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            self.path.write_text(
                json.dumps({"day": self.day, "used": self.used}), encoding="utf-8"
            )
        except OSError as exc:
            log.warning("쿼터 상태 저장 실패: %s", exc)


class KakaoDirectionsClient:
    """카카오모빌리티 길찾기 클라이언트 (캐시 + 일 한도 관리 포함)."""

    def __init__(
        self,
        api_key: str | None = None,
        *,
        daily_limit: int = DAILY_LIMIT,
        cache_ttl_sec: int = CACHE_TTL_SEC,
        state_path: Path | None = None,
        timeout: float = 5.0,
    ) -> None:
        self.api_key = api_key or load_api_key()
        self.cache_ttl_sec = cache_ttl_sec
        self.timeout = timeout
        self._cache: dict[tuple[str, str, str], _CacheEntry] = {}
        self._lock = threading.Lock()
        self._session = requests.Session()
        self.quota = QuotaState(
            path=state_path or Path(__file__).resolve().parent / "data" / "kakao_quota.json",
            limit=daily_limit,
        )
        self.stats = {"hit": 0, "miss": 0, "call": 0, "quota_block": 0, "error": 0}

    @property
    def enabled(self) -> bool:
        return bool(self.api_key)

    def get_route(
        self,
        origin: tuple[float, float],
        destination: tuple[float, float],
        *,
        origin_id: str,
        destination_id: str,
        when: datetime | None = None,
    ) -> dict | None:
        """
        경로 조회. 좌표는 (lon, lat) 순서.
        성공 시 {"duration_sec", "distance_m", "cached"}, 폴백이 필요하면 None.
        """
        if not self.enabled:
            log.warning("카카오 API 키 없음 → OSRM 폴백 (%s→%s)", origin_id, destination_id)
            return None

        # 캐시 조회 없음 — 카카오 응답 보관 금지(2026-08-20). 매 호출이 실호출이다.
        key = (origin_id, destination_id, time_bucket(when))
        if RESPONSE_CACHE_ENABLED and (cached := self._cache_get(key)) is not None:
            self.stats["hit"] += 1
            return {**cached, "cached": True}
        self.stats["miss"] += 1

        if not self.quota.consume():
            self.stats["quota_block"] += 1
            log.warning(
                "카카오 일 한도(%d건) 초과 → OSRM 폴백 (%s→%s)",
                self.quota.limit, origin_id, destination_id,
            )
            return None

        try:
            resp = self._session.get(
                API_URL,
                params={
                    "origin": f"{origin[0]},{origin[1]}",
                    "destination": f"{destination[0]},{destination[1]}",
                    "priority": "RECOMMEND",
                    "car_fuel": "GASOLINE",
                },
                headers={"Authorization": f"KakaoAK {self.api_key}"},
                timeout=self.timeout,
            )
            resp.raise_for_status()
            payload = resp.json()
        except requests.RequestException as exc:
            self.stats["error"] += 1
            log.warning("카카오 호출 실패 → OSRM 폴백 (%s→%s): %s", origin_id, destination_id, exc)
            return None

        routes = payload.get("routes") or []
        if not routes or routes[0].get("result_code") != 0:
            self.stats["error"] += 1
            reason = routes[0].get("result_msg") if routes else payload
            log.warning("카카오 경로 없음 → OSRM 폴백 (%s→%s): %s", origin_id, destination_id, reason)
            return None

        summary = routes[0]["summary"]
        result = {
            "duration_sec": float(summary["duration"]),
            "distance_m": float(summary["distance"]),
        }
        self.stats["call"] += 1
        if RESPONSE_CACHE_ENABLED:  # 항상 False — 응답 보관 금지
            self._cache_put(key, result)
        return {**result, "cached": False}

    # --- 캐시 ---------------------------------------------------------

    def _cache_get(self, key) -> dict | None:
        import time

        with self._lock:
            entry = self._cache.get(key)
            if entry is None:
                return None
            if entry.expires_at <= time.monotonic():
                del self._cache[key]
                return None
            return dict(entry.value)

    def _cache_put(self, key, value: dict) -> None:
        import time

        with self._lock:
            self._cache[key] = _CacheEntry(
                value=dict(value), expires_at=time.monotonic() + self.cache_ttl_sec
            )
