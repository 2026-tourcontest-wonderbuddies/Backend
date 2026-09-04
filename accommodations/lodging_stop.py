"""숙소 타임라인 배치 — 하루의 어디에 숙소가 들어가는지를 계산한다.

숙소는 하루의 **종점이면서 동시에 경유지**다. AI Hub 제주 실측(숙박 방문 4,645건):

    그날 첫 숙소 방문 뒤에도 다른 장소를 더 간다     37.8%
    같은 날 숙소를 두 번 이상 들른다                38.5%
    그날 **마지막** 숙소 방문 뒤에 더 간다           1.2%   ← 하루가 숙소에서 끝난다는 전제는 맞다

그래서 stop 을 **세 역할**로 나눈다. 두 개(경유·종점)로는 부족한데, 앵커가 여행 전체로
고정되면서(`recommend.recommend_anchor`) 2일차 아침의 **출발점**이 숙소가 되기 때문이다.
`depart` 를 빼면 2일차 첫 이동의 출발지가 없어 타임라인이 끊긴다.

    depart      0분        하루의 출발점. 잠은 전날 밤 슬롯 소속이라 시간을 안 먹는다
    checkin     30/60분    짐 놓고 재출발 — 그 숙소에 처음 묵는 날 하나만의 앵커
    overnight   None       그날의 끝. '0분' 이 아니다 — 다음 날 depart 까지가 이 stop 의 실체다

`checkin` 은 이미 묵고 있는 숙소에 낮에 다시 들르는 경우(휴식·샤워)는 다루지 않는다 —
2026-08-29 로직 단순화로 뺐다. `_checkin_stop()` 참조.

⚠️ 이 모듈은 **어디에 넣을지만** 정한다. 실제 슬롯 삽입은 Pipeline 3 의 몫이다.
그래서 `overnight.at` 은 비어 있다 — 그날 마지막 장소가 언제 끝나는지는 코스가 안다.
"""

from __future__ import annotations

from dataclasses import dataclass

from lodging_data import Lodging, parse_check_time
from lodging_filter import TripContext

# 설계서 3.1 기본 타임라인 앵커.
DAY_START = "09:00"

# 경유 stop 을 넣을지 가르는 경계. 실측에서 "첫 숙소 도착 시각 → 그 뒤에도 여행을 잇는 비율"
# 이 여기서 꺾인다: 17시 69.7% │ 18시 48.9% · 19시 25.8% · 20시 13.5% · 21시 5.7%.
# 무조건 넣으면 62.2%(= 100 - 37.8) 의 사용자에게 불필요한 정차가 생긴다.
CHECKIN_CUTOFF = "18:00"

# 경유 stop 체류시간. 실측 중앙값은 60분이지만 휴식·샤워까지 포함된 값이라,
# 짐만 놓는 기본값은 30분으로 두고 '여유로운 코스' 일 때만 실측 중앙값을 쓴다.
# (25% 30분 · 50% 60분 · 75% 120분). 둘 다 5분 격자에 떨어진다.
CHECKIN_STOP_MIN = 30
CHECKIN_STOP_RELAXED_MIN = 60

# 파싱 실패·결측 시 폴백. 212건 중 입실 15:00 이 148곳(70%), 퇴실 11:00 이 174곳(83%)이라
# 이 값으로 대체해도 대부분 맞는다. 컬럼 자체가 없는 숙소는 3곳뿐이다(보유율 98.6%).
DEFAULT_CHECK_IN = "15:00"
DEFAULT_CHECK_OUT = "11:00"


@dataclass
class LodgingStop:
    """타임라인에 실리는 숙소 접점 1개."""

    kind: str                         # 'depart' | 'checkin' | 'overnight'
    lodging_id: str                   # 같은 앵커를 가리키면 UI 에서 '같은 숙소' 로 묶인다
    day_index: int                    # 0-based. 0 = 1일차
    at: str = ""                      # 'HH:MM'. overnight 은 비운다 — Pipeline 3 이 정한다
    duration_min: int | None = None   # depart=0 · checkin=30/60 · overnight=None(하루 종료까지)
    note: str = ""                    # '짐 보관 문의 필요' · '체크아웃 11:00' 등

    def to_dict(self) -> dict:
        return {
            "kind": self.kind,
            "lodging_id": self.lodging_id,
            "day_index": self.day_index,
            "at": self.at,
            "duration_min": self.duration_min,
            "note": self.note,
        }


