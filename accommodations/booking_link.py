"""
예약 연결 — 노션 04-1 숙박 로직 §4 [신규] (08.20 현정 조사 + 로직 반영안).

원칙: **TourAPI 숙박 데이터가 필터링·정렬의 기준(core)** 이고, Trip.com 은 오직
"예약하기" CTA(외부 딥링크)로만 결합한다. 실시간 가격·재고 API 연동은 하지 않는다.

2단계(Tier) 구조:
  Tier 1  개별 호텔 **상세 페이지** — `tourapi_content_id ↔ tripcom_hotel_id` 매핑이 있을 때
  Tier 2  제주 도시 검색 딥링크 — 매칭 실패/미시도 시 폴백

⚠️ 도시ID 737 은 "제주시" 가 아니라 **제주 전역**이다 (2026-08-22 확인 — 노션 조사가 틀렸다).
   그 아래 하위 zone 10개를 수집해 `REGION_ZONE_IDS` 로 권역별 목록까지 좁힌다. 다만 권역
   하나가 zone 하나보다 넓은 **손실 매핑**이라, 개별 숙소로 보내려면 여전히 Tier 1 이 필요하다.

⚠️ Allianceid / SID 는 코드·노션에 평문으로 두지 않는다. 환경변수 또는 프로젝트 루트 `.env`
   에서만 읽고, 없으면 **비제휴 폴백 링크**(트래킹 파라미터 없는 공개 검색 URL)를 만든다.
   기능은 그대로 동작하고 커미션·트래킹만 빠지므로 심사 통과 후 값만 넣으면 된다.

2026-08-22 실측 확인 (이 URL 을 브라우저로 직접 열어 대조):
  · `TIER1_URL` 이 **호텔 상세 페이지**로 리다이렉트 없이 열린다 — 그라벨 호텔(1979830) ·
    2026-09-12/13 · adult=2 → 한국어 상세 화면에 객실("클럽 라운지룸")과 요금(126,429원)이
    바로 나온다. 날짜를 빼면 같은 URL 이 기본 날짜로 열리므로 날짜는 반드시 같이 보낸다.
  · `TIER2_URL` 패턴이 리다이렉트 없이 그대로 열린다.
  · `checkin` · `checkout` · `adult` 이 **검색결과에 반영된다** — 노션 체크리스트 미검증 항목 해소.
    2026-09-12/13 · adult=4 → 페이지에 "9월 12일" · "9월 13일" · "성인 4" 로 표시됨.
  · Link Builder 가 만들어 주는 정식 형태는 쿼리형(`/hotels/list?city=737&optionId=737&
    optionType=City&optionName=제주`)이고, 위 경로형과 목적지가 같다. 둘 다 유효하다.
"""

from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path
from urllib.parse import urlencode

PROJECT_DIR = Path(__file__).resolve().parent.parent
ENV_FILE = PROJECT_DIR / ".env"
DATA_DIR = Path(__file__).resolve().parent / "data"

# `content_id,tripcom_hotel_id` 두 컬럼짜리 CSV. 없으면 전량 Tier 2 로 동작한다.
HOTEL_MAP_PATH = DATA_DIR / "tripcom_hotel_map.csv"

# Tier 1 은 **호텔 상세 페이지**로 직접 보낸다. `/hotels/redirect?hotelid=…` 도 목적지를
# 그 호텔로 좁혀 주긴 하지만 착지점이 "결과 1건짜리 검색 목록" 이라, 사용자가 카드를 한 번
# 더 눌러야 객실·요금이 나온다. `/hotels/detail/` 은 그 한 단계를 없앤다 (2026-08-22 §4.6).
TIER1_URL = "https://kr.trip.com/hotels/detail/"

