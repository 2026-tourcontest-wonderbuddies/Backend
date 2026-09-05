"""
숙박 데이터 로더 — TourAPI `숙박.csv` 212건을 추천 로직이 바로 쓸 수 있는 형태로 정규화한다.

이 모듈이 하는 일은 **파싱과 정규화뿐**이다. 필터링·점수·순위는 각각
`lodging_filter.py` · `query_fit.py` · `recommend.py` 가 맡는다.

핵심 설계 — `Unknown` 을 조건 불충족과 절대 섞지 않는다.
TourAPI 숙박 컬럼은 결측률이 컬럼마다 크게 다르고(주차 94.8% ↔ 반려동물 0.9%),
"불가능" 과 "값이 없음" 을 같이 처리하면 확인만 안 된 멀쩡한 숙소가 통째로 사라진다.
그래서 모든 조건성 컬럼은 bool 이 아니라 3값(`Tri.YES/NO/UNKNOWN`)으로 읽는다.

⚠️ 숫자 0 은 결측이다. `room_capacity_summary` 의 "기준 0, 최대 0" 은 정원이 0명이라는
뜻이 아니라 TourAPI 가 객실 정보를 못 채운 것이다(C&P리조트 등). 0 을 그대로 믿으면
"최대 인원 0 < 사용자 인원" 이 되어 전량 제외된다 → `Tri.UNKNOWN` 으로 되돌린다.
"""

from __future__ import annotations

import logging
import re
from dataclasses import dataclass, field
from enum import Enum
from pathlib import Path

import pandas as pd

from accommodations.booking_link import load_hotel_map

log = logging.getLogger(__name__)

PROJECT_DIR = Path(__file__).resolve().parent.parent
LODGING_CSV = PROJECT_DIR / "TourAPI" / "lodging.csv"

# TourAPI 가 값을 못 채웠을 때 넣는 토큰들. 빈 문자열/NaN 과 같이 취급한다.
_UNKNOWN_TOKENS = {"", "unknown", "nan", "none", "-"}


class Tri(str, Enum):
    """3값 논리. `UNKNOWN` 은 '조건 불충족' 이 아니라 '확인할 수 없음' 이다."""

    YES = "yes"
    NO = "no"
    UNKNOWN = "unknown"


# --- 권역 ---------------------------------------------------------------
#
# EDA §3.4 의 4분면(한라산 기준 북/남 × 동/서) 정의를 그대로 쓴다.
# 분할 기준점은 한라산 정상 좌표다. 코드로 권역을 정의한 곳이 프로젝트에 아직 없어서
# 여기서 처음 상수화한다 — 코스 엔진 쪽에 권역 모듈이 생기면 그쪽으로 옮기고
# 이 파일은 import 만 하도록 바꿀 것.
HALLASAN_LON = 126.5292
HALLASAN_LAT = 33.3617

REGIONS = ("북부서", "북부동", "남부서", "남부동")


def region_of(lon: float, lat: float) -> str:
    """좌표 → 권역명. EDA §3.4 4분면."""
    ns = "북부" if lat >= HALLASAN_LAT else "남부"
    ew = "동" if lon >= HALLASAN_LON else "서"
    return ns + ew


# --- 숙박 유형 ------------------------------------------------------------
#
# 사용자 선택지(노션 04-1 "숙박 유형") → TourAPI `small_category_name` 매핑.
# 실제 분포: 호텔 72 · 펜션 69 · 게스트하우스 19 · 콘도 18 · 리조트 14 · 모텔 9 ·
#            농어촌민박 5 · 유스호스텔 3 · 마을관광지 2 · 한옥스테이 1
#
# ※ 모텔(9) · 마을관광지(2) 는 어느 선택지에도 넣지 않았다. 사용자가 "호텔" 을 골랐을 때
#   모텔이 섞여 나오는 건 기대와 다르고, 마을관광지는 숙박 유형이라 보기 어렵다.
#   이 11곳은 "상관없음" 을 골랐을 때만 후보에 들어온다. 정책이 바뀌면 여기만 고치면 된다.
LODGING_TYPE_MAP: dict[str, tuple[str, ...]] = {
    "호텔": ("호텔",),
    "리조트·콘도": ("리조트", "콘도"),
    "펜션·민박": ("펜션", "농어촌민박", "한옥스테이"),
    "게스트하우스": ("게스트하우스", "유스호스텔"),
}
ANY_TYPE = "상관없음"


