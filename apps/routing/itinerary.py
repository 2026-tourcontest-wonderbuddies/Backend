"""
설계 v2 Step 5 — 코스 스케줄링 + 장소 변경 시 연쇄 재계산(Ripple Effect).

핵심 동작:
  · 일자별 장소 시퀀스에 §5 오버헤드 보정 이동시간과 체류시간을 적용해 도착/출발 시각을 산출
  · 장소 변경 시 **영향받는 구간(진입 leg / 진출 leg)만** 재계산하고,
    이후 일정은 경로 재계산 없이 시간만 시프트한다 → 카카오 호출 수를 코스 길이와 무관하게 최대 2건으로 억제

백엔드 인계용 진입점은 Itinerary 클래스이며, to_dict() 는 JSON 직렬화 가능한 구조를 돌려준다.
"""

from __future__ import annotations

import csv
import logging
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from pathlib import Path

from hybrid_engine import KST, PROJECT_DIR, HybridRoutingEngine

# 장소 단위 체류시간 표 (matching/build_stay_table.py 산출물).
# 대표값 STAY_MED_15M 은 15분 격자이고, 서빙용 밴드 STAY_MIN/STAY_MAX 는
# 0.8·1.2 배를 floor15/ceil15 한 값이다 — matching/README.md 및 docs/08.16_체류시간_결정.md 참조.
#
# ※ CSV 의 stay_min 은 **하한**이다. 이 모듈의 Stop.stay_min(대표 체류시간)과 이름만 같고
#   뜻이 다르므로, 밴드는 Stop.stay_lo / Stop.stay_hi 로 받는다.
STAY_TABLE_PATH = PROJECT_DIR / "matching" / "data" / "output" / "stay_time_by_poi.csv"

# 체류시간 격자(분). 대표값은 이미 이 격자로 산출돼 있고, 여기서는 검증에만 쓴다.
# 15 는 LEG_GRID_MIN(5)의 배수라 5분 격자 정렬이 깨지지 않는다.
STAY_GRID_MIN = 15

# 카테고리별 **폴백** 체류시간(분). 체류시간 표에 없는 장소에만 쓴다.
# 2026-08-17 이동행렬 재빌드 이후 place_index 1,982곳이 전부 체류시간 표에 있어
# **현재 폴백 대상은 0곳**이다 (재빌드 전에는 41곳 — 축제공연행사 30·음식점 7·숙박 3·관광지 1).
# 표 없이 라우팅 엔진만 떼어 배포하는 경우와 POI 수집이 다시 어긋나는 경우를 위한 백스톱으로 남긴다.
# TourAPI stay_duration 컬럼은 1,987건 중 98건만 존재하고 그중 84건이 "Unknown" 이라 사용 불가.
# 설계서 §1의 "행동 패턴 학습 자료"인 AI Hub 방문지정보(제주)의 RESIDENCE_TIME_MIN
# 실측 분포에서 산출했다 — calibrate_stay_duration.py 참조.
# 대상 19,809건(제주 방문 로그 + 유형 필터), 중앙값을 30분 격자로 반올림.
# 2026-08-17: 모집단을 정본 파일(제주+도서지역)에서 제주 코스 파일로 좁히고 0분·상한 절단을
# 없앴다(§0 결정과 일치). 표본은 26,655 → 19,809 으로 줄었지만 네 값 모두 그대로다.
DEFAULT_STAY_MIN = {
    "관광지": 60,        # 실측 n=6,379  중앙값 60 (자연·역사·테마·산책로·체험 통합)
    "문화시설": 60,      # 실측 n=707    중앙값 60 (평균 83.2 · IQR 60~90)
    "축제공연행사": 90,  # 실측 근거 없음 — EDA 유형 필터에서 제외된 유형이라 기존값 유지
    "음식점": 60,        # 실측 n=9,176  중앙값 60
    "쇼핑": 30,          # 실측 n=3,109  중앙값 30
    "숙박": 0,           # 숙박은 일자 종료 지점이므로 체류시간을 잡지 않는다
}
FALLBACK_STAY_MIN = 60