# 상세 페이지 파라미터는 Trip 자체 UI 가 내보내는 카멜케이스를 따른다. 실측으로는 소문자
# (`checkin`)도 동작했지만, 우리가 통제하지 못하는 외부 계약이라 사이트가 실제로 쓰는
# 표기를 쓴다. Tier 2 는 소문자 그대로다 — 두 티어의 표기가 다른 건 의도된 것이다.
TIER1_STAY_PARAMS = {"checkin": "checkIn", "checkout": "checkOut", "adult": "adult"}
TIER2_URL = "https://kr.trip.com/hotels/jeju-hotels-list-{city_id}/"
TIER2_ZONE_URL = "https://kr.trip.com/hotels/jeju-hotels-list-{city_id}/zone{zone_id}/"

# 도시ID 737. 노션 조사에는 "제주시" 로 적혀 있으나 **실제로는 제주 전역**이다
# (2026-08-22 확인 — 737 아래에 중문·서귀포 시내·성산 zone 이 전부 달려 있다).
DEFAULT_CITY_ID = "737"

# 착지 화면 기본 통화. apidoc.trip.com 에 `curr` 가 "partner's site 에서 고른 통화를
# landing page 통화로 정한다" 로 문서화되어 있어 항상 붙인다(제휴 여부·Tier 무관).
# 2026-08-22 실측: curr=KRW 는 적용됨.
#
# ⚠️ 언어(locale/lang)는 `/hotels/redirect` 에 문서화된 파라미터가 없다. 2026-08-22
# 실측: 같은 hotelid 를 kr.trip.com · kr.trip.com 양쪽 도메인으로 열어봐도 (둘 다
# 개별 호텔 페이지로는 정상 도달) 언어는 둘 다 한국어로 안 바뀌었다 — **도메인도
# 언어를 결정하지 않는다.** apidoc 의 "Trip 이 BookURL 을 자체 조정하며 파트너가
# 언어 관계를 하드코딩할 필요 없다" 는 문구와 일치 — 언어는 방문자 브라우저/쿠키/IP
# 기준으로 Trip 서버가 정하는 것으로 보이고, 우리 쪽 URL 로는 제어 불가능해 보인다.
# 그 뒤 `/hotels/detail/` 로 옮기면서 `locale=ko-KR` 을 붙였고, 2026-08-22 재실측에서는
# kr 도메인 + locale 조합으로 착지 화면이 한국어로 나왔다. 위 "언어 제어 불가" 관찰은
# `/hotels/redirect` 기준이었다는 뜻이다 — 엔드포인트를 되돌리면 이 문제도 같이 돌아온다.
DEFAULT_CURRENCY = "KRW"

# 737 아래 하위 구역(zone). 2026-08-22 공개 페이지에서 수집한 10개:
#   763 제주국제공항/제주시 · 81308475 제주국제공항/연동 · 81308477 동문시장/제주시청
#   15037 애월 · 14662 한림공원/협재 · 15038 함덕/조천/구좌
#   1206 성산일출봉/표선면 · 1205 중문관광단지 · 764 서귀포 시내 · 11919 한라산 국립공원
#
# 권역(EDA §3.4 4분면) → zone 매핑. 권역 하나가 zone 하나보다 넓으므로 **손실 매핑**이다.
# 각 권역의 숙박 밀집지를 대표하는 zone 을 골랐고, 경계에 걸치는 1206(성산=북부동 +
# 표선=남부동)은 어느 쪽에도 쓰지 않았다. 매핑이 없는 권역은 제주 전역(737)으로 폴백한다.
REGION_ZONE_IDS: dict[str, str] = {
    "북부서": "763",    # 제주국제공항/제주시
    "북부동": "15038",  # 함덕/조천/구좌
    "남부서": "1205",   # 중문관광단지
    "남부동": "764",    # 서귀포 시내
}

# "인자 생략"과 "명시적 None"을 구분하기 위한 센티널. `BookingLinkBuilder.__init__` 참조.
_UNSET: str = object()  # type: ignore[assignment]


def _load_env(name: str) -> str | None:
    """환경변수 우선, 없으면 프로젝트 루트 `.env`. `hybrid_engine.load_api_key()` 와 같은 방식."""
    value = os.environ.get(name)
    if value:
        return value.strip()
    if ENV_FILE.exists():
        for line in ENV_FILE.read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if line.startswith("#") or "=" not in line:
                continue
            key, _, raw = line.partition("=")
            if key.strip() == name:
                return raw.strip().strip("'\"") or None
    return None