# --- 원시 문자열 파서 -------------------------------------------------------


def _clean(value) -> str:
    """NaN·Unknown 토큰을 빈 문자열로 눌러 준다."""
    if value is None or (isinstance(value, float) and pd.isna(value)):
        return ""
    text = str(value).strip()
    return "" if text.lower() in _UNKNOWN_TOKENS else text


# 원본이 잘려 들어온 값. 늘리기 전에 반드시 부분일치 함정을 확인할 것 — 이 집합은
# **정확일치**로만 쓰이고, 그게 이 값들을 여기에 따로 둔 이유다.
_TRUNCATED_YES = frozenset({"능"})


def parse_yes_no(raw, *, field_name: str = "") -> tuple[Tri, bool]:
    """'가능' / '불가능' 계열 컬럼을 3값으로 읽는다. 반환: (판정, 부분충족 여부).

    부분충족은 '가능(일부 객실)' · '콘도형 객실만 가능' 처럼 조건부인 경우다.
    필터에서는 YES 로 통과시키되 카드에 단서를 남긴다.

    '불가' 검사를 먼저 해야 한다 — '불가능' 안에 '가능' 이 들어 있다.
    """
    text = _clean(raw)
    if not text:
        return Tri.UNKNOWN, False
    if "불가" in text:
        return Tri.NO, False
    # '없음' · '미제공' 은 **정확일치로만** NO 로 본다. 부분일치로 넓히면 "주차 제한 없음"
    # 처럼 없음이 오히려 가능을 뜻하는 문구가 뒤집힌다.
    if text in {"없음", "미제공"}:
        return Tri.NO, False
    # TourAPI 원본에 잘려 들어온 값(`parking` 1건). 부분일치 토큰으로 '능' 을 넣으면
    # "취사 기능 없음" 같은 문구가 '기능' 의 능에 걸려 YES 로 뒤집히므로 — 위 '없음' 은
    # 정확일치라 막아 주지 못한다 — 정확일치로만 되살린다.
    if text in _TRUNCATED_YES:
        return Tri.YES, False
    partial = any(token in text for token in ("일부", "객실만", "만 가능"))
    if any(token in text for token in ("가능", "있음", "무료", "유료")):
        return Tri.YES, partial
    log.debug("해석 못 한 %s 값: %r → UNKNOWN", field_name or "값", text)
    return Tri.UNKNOWN, False


_MAX_CAPACITY_RE = re.compile(r"최대\s*(\d+)")


def parse_room_capacity(raw) -> tuple[Tri, int | None]:
    """`room_capacity_summary` → (판정, 확인된 최대 수용 인원).

    형식: '35평 A: 기준 4, 최대 6 | Standard: 기준 2, 최대 2 | ...'
    객실별 '최대' 중 가장 큰 값을 그 숙소가 받을 수 있는 인원으로 본다.
    전부 0 이면 값이 안 채워진 것이므로 UNKNOWN.
    """
    text = _clean(raw)
    if not text:
        return Tri.UNKNOWN, None
    values = [int(m) for m in _MAX_CAPACITY_RE.findall(text)]
    values = [v for v in values if v > 0]
    if not values:
        return Tri.UNKNOWN, None
    return Tri.YES, max(values)


def parse_facilities(raw) -> tuple[Tri, set[str]]:
    """`facilities` → (판정, 보유 시설 이름 집합).

    형식: '바비큐장: 1 / 사우나: 0 / 기타 부대시설: 제페토 공방, 세미나실'
    `이름: 1` 은 보유, `이름: 0` 은 미보유, `기타 부대시설` 은 쉼표로 나열된 자유 텍스트다.
    """
    text = _clean(raw)
    if not text:
        return Tri.UNKNOWN, set()

    have: set[str] = set()
    for chunk in text.split(" / "):
        if ":" not in chunk:
            if chunk.strip():
                have.add(chunk.strip())
            continue
        name, _, value = chunk.partition(":")
        name, value = name.strip(), value.strip()
        if value in {"0", ""}:
            continue
        if value == "1":
            have.add(name)
        else:  # 기타 부대시설처럼 값 자리에 자유 텍스트가 오는 경우
            have.update(part.strip() for part in value.split(",") if part.strip())
    return Tri.YES, have