# 일정 편성에 쓰이는 이동시간 격자(분).
# 체류시간이 이미 30분 배수이므로 이동시간을 5분 배수로 맞추면 도착·출발 시각 전체가
# 5분 격자에 정렬된다. 이동시간 실측은 연속 분포라(카카오 응답 중 30분 배수는 1.7%뿐)
# 30분으로 끊으면 MAE 가 5.88 → 10.06분으로 캘리브레이션 이득을 통째로 반납하지만,
# 5분 격자의 손실은 0.14분에 그친다. LEG_GRID_MIN = 1 로 두면 격자 없이 동작한다.
LEG_GRID_MIN = 5

logger = logging.getLogger("hybrid_routing.itinerary")


def snap_to_grid(minutes: float) -> int:
    """이동시간을 격자로 반올림한다."""
    return int(round(minutes / LEG_GRID_MIN) * LEG_GRID_MIN)


@dataclass(frozen=True)
class StayEstimate:
    """장소 하나의 체류시간 추정치. representative 가 일정에 반영되는 값이다."""

    representative: int  # STAY_MED_15M — 15분 격자 대표값
    lo: int              # STAY_MIN     — 하한 (floor15(0.8 × 대표값))
    hi: int              # STAY_MAX     — 상한 (ceil15(1.2 × 대표값))
    source: str          # observed / blend / category / lodging_anchor / fallback
    obs_n: int           # 관측 방문 수 (폴백은 0)


def load_stay_table(path: Path = STAY_TABLE_PATH) -> dict[str, StayEstimate]:
    """체류시간 표를 content_id → StayEstimate 로 읽는다.

    표가 없으면 빈 dict 를 돌려주고 DEFAULT_STAY_MIN 폴백으로 동작한다
    (라우팅 엔진만 떼어 배포하는 경우를 위해 필수 의존성으로 두지 않는다).
    """
    if not path.exists():
        logger.warning("체류시간 표를 찾을 수 없어 카테고리 폴백으로 동작합니다: %s", path)
        return {}

    table: dict[str, StayEstimate] = {}
    off_grid: list[str] = []
    with path.open(encoding="utf-8-sig", newline="") as fh:
        for row in csv.DictReader(fh):
            raw = row.get("stay_med_15m", "").strip()
            if not raw:
                continue
            rep = int(float(raw))
            # 격자가 깨지면 도착·출발 시각 정렬이 무너진다. 스냅해서 살리되 반드시 남긴다.
            if rep % LEG_GRID_MIN:
                off_grid.append(f"{row['title']}({rep}분)")
                rep = snap_to_grid(rep)
            table[row["content_id"]] = StayEstimate(
                representative=rep,
                lo=int(float(row["stay_min"])),
                hi=int(float(row["stay_max"])),
                source=row["stay_src"],
                obs_n=int(float(row["obs_n"] or 0)),
            )
    if off_grid:
        logger.warning(
            "체류시간 %d건이 %d분 격자를 벗어나 스냅했습니다: %s",
            len(off_grid), LEG_GRID_MIN, ", ".join(off_grid[:5]),
        )
    logger.info("체류시간 표 %d곳 로드: %s", len(table), path.name)
    return table


_STAY_TABLE: dict[str, StayEstimate] | None = None


def stay_table() -> dict[str, StayEstimate]:
    """프로세스당 1회만 읽는다 (Itinerary 를 요청마다 새로 만들어도 재파싱하지 않는다)."""
    global _STAY_TABLE
    if _STAY_TABLE is None:
        _STAY_TABLE = load_stay_table()
    return _STAY_TABLE