def check_in_of(lodging: Lodging) -> str:
    """숙소의 입실 시각. 못 읽으면 15:00."""
    return parse_check_time(lodging.check_in_time) or DEFAULT_CHECK_IN


def check_out_of(lodging: Lodging) -> str:
    """숙소의 퇴실 시각. 못 읽으면 11:00."""
    return parse_check_time(lodging.check_out_time) or DEFAULT_CHECK_OUT


def plan_stops(
    trip: TripContext,
    lodging_by_night: list[Lodging],
    reach_times: list[str],
    *,
    relaxed: bool = False,
    show_checkin: bool = True,
    day_start: str = DAY_START,
) -> list[list[LodgingStop]]:
    """여행 전체의 숙소 stop 을 **일차별로** 계산한다. 길이는 `nights + 1`(출도일 포함).

    `lodging_by_night` — 박별로 그날 밤 묵는 숙소. 앵커가 1곳이면 같은 객체가 반복된다.
    `reach_times`      — 박별로 **그날 숙소에 처음 닿을 수 있는 시각** `'HH:MM'`.
                         코스를 아는 Pipeline 3 이 계산해 넘긴다. 18시 경계 판정의 입력이다.

    `checkin` 은 **그 숙소에 처음 묵는 날**에만 넣는다. 1일차뿐 아니라(앵커 분할이 재활성화되면)
    숙소가 바뀌는 날도 해당한다 — 그날도 새 숙소에 짐을 처음 푸는 날이기 때문이다.

    `show_checkin=False` 를 주면 조건을 만족해도 아예 안 넣는다 — **결과 화면 토글**이지
    입력 폼 항목이 아니다. 그 시점엔 사용자도 그날 코스가 어떻게 짜였는지 몰라 판단할
    근거가 없다(§L0 날짜와 같은 이유). 18시 컷오프 자체는 끌 수 없다 — 그건 사용자
    선택이 아니라 "그 시각에 닿을 수 있다"는 사실이다.

    ⚠️ **이미 묵고 있는 숙소에 낮에 다시 들르는 경우(휴식·샤워, 실측 35.9%)는 지원하지
    않는다.** 한때 `mid_stay_return` 토글로 받았는데, 로직 복잡도를 늘릴 값어치가 없다고
    판단해 2026-08-29 뺐다 — 체크인 시각은 **그 숙소에 처음 묵는 날 하나만의 앵커**로 쓴다
    (README §4 "중간일 복귀 토글" 참조). 필요해지면 이 함수에 매개변수를 다시 추가할 것.
    """
    nights = trip.nights
    if nights <= 0:
        return []
    if len(lodging_by_night) != nights:
        raise ValueError(
            f"lodging_by_night 길이({len(lodging_by_night)})가 nights({nights})와 다르다"
        )
    if len(reach_times) != nights:
        raise ValueError(f"reach_times 길이({len(reach_times)})가 nights({nights})와 다르다")

    stop_min = CHECKIN_STOP_RELAXED_MIN if relaxed else CHECKIN_STOP_MIN
    days: list[list[LodgingStop]] = [[] for _ in range(nights + 1)]

    for night in range(nights):
        lodging = lodging_by_night[night]
        previous = lodging_by_night[night - 1] if night > 0 else None
        first_night_here = previous is None or previous.content_id != lodging.content_id

        # (1) depart — 전날 밤 숙소에서 그날을 시작한다. 0분: 잠은 전날 슬롯 소속이다.
        if previous is not None:
            days[night].append(_depart_stop(previous, lodging, night, day_start))

        # (2) checkin — 그 숙소에 처음 묵는 날이고, 18시 전에 닿을 수 있고, 사용자가
        # 끄지 않았을 때만. show_checkin 은 결과 화면 토글이지 18시 컷오프를 대신하지 않는다.
        reach = reach_times[night]
        if first_night_here and show_checkin and reach < CHECKIN_CUTOFF:
            days[night].append(_checkin_stop(lodging, night, reach, stop_min))

        # (3) overnight — 그날의 끝. 시각은 Pipeline 3 이 코스 종료 시점으로 채운다.
        days[night].append(
            LodgingStop(kind="overnight", lodging_id=lodging.content_id, day_index=night)
        )

    # 출도일 — 숙박은 없고 마지막 밤 숙소에서 나가는 것만 남는다.
    days[nights].append(_depart_stop(lodging_by_night[-1], None, nights, day_start))
    return days