_INT_RE = re.compile(r"(\d+)")


def parse_int(raw) -> int | None:
    """'10실' · '70,000' 처럼 단위/구분자가 붙은 정수를 뽑는다."""
    text = _clean(raw).replace(",", "")
    if not text:
        return None
    m = _INT_RE.search(text)
    return int(m.group(1)) if m else None


# 'HH:MM' 또는 'H:MM'. 앞에 붙은 오전/오후를 같이 잡아 12시간제를 되돌린다.
_CLOCK_RE = re.compile(r"(오전|오후)?\s*(\d{1,2})\s*:\s*(\d{2})")


def parse_check_time(raw) -> str | None:
    """TourAPI 입실/퇴실 시각 → `'HH:MM'`. 못 읽으면 `None`.

    이 컬럼은 정해진 형식이 없다. 212건 실측에서 나온 것만 해도 이렇다.

        15:00                                  단순 시각 (입실 148곳 · 퇴실 174곳)
        16:00~22:00                            구간
        14:00 이후~15:00 이전에                 문장
        오후 4:00 이후                          12시간제
        15:00~23:59 (레이트체크인 마감 23:59)    괄호 주석

    **맨 앞 시각만** 취한다. 구간이든 문장이든 "이때부터 가능" 이 앞에 오기 때문이고,
    타임라인이 알아야 하는 것도 그 하한이다. 못 읽은 값은 호출부가 기본값
    (입실 15:00 · 퇴실 11:00)으로 대체한다 — 그 폴백이 각각 70%·83% 를 덮는다.
    """
    text = _clean(raw)
    if not text:
        return None
    m = _CLOCK_RE.search(text)
    if not m:
        return None

    meridiem, hour_s, minute_s = m.groups()
    hour, minute = int(hour_s), int(minute_s)
    if meridiem == "오후" and hour < 12:
        hour += 12
    elif meridiem == "오전" and hour == 12:
        hour = 0
    # 24:00 은 자정 표기다. 그 위는 오독이므로 버린다.
    if hour == 24 and minute == 0:
        hour = 0
    if not (0 <= hour <= 23 and 0 <= minute <= 59):
        return None
    return f"{hour:02d}:{minute:02d}"


# --- 레코드 --------------------------------------------------------------

# `search_text` 구성 컬럼 (노션 04-1 "숙소별 search_text" 정의 그대로).
SEARCH_TEXT_COLUMNS = (
    "title",
    "address",
    "overview",
    "small_category_name",
    "room_type",
    "parking",
    "cooking",
    "food_place",
    "facilities",
    "room_capacity_summary",
    "room_options",
    "pet_allowed_type",
    "pet_allowed_animals",
    "pet_requirements",
    "pet_extra_info",
)