def snap_start_time(moment: datetime) -> datetime:
    """일자 시작 시각을 격자로 **올림**한다.

    내림하면 렌터카 인수·수속이 끝나기 전 시각에 일정이 배치되어 물리적으로 불가능한
    코스가 나온다. 올림이면 가용시간이 최대 4분 줄어들 뿐이고, 슬롯 수는 줄어드는
    방향으로만 변한다(설계서 3.3 기준 최대 1.05%).
    """
    snapped = moment.replace(second=0, microsecond=0)
    if snapped != moment:  # 초 단위 잔여분은 다음 분으로 올린다
        snapped += timedelta(minutes=1)
    remainder = snapped.minute % LEG_GRID_MIN
    if remainder:
        snapped += timedelta(minutes=LEG_GRID_MIN - remainder)
    return snapped


@dataclass
class Stop:
    content_id: str
    title: str
    category: str
    stay_min: int            # 일정에 반영되는 대표 체류시간 (STAY_MED_15M)
    stay_lo: int = 0         # 체류시간 밴드 하한 (CSV STAY_MIN)
    stay_hi: int = 0         # 체류시간 밴드 상한 (CSV STAY_MAX)
    stay_src: str = "fallback"
    stay_obs_n: int = 0
    arrive_at: datetime | None = None
    depart_at: datetime | None = None

    def to_dict(self) -> dict:
        return {
            "content_id": self.content_id,
            "title": self.title,
            "category": self.category,
            "stay_min": self.stay_min,
            "stay_lo": self.stay_lo,
            "stay_hi": self.stay_hi,
            "stay_src": self.stay_src,
            "stay_obs_n": self.stay_obs_n,
            "arrive_at": self.arrive_at.isoformat() if self.arrive_at else None,
            "depart_at": self.depart_at.isoformat() if self.depart_at else None,
        }


@dataclass
class Leg:
    from_id: str
    to_id: str
    from_title: str
    to_title: str
    duration_min: float     # 순수 경로 소요 (보정·격자 적용 전 원본)
    duration_adjusted: int  # §5 보정 후 LEG_GRID_MIN 격자로 반올림 (일정에 반영되는 값)
    distance_m: float
    source: str

    def to_dict(self) -> dict:
        return {
            "from_id": self.from_id,
            "to_id": self.to_id,
            "from_title": self.from_title,
            "to_title": self.to_title,
            "duration_min": self.duration_min,
            "duration_adjusted": self.duration_adjusted,
            "distance_m": self.distance_m,
            "source": self.source,
        }


@dataclass
class ChangeReport:
    """장소 변경 1건이 일으킨 연쇄 효과 요약. 백엔드가 그대로 응답에 실을 수 있다."""

    action: str
    day_index: int
    position: int
    removed: str | None
    added: str | None
    recomputed_legs: list[Leg] = field(default_factory=list)
    total_legs: int = 0
    api_calls_used: int = 0
    end_before: datetime | None = None
    end_after: datetime | None = None

    @property
    def shift_min(self) -> float:
        if not (self.end_before and self.end_after):
            return 0.0
        return round((self.end_after - self.end_before).total_seconds() / 60, 1)

    def to_dict(self) -> dict:
        return {
            "action": self.action,
            "day_index": self.day_index,
            "position": self.position,
            "removed": self.removed,
            "added": self.added,
            "recomputed_legs": [leg.to_dict() for leg in self.recomputed_legs],
            "recomputed_count": len(self.recomputed_legs),
            "total_legs": self.total_legs,
            "api_calls_used": self.api_calls_used,
            "end_before": self.end_before.isoformat() if self.end_before else None,
            "end_after": self.end_after.isoformat() if self.end_after else None,
            "shift_min": self.shift_min,
        }


@dataclass
class DayPlan:
    day_index: int
    start_at: datetime
    stops: list[Stop]
    legs: list[Leg] = field(default_factory=list)

    @property
    def end_at(self) -> datetime | None:
        return self.stops[-1].arrive_at if self.stops else None

    def to_dict(self) -> dict:
        return {
            "day_index": self.day_index,
            "start_at": self.start_at.isoformat(),
            "end_at": self.end_at.isoformat() if self.end_at else None,
            "stops": [s.to_dict() for s in self.stops],
            "legs": [l.to_dict() for l in self.legs],
        }