def load_hotel_map(path: Path | str = HOTEL_MAP_PATH) -> dict[str, str]:
    """`tourapi_content_id → tripcom_hotel_id` 매핑. 파일이 없으면 빈 dict."""
    path = Path(path)
    if not path.exists():
        return {}
    import csv

    mapping: dict[str, str] = {}
    with path.open(encoding="utf-8-sig", newline="") as fh:
        for row in csv.DictReader(fh):
            content_id = (row.get("content_id") or "").strip()
            hotel_id = (row.get("tripcom_hotel_id") or "").strip()
            if content_id and hotel_id:
                mapping[content_id] = hotel_id
    return mapping


@dataclass
class BookingLink:
    """숙소 카드의 '예약하기' 버튼에 실리는 값."""

    url: str
    link_type: str          # 'hotel' | 'city_fallback' (노션 §1 정의 그대로)
    tracked: bool           # Allianceid/SID 가 실린 제휴 링크인가
    zone_id: str = ""       # city_fallback 이 권역 zone 으로 좁혀졌으면 그 zone ID

    def to_dict(self) -> dict:
        return {
            "tripcom_link": self.url,
            "tripcom_link_type": self.link_type,
            "tracked": self.tracked,
            "tripcom_zone_id": self.zone_id,
        }


class BookingLinkBuilder:
    """Trip.com 딥링크 생성기.

    자격증명은 생성 시 한 번만 읽는다. 테스트에서는 값을 직접 주입해 환경을 타지 않게 한다.
    """

    def __init__(
        self,
        alliance_id: str | None = _UNSET,  # type: ignore[assignment]
        sid: str | None = _UNSET,  # type: ignore[assignment]
        hotel_map: dict[str, str] | None = None,
        ad_id: str | None = _UNSET,  # type: ignore[assignment]
    ):
        # 인자를 **생략**하면 환경에서 찾고, **명시적으로 None** 을 주면 비제휴 링크를 만든다.
        # 기본값을 None 으로 두면 두 의도를 구분할 수 없어서, 자격증명이 .env 에 들어온 순간
        # "비제휴로 만들어 달라" 는 요청이 조용히 제휴 링크로 바뀐다(실제로 테스트가 이걸 잡았다).
        self.alliance_id = _load_env("TRIPCOM_ALLIANCE_ID") if alliance_id is _UNSET else alliance_id
        self.sid = _load_env("TRIPCOM_SID") if sid is _UNSET else sid
        self.ad_id = _load_env("TRIPCOM_AD_ID") if ad_id is _UNSET else ad_id
        self.hotel_map = hotel_map if hotel_map is not None else load_hotel_map()

    @property
    def tracked(self) -> bool:
        return bool(self.alliance_id and self.sid)

    def _tracking_params(self, content_id: str, sid_key: str) -> dict[str, str]:
        """제휴 파라미터. 자격증명이 없으면 빈 dict → 비제휴 공개 URL 이 된다.

        Tier1 은 `Sid`, Tier2 는 `SID` 로 대소문자가 다르다(조사 원문 기준). 임의로 통일하지 말 것.
        """
        if not self.tracked:
            return {}
        params = {"Allianceid": self.alliance_id, sid_key: self.sid, "trip_sub1": content_id}
        if self.ad_id:
            # Ad ID(광고 단위). 대시보드 리포트가 이 값으로 행을 나눈다.
            # 숙박 링크는 **Hotels 계열 Ad ID** 를 쓴다 — 계정에 CarRental Ad ID 도 있어서
            # 그쪽이 붙으면 리포트 분류·커미션 카테고리가 어긋난다 (2026-08-22 팀 확정).
            params["trip_sub3"] = self.ad_id
        return params

    def build(
        self,
        content_id: str,
        *,
        tripcom_hotel_id: str | None = None,
        region: str = "",
        checkin: str = "",
        checkout: str = "",
        adults: int | None = None,
    ) -> BookingLink:
        """`tripcom_hotel_id` 를 명시하면(빈 문자열이어도) 그 값을 그대로 쓰고 내부
        `hotel_map` 조회를 건너뛴다 — 호출자가 `Lodging.tripcom_hotel_id`(숙소 마스터
        필드, `lodging_data.load_lodgings()` 가 `data/tripcom_hotel_map.csv` 를 병합해 채움)
        를 이미 들고 있을 때를 위함이다. 생략하면 기존처럼 `self.hotel_map` 에서 찾는다.
        """
        hotel_id = tripcom_hotel_id if tripcom_hotel_id is not None else self.hotel_map.get(str(content_id))

        # 숙박 조건(날짜·인원)은 **Tier 무관하게** 붙인다. 예전에는 Tier 2 에만 붙어서
        # 매핑된 숙소일수록 오히려 "오늘~내일" 기본값으로 열렸다 — 9월 여행을 계획한
        # 사용자가 예약 버튼을 누르면 매진 화면을 보게 된다. 매핑 커버리지가 88.2% 로
        # 올라가면서 이 결함이 대부분의 숙소에 적용돼 2026-08-22 고쳤다.
        #
        # 실측(2026-08-22): `/hotels/redirect?hotelid=7078350&checkin=2026-09-12&
        # checkout=2026-09-13&adult=4` → 착지 페이지에 "9월 12일(토)-9월 13일(일) 1박",
        # "성인 4명", 실시간 요금 128,150원 이 표시된다. 날짜를 빼면 같은 URL 이 "매진" 이다.
        # `adult` 는 리다이렉트 후 최종 URL 에서는 사라지지만 화면에는 반영된다.
        stay: dict[str, str] = {}
        if checkin:
            stay["checkin"] = checkin
        if checkout:
            stay["checkout"] = checkout
        if adults:
            stay["adult"] = str(adults)

        # Tier 1 (개별 호텔 상세 페이지)
        if hotel_id:
            params = {"hotelId": hotel_id, "curr": DEFAULT_CURRENCY, "locale": "ko-KR"}
            params.update({TIER1_STAY_PARAMS[k]: v for k, v in stay.items()})
            params.update(self._tracking_params(str(content_id), "Sid"))
            return BookingLink(f"{TIER1_URL}?{urlencode(params)}", "hotel", self.tracked)

        # Tier 2 (지역 폴백)
        params = {"curr": DEFAULT_CURRENCY, "locale": "ko-KR", **stay, **self._tracking_params(str(content_id), "SID")}

        # 권역 zone 이 있으면 제주 전역 대신 그 구역 목록으로 좁힌다.
        # 여전히 "목록" 이라 개별 숙소로 가지는 않는다 — 근본 해결은 Tier 1 이다.
        zone_id = REGION_ZONE_IDS.get(region, "")
        url = (
            TIER2_ZONE_URL.format(city_id=DEFAULT_CITY_ID, zone_id=zone_id)
            if zone_id
            else TIER2_URL.format(city_id=DEFAULT_CITY_ID)
        )
        if params:
            url = f"{url}?{urlencode(params)}"
        return BookingLink(url, "city_fallback", self.tracked, zone_id)


def price_hint(min_room_price: int | None, budget_per_night: int | None = None) -> str:
    """§4 `price_hint`.

    TourAPI 최저 객실가는 212건 중 110건(51.9%)뿐이고 실시간 가격이 아니다.
    그래서 예산을 입력받아도 **가격 없는 숙소를 제외하지 않고** 확인 문구만 남긴다.
    """
    if min_room_price is None:
        return "가격 확인 필요"
    text = f"참고가 {min_room_price:,}원~ (실시간 아님)"
    if budget_per_night is not None and min_room_price > budget_per_night:
        text += f" · 예산({budget_per_night:,}원) 초과"
    return text
