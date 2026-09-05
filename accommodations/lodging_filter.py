"""
숙박 후보 필터링 — 노션 04-1 숙박 로직 §2 (기본 필터 + Unknown 처리).

필터의 결과는 bool 이 아니라 **3분류**다. 판정을 가르는 건 "숙소가 조건에 맞는가" 가
아니라 **"데이터가 무엇을 말하는가"** 다.

    데이터가 만족한다고 말함   → 카드로 나온다 (`Verdict.PASS`)
    데이터가 안 된다고 말함    → 후보 목록에서 **삭제** (`Verdict.EXCLUDED`)
    데이터가 비어 있음        → 카드로 나온다 + 확인 필요 표시 (`Verdict.NEEDS_CHECK`)

`EXCLUDED` 는 순위가 밀리는 게 아니라 목록에서 사라지는 것이다 — 취사 요청이면
'불가능' 인 숙소만 사라지고, 취사 정보가 빈 숙소는 남는다.

`NEEDS_CHECK` 를 `EXCLUDED` 로 접으면 안 된다. 예를 들어 4명 여행에서
`room_capacity_summary` 가 비어 있는 숙소 72곳(212건 중 34%)이 통째로 사라진다.
Unknown 은 "안 되는 곳" 이 아니라 "전화로 확인해야 하는 곳" 이다.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from datetime import date, timedelta
from enum import Enum

from accommodations.lodging_data import ANY_TYPE, LODGING_TYPE_MAP, Lodging, Tri

log = logging.getLogger(__name__)


class Verdict(str, Enum):
    PASS = "pass"
    NEEDS_CHECK = "needs_check"
    EXCLUDED = "excluded"


@dataclass
class ConditionCheck:
    """필수조건 1건의 판정 결과. 사용자 카드의 '확인 필요' 문구가 여기서 나온다."""

    name: str
    status: Tri
    detail: str = ""

    def to_dict(self) -> dict:
        return {"name": self.name, "status": self.status.value, "detail": self.detail}


@dataclass
class FilterResult:
    lodging: Lodging
    verdict: Verdict
    checks: list[ConditionCheck] = field(default_factory=list)

    @property
    def unknown_count(self) -> int:
        """필수조건 중 확인 불가 개수. 동률일 때 적은 쪽을 우선하는 정렬 키다."""
        return sum(1 for c in self.checks if c.status is Tri.UNKNOWN)

    @property
    def unknown_fields(self) -> list[str]:
        return [c.name for c in self.checks if c.status is Tri.UNKNOWN]

    @property
    def excluded_reasons(self) -> list[str]:
        """제외 사유 **전부**. 한 숙소가 여러 조건에 동시에 걸리는 경우가 흔하다."""
        return [c.name for c in self.checks if c.status is Tri.NO]

    @property
    def excluded_reason(self) -> str | None:
        """대표 사유 1개(검사 순서상 첫 번째).

        ⚠️ 카드 한 줄에 쓰는 용도다. **"왜 후보가 부족한가" 를 집계할 때 쓰면 안 된다** —
        여러 조건에 걸린 숙소가 첫 사유로만 세어져 실제 원인이 가려진다.
        집계에는 `exclusion_summary()` 를 쓸 것.
        """
        for check in self.checks:
            if check.status is Tri.NO:
                return f"{check.name}: {check.detail or '조건 불충족'}"
        return None


def _shift_date(iso: str, days: int) -> str:
    """'YYYY-MM-DD' 를 days 만큼 민다. 월말·연말 넘김은 date 가 알아서 처리한다."""
    return (date.fromisoformat(iso) + timedelta(days=days)).isoformat()


# --- 여행 폼 정책 (설계서 Pipeline 2 §2.1) ---------------------------------
#
# ⚠️ 아래 두 상수는 **숙박이 아니라 여행 입력 폼의 정책**이다. 프로젝트에 폼 계층 코드가
# 없어서 여기서 처음 상수화했다 — `region_of()` 와 같은 사정이다. 폼 계층이 생기면 그쪽으로
# 옮기고 여기선 import 만 할 것. 인원 수는 숙박뿐 아니라 식당 좌석·렌터카 인승에도 쓰인다.

# 인원 스테퍼 기본값. AI Hub 제주 여행 1,488건 실측(2026-08-27): 중앙 2명 · 평균 2.43명.
# 2명이 716건(48.1%)으로 압도적이고 1~2명이 68.1% 다.
#
# 폼은 인원을 **숫자로 직접** 받는다. 동행자 유형(혼자·연인·친구·…)을 받아 인원을 추정하던
# 단계는 없앴다 — 유형이 실측·필터·점수식 어디에도 쓰이지 않아서 "유형을 받아 인원으로
# 바꾸는 것" 이 그 입력의 유일한 용도였고, 그러면 추정 오차만 남는다("친구" 가 2명인지
# 5명인지 모른다). 인원이 틀리면 정원 필터와 Trip.com 딥링크 `adult` 가 동시에 어긋난다.
DEFAULT_GUESTS = 2

# 스테퍼 상한. TourAPI 숙박에서 **확인된 최대 정원이 20명**이라 그 위로는 정원 조건을
# 충족 판정할 수 있는 숙소가 아예 없다(전부 NEEDS_CHECK 가 된다).
# 막지는 않는다 — §2.2 Unknown 처리대로 후보는 남기고 "확인 필요" 로 내보내는 게 맞다.
MAX_VERIFIED_CAPACITY = 20

# 이동수단 값 집합 (설계서 2.1 #4 렌터카·자가용·택시). **한글 라벨은 받지 않는다.**
#
# ⚠️ 이 값이 틀리면 조용히 잘못된 결과가 나온다 — `uses_car` 가 False 로 떨어지면서
# 주차 하드필터(`evaluate` 검사 3)가 통째로 꺼지고, 렌터카 여행자에게 주차 불가 숙소가
# 추천된다. 예외도 경고도 없이 후보 수만 185 → 186 으로 늘어난다. 그래서 검증한다.
#
# 폼은 한글 라벨("렌터카")을 쓰므로 **어딘가에서 변환이 필요한데 그 코드가 아직 없다.**
# 여기서 한글을 받아 주면 규약이 두 개가 되고 `restaurant/` 가 같은 걸 또 만든다.
# 변환은 폼 계층의 몫으로 두고, 여기서는 계약을 어긴 값을 즉시 거부해 공백을 드러낸다.
VEHICLES = frozenset({"car", "rental", "taxi"})


def _check_vehicle(vehicle: str | None) -> None:
    """이동수단 값 검증. `None`(미지정)은 통과."""
    if vehicle is None or vehicle in VEHICLES:
        return
    hint = ""
    if vehicle.lower() in VEHICLES:
        hint = f" — 소문자로 {vehicle.lower()!r} 를 넘길 것"
    elif vehicle in {"렌터카", "자가용", "택시"}:
        hint = " — 폼의 한글 라벨은 폼 계층에서 'rental'/'car'/'taxi' 로 바꿔 넘길 것"
    raise ValueError(
        f"vehicle 은 {sorted(VEHICLES)} 또는 None 이어야 한다 (받은 값 {vehicle!r}){hint}. "
        "값이 틀리면 주차 하드필터가 조용히 꺼진다"
    )


@dataclass
class TripContext:
    """여행 단위로 이미 정해져 있는 값들 (설계서 Pipeline 2 '1차 필수 입력').

    숙박 폼에서 다시 묻지 않고 여기서 끌어온다. `LodgingRequest.from_trip_context()`
    가 이 객체를 요청으로 옮기고, `LodgingRecommender.recommend_anchor()` 가
    `day_last_place_ids` 로 앵커를 고른다.

    `day_last_place_ids` · `day_regions` 는 **1박부터 마지막 박까지**의 값이다.
    출도일(마지막 날)은 숙박이 없으므로 들어가지 않는다 — 그래서 길이가 `nights` 다.
    """

    start_date: str                              # 'YYYY-MM-DD' 제주 도착일 = 1박의 체크인
    nights: int                                  # 박 수 (2박3일이면 2)
    guests: int                                  # 여행 인원. 설계서 2.1 에 아직 없는 입력이다
    vehicle: str | None = None                   # 'car' | 'rental' | 'taxi' | None

    # 희망권역 (설계서 2.1 #8). 노션 04-1 §2.1-1 "사용자의 희망권역에 있는 숙소만 남긴다".
    # 비우면 제주 전역이다. `from_trip_context` 가 그대로 요청으로 넘긴다.
    regions: tuple[str, ...] = ()

    # 코스 엔진 결과. 앵커 선정의 기준점이 되므로 없으면 `recommend_anchor` 가 못 돈다.
    day_last_place_ids: tuple[str, ...] = ()
    # 박별 권역. 앵커를 2곳으로 나눌지 판단할 때만 쓴다. 비우면 항상 1곳이다.
    day_regions: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        # 모든 날짜가 여기서 파생된다. 생성 시점에 안 보면 `end_date` · `night_dates()` 를
        # 부르는 **한참 뒤에** 엉뚱한 위치에서 터진다.
        try:
            date.fromisoformat(self.start_date)
        except (TypeError, ValueError) as exc:
            raise ValueError(
                f"start_date 는 'YYYY-MM-DD' 여야 한다 (받은 값 {self.start_date!r})"
            ) from exc
        if self.nights < 0:
            raise ValueError(f"nights 는 음수일 수 없다 (받은 값 {self.nights})")
        _check_vehicle(self.vehicle)
        if self.guests < 1:
            raise ValueError(f"guests 는 1 이상이어야 한다 (받은 값 {self.guests})")
        if self.guests > MAX_VERIFIED_CAPACITY:
            # 막지 않는다 — 후보는 남고 전부 '정원 확인 필요' 로 나간다(§2.2).
            log.warning(
                "인원 %d명은 TourAPI 확인 정원 최대치(%d명)를 넘는다. "
                "정원이 확인된 숙소는 전부 제외되고 미확인 숙소만 후보로 남는다.",
                self.guests,
                MAX_VERIFIED_CAPACITY,
            )
        # 길이가 어긋나면 앵커가 엉뚱한 날의 장소를 기준으로 잡힌다. 조용히 자르지 말 것.
        for name in ("day_last_place_ids", "day_regions"):
            value = getattr(self, name)
            if value and len(value) != self.nights:
                raise ValueError(
                    f"{name} 길이({len(value)})가 nights({self.nights})와 다르다. "
                    "출도일은 숙박이 없으므로 1박~마지막 박까지만 넣는다"
                )

    @property
    def end_date(self) -> str:
        """마지막 박의 체크아웃 날짜 = 출도일."""
        return _shift_date(self.start_date, self.nights)

    def night_dates(self) -> list[tuple[str, str]]:
        """박별 (체크인, 체크아웃). 1박2일이면 [('09-12', '09-13')] 한 쌍이다."""
        return [
            (_shift_date(self.start_date, i), _shift_date(self.start_date, i + 1))
            for i in range(self.nights)
        ]


@dataclass
class LodgingRequest:
    """노션 04-1 숙박 로직 §1 '사용자 입력값 수집'.

    필수 3개(인원·유형·조건)와 선택 2개(자유 입력·예산)를 그대로 옮긴 값 객체다.

    호출부가 필드를 직접 채우는 대신 `from_trip_context()` 를 쓸 것 —
    여행 문맥에서 파생되는 값(날짜·이동수단·인원·희망권역)을 코드로 고정한다.
    """

    guests: int                                  # 숙박 인원 (필수)
    lodging_type: str = ANY_TYPE                 # 숙박 유형 (필수, 기본 '상관없음')
    need_parking: bool = False                   # 원하는 조건 — 주차 가능
    need_cooking: bool = False                   # 원하는 조건 — 취사 가능
    wanted_facilities: tuple[str, ...] = ()      # 원하는 조건 — 부대시설 직접 입력
    free_text: str = ""                          # 자유 입력 (선택)
    budget_per_night: int | None = None          # 1박 예산 (선택)

    regions: tuple[str, ...] = ()                # 희망권역. 비우면 제주 전역
    vehicle: str | None = None                   # 'car' | 'rental' | 'taxi' | None
    checkin_date: str = ""                       # 'YYYY-MM-DD' — 예약 딥링크용
    checkout_date: str = ""

    def __post_init__(self) -> None:
        _check_vehicle(self.vehicle)
        # 빈 문자열은 예전부터 '유형 안 고름' 으로 동작해 왔다. 거부하지 말고 정규화한다.
        if not self.lodging_type:
            self.lodging_type = ANY_TYPE
        if self.lodging_type != ANY_TYPE and self.lodging_type not in LODGING_TYPE_MAP:
            raise ValueError(
                f"lodging_type 은 {sorted(LODGING_TYPE_MAP)} 또는 {ANY_TYPE!r} 이어야 한다 "
                f"(받은 값 {self.lodging_type!r}). 매핑에 없는 값은 후보를 0곳으로 만든다"
            )

    @classmethod
    def from_trip_context(
        cls,
        trip: TripContext,
        *,
        lodging_type: str = ANY_TYPE,
        need_cooking: bool = False,
        free_text: str = "",
        **overrides,
    ) -> "LodgingRequest":
        """여행 문맥 + 숙박 폼 3개 → 요청 객체.

        키워드 3개가 숙박 스텝에서 실제로 받는 전부다(전부 기본값이 있어 필수 입력은 0개).
        나머지는 여행 문맥에서 파생된다.

        `regions=trip.regions` — 노션 04-1 §2.1-1 "사용자의 희망권역에 있는 숙소만 남긴다"
        그대로다. 여행 폼에서 희망권역을 고르지 않았으면 비어 있고, 그때는 제주 전역이 된다.

        ⚠️ 권역을 좁히면 후보가 빠르게 준다 — 남부동 45곳에서 유형·인원까지 걸면 한 자릿수다.
        후보가 부족하면 `exclusion_summary()` 가 '권역' 을 사유로 집계해 주니, 무엇을 풀지는
        그걸 보고 안내할 것(§3.12).

        `need_parking=False` — 끄는 게 아니다. 렌터카·자가용이면 `uses_car` 가 주차 검사를
        자동으로 켠다(`evaluate` 검사 3). 그래서 폼에 주차 체크박스가 따로 필요 없다.

        `checkin_date` / `checkout_date` 는 여행 전체 구간이다. 앵커를 2곳으로 나누면
        `recommend_anchor` 가 구간별 날짜로 다시 덮어쓴다.

        `**overrides` 는 결과 화면에서 조건을 좁혀 재요청할 때만 쓸 것 —
        `from_trip_context(trip, regions=("남부동",))` 처럼.
        """
        fields = dict(
            guests=trip.guests,
            lodging_type=lodging_type,
            need_cooking=need_cooking,
            free_text=free_text,
            vehicle=trip.vehicle,
            checkin_date=trip.start_date,
            checkout_date=trip.end_date,
            regions=trip.regions,
            need_parking=False,
        )
        fields.update(overrides)
        return cls(**fields)

    @property
    def has_free_text(self) -> bool:
        return bool(self.free_text.strip())

    @property
    def uses_car(self) -> bool:
        """자가용·렌터카 이용 여부. 택시는 주차가 필요 없다."""
        return self.vehicle in {"car", "rental"}


def _merge(status_a: Verdict, status_b: Verdict) -> Verdict:
    """가장 나쁜 판정이 이긴다: EXCLUDED > NEEDS_CHECK > PASS."""
    order = {Verdict.PASS: 0, Verdict.NEEDS_CHECK: 1, Verdict.EXCLUDED: 2}
    return status_a if order[status_a] >= order[status_b] else status_b


def _verdict_of(status: Tri) -> Verdict:
    if status is Tri.YES:
        return Verdict.PASS
    if status is Tri.NO:
        return Verdict.EXCLUDED
    return Verdict.NEEDS_CHECK


def evaluate(lodging: Lodging, request: LodgingRequest) -> FilterResult:
    """숙소 1건에 대해 §2.1 기본 필터 + §2.2 Unknown 처리를 적용한다."""
    checks: list[ConditionCheck] = []
    verdict = Verdict.PASS

    # (1) 희망권역 — 좌표가 100% 있어서 Unknown 이 없다. 못 맞추면 바로 제외.
    if request.regions:
        in_region = lodging.region in request.regions
        checks.append(
            ConditionCheck(
                "권역",
                Tri.YES if in_region else Tri.NO,
                f"{lodging.region} (희망: {'·'.join(request.regions)})",
            )
        )
        if not in_region:
            verdict = _merge(verdict, Verdict.EXCLUDED)

    # (2) 숙박 유형 — small_category_name 100%. Unknown 없음.
    #
    # 기획안 §2.1-2 는 "해당 유형을 우선하거나 필터링한다" 로 두 갈래를 열어 뒀지만,
    # **필터링으로 확정했다(2026-08-28)** — 사용자가 유형을 고른 이상 다른 유형이 섞이면
    # 기대와 다르다. '우선' 쪽은 §3 등급표에 유형 자리가 없어 구현할 데도 없었다.
    if request.lodging_type and request.lodging_type != ANY_TYPE:
        matched = lodging.matches_type(request.lodging_type)
        checks.append(
            ConditionCheck(
                "숙박유형",
                Tri.YES if matched else Tri.NO,
                f"{lodging.small_category} (희망: {request.lodging_type})",
            )
        )
        if not matched:
            verdict = _merge(verdict, Verdict.EXCLUDED)

    # (3) 자동차 이용 시 주차 불가 제외 (§2.1-3).
    #     사용자가 '주차 가능' 을 직접 고른 경우(4)와 조건이 겹치므로 한 번만 검사한다.
    if request.uses_car or request.need_parking:
        detail = "주차 가능" if lodging.parking is Tri.YES else "주차 정보 없음"
        if lodging.parking is Tri.NO:
            detail = "주차 불가"
        elif lodging.parking_partial:
            detail = "주차 조건부 가능"
        checks.append(ConditionCheck("주차", lodging.parking, detail))
        verdict = _merge(verdict, _verdict_of(lodging.parking))

    # (4) 확인된 객실 최대 인원 < 사용자 인원 → 제외 (§2.1-4).
    if lodging.capacity_status is Tri.YES and lodging.max_guests is not None:
        fits = lodging.max_guests >= request.guests
        checks.append(
            ConditionCheck(
                "인원",
                Tri.YES if fits else Tri.NO,
                f"최대 {lodging.max_guests}명 (요청 {request.guests}명)",
            )
        )
        if not fits:
            verdict = _merge(verdict, Verdict.EXCLUDED)
    else:
        checks.append(ConditionCheck("인원", Tri.UNKNOWN, f"객실 정원 미확인 (요청 {request.guests}명)"))
        verdict = _merge(verdict, Verdict.NEEDS_CHECK)

    # (5) 원하는 조건 — 취사
    if request.need_cooking:
        detail = {Tri.YES: "취사 가능", Tri.NO: "취사 불가", Tri.UNKNOWN: "취사 정보 없음"}[lodging.cooking]
        if lodging.cooking is Tri.YES and lodging.cooking_partial:
            detail = "일부 객실만 취사 가능"
        checks.append(ConditionCheck("취사", lodging.cooking, detail))
        verdict = _merge(verdict, _verdict_of(lodging.cooking))

    # (6) 원하는 조건 — 부대시설 직접 입력 (복수 가능, 전부 만족해야 한다)
    for keyword in request.wanted_facilities:
        status = lodging.has_facility(keyword)
        detail = {
            Tri.YES: f"{keyword} 있음",
            Tri.NO: f"{keyword} 없음",
            Tri.UNKNOWN: f"{keyword} 확인 불가",
        }[status]
        checks.append(ConditionCheck(f"부대시설:{keyword}", status, detail))
        verdict = _merge(verdict, _verdict_of(status))

    return FilterResult(lodging=lodging, verdict=verdict, checks=checks)


def filter_candidates(
    lodgings: list[Lodging], request: LodgingRequest
) -> tuple[list[FilterResult], list[FilterResult]]:
    """전체 숙소 → (살아남은 후보, 제외된 후보).

    제외 목록도 함께 돌려주는 이유는 "왜 후보가 0개인가" 를 설명해야 하기 때문이다.
    희망권역을 좁게 잡으면 실제로 0개가 나올 수 있다(남부동 45곳에서 유형·인원까지 걸면 한 자릿수).
    """
    kept: list[FilterResult] = []
    dropped: list[FilterResult] = []
    for lodging in lodgings:
        result = evaluate(lodging, request)
        (dropped if result.verdict is Verdict.EXCLUDED else kept).append(result)
    return kept, dropped


def exclusion_summary(dropped: list[FilterResult]) -> dict[str, int]:
    """제외된 숙소들이 **어떤 조건에** 걸렸는지 센다. 많이 걸린 순으로 돌려준다.

    한 숙소가 여러 조건에 걸리면 **전부** 센다. 그래서 합계가 `len(dropped)` 보다 클 수 있다.
    그게 요점이다 — 첫 사유(`excluded_reason`)만 세면 실제 원인이 가려진다.

    4인·렌터카·펜션민박·취사 요청에서 138곳이 제외될 때 실측이 이렇다.

        첫 사유만 세면   숙박유형 132 · 인원  5 · 취사  1
        전부 세면        숙박유형 132 · 인원 24 · 취사 51 · 주차 1
                                            └─ 취사가 51곳을 죽였는데 1곳으로만 보인다

    "후보가 부족하니 무엇을 풀어야 하나" 를 안내할 때 이 값을 쓸 것.
    """
    counts: dict[str, int] = {}
    for result in dropped:
        for name in result.excluded_reasons:
            counts[name] = counts.get(name, 0) + 1
    return dict(sorted(counts.items(), key=lambda kv: (-kv[1], kv[0])))