def _depart_stop(
    previous: Lodging, current: Lodging | None, day_index: int, day_start: str
) -> LodgingStop:
    """전날 밤 숙소에서 출발. 그날이 그 숙소의 **마지막 아침**이면 퇴실 제약을 단다."""
    leaving = current is None or current.content_id != previous.content_id
    note = ""
    if leaving:
        check_out = check_out_of(previous)
        note = f"체크아웃 {check_out} — 짐 회수 필요"
        if day_start > check_out:
            # 09:00 출발이 기본이라 보통은 안 걸리지만, 늦게 시작하는 날은 순서가 뒤집힌다.
            note = f"체크아웃 {check_out} 이후 출발 — 짐을 먼저 빼야 한다"
    return LodgingStop(
        kind="depart",
        lodging_id=previous.content_id,
        day_index=day_index,
        at=day_start,
        duration_min=0,
        note=note,
    )


def _checkin_stop(lodging: Lodging, day_index: int, reach: str, stop_min: int) -> LodgingStop:
    """짐 놓고 재출발. 그 숙소에 처음 묵는 날에만 불린다 — `plan_stops()` 참조.

    입실 시각보다 일찍 닿으면 얼리 체크인이 보장되지 않는다. 그럴 때도 stop 은 넣는다 —
    기다렸다 15시에 들어가는 게 아니라 짐만 맡기고 나가는 동선이 실제이기 때문이다
    (실측: 12~14시 도착도 86~91% 가 여행을 잇는다). 보장되지 않는다는 사실만 §2.2
    Unknown 처리와 같은 방식으로 카드에 남긴다.
    """
    check_in = check_in_of(lodging)
    if reach < check_in:
        note = f"체크인 {check_in}부터 — 짐 보관 가능 여부 문의 필요"
    else:
        note = f"체크인 {check_in}"
    return LodgingStop(
        kind="checkin",
        lodging_id=lodging.content_id,
        day_index=day_index,
        at=reach,
        duration_min=stop_min,
        note=note,
    )


def render_stops(days: list[list[LodgingStop]], titles: dict[str, str] | None = None) -> str:
    """디버깅용. `titles` 를 주면 숙소 이름으로 찍는다."""
    titles = titles or {}
    label = {"depart": "출발", "checkin": "체크인·짐", "overnight": "1박"}
    lines = []
    for index, stops in enumerate(days):
        lines.append(f"[{index + 1}일차]")
        for stop in stops:
            name = titles.get(stop.lodging_id, stop.lodging_id)
            when = stop.at or "코스 종료 시점"
            dur = "" if stop.duration_min is None else f" {stop.duration_min}분"
            note = f"  · {stop.note}" if stop.note else ""
            lines.append(f"  {when:>13}  {label[stop.kind]:<8}{name}{dur}{note}")
    return "\n".join(lines)


__all__ = [
    "CHECKIN_CUTOFF",
    "CHECKIN_STOP_MIN",
    "CHECKIN_STOP_RELAXED_MIN",
    "DAY_START",
    "DEFAULT_CHECK_IN",
    "DEFAULT_CHECK_OUT",
    "LodgingStop",
    "check_in_of",
    "check_out_of",
    "plan_stops",
    "render_stops",
]