class Itinerary:
    def __init__(
        self,
        engine: HybridRoutingEngine,
        days: list[list[str]],
        start_times: list[datetime],
        vehicle: str = "car",
        stays: dict[str, StayEstimate] | None = None,
    ):
        if len(days) != len(start_times):
            raise ValueError("days 와 start_times 의 길이가 다릅니다")
        self.engine = engine
        self.vehicle = vehicle
        # 장소 단위 체류시간. 넘기지 않으면 프로세스 공용 표를 쓴다.
        self.stays = stay_table() if stays is None else stays
        # 시작 시각이 격자를 벗어나면 하루 전체 정렬이 무너지므로 올림 스냅한다.
        # 설계서 3.1 기준 1일차(입도일)와 당일치기에서만 발생한다.
        snapped_starts = []
        for i, raw in enumerate(start_times):
            snapped = snap_start_time(raw)
            if snapped != raw:
                logger.info(
                    "Day %d 시작 시각을 %d분 격자로 올림: %s → %s",
                    i + 1, LEG_GRID_MIN, f"{raw:%H:%M}", f"{snapped:%H:%M}"
                )
            snapped_starts.append(snapped)
        self.days = [
            DayPlan(i, start, [self._make_stop(cid) for cid in stop_ids])
            for i, (stop_ids, start) in enumerate(zip(days, snapped_starts))
        ]
        for day in self.days:
            self._rebuild_legs(day)
            self._reschedule(day)

    # --- 구성 요소 -------------------------------------------------------

    def _make_stop(self, content_id: str) -> Stop:
        cid = str(content_id)
        row = self.engine.place(cid)
        category = str(row["content_type_name"])
        est = self.stays.get(cid)
        if est is None:
            # 표에 없는 장소는 카테고리 폴백. 밴드가 없으므로 대표값으로 눌러 둔다.
            fallback = DEFAULT_STAY_MIN.get(category, FALLBACK_STAY_MIN)
            est = StayEstimate(fallback, fallback, fallback, "fallback", 0)
        return Stop(
            content_id=cid,
            title=str(row["title"]),
            category=category,
            stay_min=est.representative,
            stay_lo=est.lo,
            stay_hi=est.hi,
            stay_src=est.source,
            stay_obs_n=est.obs_n,
        )

    def _compute_leg(self, origin: Stop, destination: Stop, realtime: bool) -> Leg:
        result = self.engine.get_travel_time(
            origin.content_id,
            destination.content_id,
            mode="kakao" if realtime else "osrm",
            vehicle=self.vehicle,
        )
        return Leg(
            from_id=origin.content_id,
            to_id=destination.content_id,
            from_title=origin.title,
            to_title=destination.title,
            duration_min=result["duration_min"],
            duration_adjusted=snap_to_grid(result["duration_min_adjusted"]),
            distance_m=result["distance_m"],
            source=result["source"],
        )

    def _rebuild_legs(self, day: DayPlan, realtime: bool = False) -> None:
        day.legs = [
            self._compute_leg(day.stops[i], day.stops[i + 1], realtime)
            for i in range(len(day.stops) - 1)
        ]

    def _reschedule(self, day: DayPlan) -> None:
        """경로 재계산 없이 시각만 앞뒤로 흘려보낸다 (연쇄 시프트)."""
        cursor = day.start_at
        for i, stop in enumerate(day.stops):
            if i > 0:
                cursor += timedelta(minutes=day.legs[i - 1].duration_adjusted)
            stop.arrive_at = cursor
            cursor += timedelta(minutes=stop.stay_min)
            stop.depart_at = cursor

    # --- 변경 연산 (Ripple Effect) --------------------------------------

    def _affected_leg_indices(self, day: DayPlan, position: int) -> list[int]:
        """position 의 진입 leg(position-1)와 진출 leg(position)만 영향을 받는다."""
        return [i for i in (position - 1, position) if 0 <= i < len(day.stops) - 1]

    def _apply(
        self,
        action: str,
        day_index: int,
        position: int,
        mutate,
        removed: str | None,
        added: str | None,
        realtime: bool,
    ) -> ChangeReport:
        day = self.days[day_index]
        report = ChangeReport(
            action=action,
            day_index=day_index,
            position=position,
            removed=removed,
            added=added,
            end_before=day.end_at,
        )
        calls_before = self.engine.cache_misses

        mutate(day)

        # leg 리스트 길이를 stops 에 맞춰 자리만 맞춰 둔다 (내용은 아래에서 채움)
        needed = max(0, len(day.stops) - 1)
        while len(day.legs) < needed:
            day.legs.insert(min(position, len(day.legs)), None)  # type: ignore[arg-type]
        while len(day.legs) > needed:
            day.legs.pop(min(position, len(day.legs) - 1))

        for i in self._affected_leg_indices(day, position):
            day.legs[i] = self._compute_leg(day.stops[i], day.stops[i + 1], realtime)
            report.recomputed_legs.append(day.legs[i])

        # 방어: 위 인덱스 계산에서 빠진 자리가 있으면 사전계산값으로 채운다
        for i, leg in enumerate(day.legs):
            if leg is None:
                day.legs[i] = self._compute_leg(day.stops[i], day.stops[i + 1], realtime=False)

        self._reschedule(day)
        report.total_legs = len(day.legs)
        report.api_calls_used = self.engine.cache_misses - calls_before
        report.end_after = day.end_at
        return report

    def replace_stop(
        self, day_index: int, position: int, new_content_id: str, realtime: bool = True
    ) -> ChangeReport:
        day = self.days[day_index]
        old = day.stops[position]
        new_stop = self._make_stop(new_content_id)
        return self._apply(
            "replace",
            day_index,
            position,
            lambda d: d.stops.__setitem__(position, new_stop),
            removed=old.title,
            added=new_stop.title,
            realtime=realtime,
        )

    def insert_stop(
        self, day_index: int, position: int, new_content_id: str, realtime: bool = True
    ) -> ChangeReport:
        new_stop = self._make_stop(new_content_id)
        return self._apply(
            "insert",
            day_index,
            position,
            lambda d: d.stops.insert(position, new_stop),
            removed=None,
            added=new_stop.title,
            realtime=realtime,
        )

    def remove_stop(self, day_index: int, position: int, realtime: bool = True) -> ChangeReport:
        old = self.days[day_index].stops[position]
        return self._apply(
            "remove",
            day_index,
            position,
            lambda d: d.stops.pop(position),
            removed=old.title,
            added=None,
            realtime=realtime,
        )

    # --- 출력 -------------------------------------------------------------

    def to_dict(self) -> dict:
        return {"vehicle": self.vehicle, "days": [d.to_dict() for d in self.days]}

    def render(self) -> str:
        lines = []
        for day in self.days:
            lines.append(f"Day {day.day_index + 1} (출발 {day.start_at:%H:%M})")
            for i, stop in enumerate(day.stops):
                if i > 0:
                    leg = day.legs[i - 1]
                    lines.append(
                        f"      ↓ 이동 {leg.duration_adjusted:>3d}분 "
                        f"({leg.distance_m / 1000:>5.1f}km, {leg.source})"
                    )
                window = f"{stop.arrive_at:%H:%M}"
                if stop.stay_min:
                    window += f"~{stop.depart_at:%H:%M}"
                    stay = (f"체류 {stop.stay_min:>3d}분 "
                            f"({stop.stay_lo}~{stop.stay_hi} · {stop.stay_src}"
                            + (f" n={stop.stay_obs_n}" if stop.stay_obs_n else "") + ")")
                else:
                    stay = "숙박 앵커 · 일자 종료"
                lines.append(
                    f"  {window:>12}  {stop.title[:32]:34s} [{stop.category}] {stay}")
            lines.append("")
        return "\n".join(lines)


def kst(year: int, month: int, day: int, hour: int, minute: int = 0) -> datetime:
    return datetime(year, month, day, hour, minute, tzinfo=KST)