@dataclass
class Lodging:
    """정규화된 숙소 1건."""

    content_id: str
    title: str
    address: str
    lon: float
    lat: float
    region: str
    signgu_code: str
    small_category: str

    # `content_id → tripcom_hotel_id` 오프라인 매칭 결과. `data/tripcom_hotel_map.csv`
    # ("lodging_anchor" 테이블)에서 병합해 넣는다. 매칭이 없으면 빈 문자열 — Tier2 폴백은
    # `booking_link.BookingLinkBuilder` 가 그대로 처리한다.
    tripcom_hotel_id: str = ""

    # 조건 판정용 3값
    parking: Tri = Tri.UNKNOWN
    parking_partial: bool = False
    cooking: Tri = Tri.UNKNOWN
    cooking_partial: bool = False
    capacity_status: Tri = Tri.UNKNOWN
    max_guests: int | None = None
    facilities_status: Tri = Tri.UNKNOWN
    facilities: set[str] = field(default_factory=set)

    # 안내 정보 (필터에 쓰지 않는다)
    check_in_time: str = ""
    check_out_time: str = ""
    room_count: int | None = None
    room_type: str = ""
    room_options: str = ""
    min_room_price: int | None = None
    overview: str = ""
    homepage: str = ""
    contact: str = ""

    search_text: str = ""

    def matches_type(self, user_type: str) -> bool:
        """사용자가 고른 숙박 유형에 이 숙소가 속하는가."""
        if not user_type or user_type == ANY_TYPE:
            return True
        return self.small_category in LODGING_TYPE_MAP.get(user_type, ())

    def has_facility(self, keyword: str) -> Tri:
        """부대시설 키워드 보유 여부.

        `facilities` 집합에 없으면 `room_options`(객실 옵션) 텍스트까지 본다 —
        수영장·바비큐장은 부대시설에, 스파욕조·취사용품은 객실 옵션에 적히는 경우가 섞여 있다.
        """
        key = keyword.strip()
        if not key:
            return Tri.UNKNOWN
        if any(key in name for name in self.facilities):
            return Tri.YES
        if key in self.room_options or key in self.overview:
            return Tri.YES
        # 시설 목록 자체가 없으면 '없다' 고 말할 근거가 없다.
        return Tri.NO if self.facilities_status is Tri.YES else Tri.UNKNOWN


def _build_search_text(row: pd.Series) -> str:
    parts = [_clean(row.get(col)) for col in SEARCH_TEXT_COLUMNS]
    return " ".join(p for p in parts if p)


def load_lodgings(
    csv_path: Path | str = LODGING_CSV,
    hotel_map: dict[str, str] | None = None,
) -> list[Lodging]:
    """TourAPI 숙박 CSV → `Lodging` 리스트.

    좌표가 없는 행은 권역 판정과 이동시간 계산이 모두 불가능하므로 제외하고 경고를 남긴다
    (현행 수집분 212건은 좌표 100% — 제외 대상 0건).

    `hotel_map` 을 생략하면 `booking_link.load_hotel_map()` 으로 `content_id → tripcom_hotel_id`
    오프라인 매칭 결과를 읽어 병합한다(매칭 파일이 없으면 전량 빈 문자열).
    """
    df = pd.read_csv(csv_path, encoding="utf-8-sig", dtype=str, keep_default_na=False)
    if hotel_map is None:
        hotel_map = load_hotel_map()

    lodgings: list[Lodging] = []
    dropped = 0
    for _, row in df.iterrows():
        try:
            lon, lat = float(row["longitude"]), float(row["latitude"])
        except (TypeError, ValueError):
            dropped += 1
            continue

        parking, parking_partial = parse_yes_no(row.get("parking"), field_name="parking")
        cooking, cooking_partial = parse_yes_no(row.get("cooking"), field_name="cooking")
        capacity_status, max_guests = parse_room_capacity(row.get("room_capacity_summary"))
        facilities_status, facilities = parse_facilities(row.get("facilities"))
        content_id = str(row["content_id"]).strip()

        lodgings.append(
            Lodging(
                content_id=content_id,
                title=_clean(row.get("title")),
                address=_clean(row.get("address")),
                lon=lon,
                lat=lat,
                region=region_of(lon, lat),
                signgu_code=_clean(row.get("signgu_code")),
                small_category=_clean(row.get("small_category_name")),
                tripcom_hotel_id=hotel_map.get(content_id, ""),
                parking=parking,
                parking_partial=parking_partial,
                cooking=cooking,
                cooking_partial=cooking_partial,
                capacity_status=capacity_status,
                max_guests=max_guests,
                facilities_status=facilities_status,
                facilities=facilities,
                check_in_time=_clean(row.get("check_in_time")),
                check_out_time=_clean(row.get("check_out_time")),
                room_count=parse_int(row.get("room_count")),
                room_type=_clean(row.get("room_type")),
                room_options=_clean(row.get("room_options")),
                min_room_price=parse_int(row.get("min_room_price")),
                overview=_clean(row.get("overview")),
                homepage=_clean(row.get("homepage")),
                contact=_clean(row.get("contact")),
                search_text=_build_search_text(row),
            )
        )

    if dropped:
        log.warning("좌표 결측으로 제외한 숙소 %d건", dropped)
    return lodgings
