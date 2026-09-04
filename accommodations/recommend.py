"""
숙박 추천 파이프라인 — 노션 04-1 "숙박 로직" 구현체.

    1. 사용자 입력값 수집            → `lodging_filter.LodgingRequest`
    2. 후보 필터링 + Unknown 처리    → `lodging_filter.filter_candidates`
    3. 이동 및 최종 순위 (상위 3개)  → 이 모듈
    4. [신규] 예약 연결              → `booking_link`
    5. 최종 카드 출력                → `LodgingCard.to_dict()`

사용:

    from recommend import LodgingRecommender, LodgingRequest
    from kr_sbert_embedder import KrSbertEmbedder

    recommender = LodgingRecommender(embedder=KrSbertEmbedder())  # 데이터·인덱스 1회 로드
    # embedder 생략 시 CharNgramEmbedder(문자 2-gram) 폴백으로 동작한다 — 의존성 없는
    # 빠른 테스트·데모용. 실제 자유입력 매칭 품질이 필요하면 위처럼 반드시 주입할 것
    # (query_fit.py L3 절 "확정 필요" 참조).
    request = LodgingRequest(guests=4, lodging_type="펜션·민박",
                             need_cooking=True, regions=("남부서",),
                             free_text="바비큐가 가능하고 해변과 가까운 숙소",
                             vehicle="rental",
                             checkin_date="2026-09-12", checkout_date="2026-09-13")
    cards = recommender.recommend(last_place_id="126295", request=request)

`LodgingRecommender` 는 프로세스당 1개만 만들어 재사용할 것 —
생성 시 CSV·이동행렬(22MB)·QueryFit 인덱스를 전부 읽는다.
"""

from __future__ import annotations

import logging
import sys
from dataclasses import dataclass, field, replace
from pathlib import Path

MODULE_DIR = Path(__file__).resolve().parent
PROJECT_DIR = MODULE_DIR.parent
ROUTING_DIR = PROJECT_DIR / "routing"

# routing 모듈들은 서로를 평면 이름으로 import 한다(`from hybrid_engine import ...`).
# 같은 규약을 따르려면 routing 디렉터리를 경로에 올려 둬야 한다.
for _path in (str(MODULE_DIR), str(ROUTING_DIR)):
    if _path not in sys.path:
        sys.path.insert(0, _path)

from booking_link import BookingLinkBuilder, price_hint  # noqa: E402
from lodging_data import Lodging, Tri, load_lodgings  # noqa: E402
from lodging_filter import (  # noqa: E402
    ConditionCheck,
    FilterResult,
    LodgingRequest,
    TripContext,
    Verdict,
    exclusion_summary,
    filter_candidates,
)
from lodging_stop import LodgingStop, plan_stops, render_stops  # noqa: E402
from query_fit import PREFERRED_POOL_SIZE, Embedder, QueryFitIndex, top_query_fit  # noqa: E402

log = logging.getLogger(__name__)

TOP_N = 3  # 노션 §3 "상위 3개 추천"

# 여행 하나에 숙소를 몇 곳까지 잡나. EDA §1.7 실측: 여행당 평균 1.72곳, 1곳으로 끝낸
# 여행 41.5%(3박4일도 1.42곳). "매일 옮기는 사람은 없다" 가 결론이라 상한을 2로 둔다.
# `split_nights()` 자체의 상한이고, 실제로 분할을 쓸지는 아래 SPLIT_ANCHORS_ENABLED 가 정한다.
MAX_ANCHORS = 2

# 앵커를 나눌지 고민이라도 하는 최소 박 수. 1박2일·2박3일은 무조건 1곳이다.
MIN_NIGHTS_FOR_SPLIT = 3

# 앵커 분할을 지금 실제로 쓸지 — 2026-08-29 잠정 비활성화.
# AI Hub 원본(03_visit_제주.csv, 숙박 방문 4,645건)으로 "그날 마지막 장소" 권역 변경 신호를
# 실제 숙소 교체 시점과 대조했더니: 실제로 숙소를 2곳 쓴 여행(330건)의 20.9%는 분할을 아예
# 놓치고, 반대로 숙소를 안 바꾼 여행(282건)의 40.8%는 괜히 분할로 오판했다(코스가 한라산
# 경계를 넘나들 뿐 숙소는 그대로인 경우). 분할 지점 자체는 감지만 되면 76%가 정확했지만,
# "분할해야 하나 말아야 하나"가 양방향으로 불안정해 코스 로직 복잡도를 감수할 값어치가
# 없다고 판단했다 — 지금은 박 수·권역 변경과 무관하게 앵커 1곳으로 고정한다.
# `split_nights()` 자체(및 테스트)는 그대로 둔다 — 판정 신호가 개선되면(예: 연속 며칠 이상
# 권역이 유지될 때만 변경으로 보기) 여기만 True 로 바꾸면 된다. README §4·docs/08.28 §9 참고.
SPLIT_ANCHORS_ENABLED = False

# 등급 내 정렬 가중치 — 이동시간과 QueryFit을 사전식이 아니라 연속 가중합으로 결합한다.
# 2026-08-29 확정(README §4, balanced 프리셋 실측 비교 후 채택 — rank_weighted_prototype.py
# 참고). 노션 04-1 §3 은 "이동시간 → QueryFit 사전식"을 명시하지만, 실측에서 같은 등급 내
# 이동시간이 정확히 동률인 후보가 43%(실제 라우팅 엔진 기준 duration_min 0.1분 반올림
# 때문)나 됐다 — QueryFit이 이미 자주 순위를 가른다는 뜻이다. 문제는 "동률은 아니지만
# 비슷한"(예: 25.0분 vs 25.5분) 구간에서 QueryFit 차이가 아무리 커도 항상 이동시간이
# 이기는 쪽이었다. 가중치는 반올림·밴딩이 아니라 항상 연속 결합이라 §3.5 가 경계한
# "인위적 동률로 2순위 신호가 과도하게 개입하는" 함정과는 다르다.
W_TRAVEL_MIN = 1.0  # 이동시간 1분당 페널티
W_QUERY_FIT = 1.0  # QueryFit 1점당 보너스


def split_nights(trip: TripContext, max_anchors: int = MAX_ANCHORS) -> list[list[int]]:
    """박 인덱스를 앵커별로 나눈다. `[[0, 1], [2, 3]]` = 앞 2박 한 곳, 뒤 2박 한 곳.

    규칙은 EDA §1.7 실측을 그대로 옮긴 것이다.

        3박 미만            → 1곳   (41.5% 가 1곳으로 끝내고, 짧을수록 더 그렇다)
        3박 이상 + 권역 변경 → 2곳   (연속일 권역 변경 56.5%)
        권역 정보 없음       → 1곳   (보수적으로. 매일 옮기라는 결과보다 낫다)

    ⚠️ 첫 권역 변경 지점에서 한 번만 자른다. `A B A B` 처럼 권역이 왔다 갔다 하면
    `[A] [B A B]` 가 되어 최적이 아니지만, 상한이 2곳인 이상 어느 지점을 골라도
    한 번은 어긋난다. 실측에서 드문 패턴이라 단순한 쪽을 택했다.
    """
    if trip.nights <= 0:
        return []
    if trip.nights < MIN_NIGHTS_FOR_SPLIT or max_anchors < 2 or not trip.day_regions:
        return [list(range(trip.nights))]

    for i in range(1, trip.nights):
        if trip.day_regions[i] != trip.day_regions[i - 1]:
            return [list(range(i)), list(range(i, trip.nights))]
    return [list(range(trip.nights))]


@dataclass
class LodgingCard:
    """최종 카드 1장 (§5). Pipeline 7 'Place Cards' 숙박 항목에 그대로 실린다."""

    content_id: str
    title: str
    address: str
    region: str
    small_category: str
    lon: float
    lat: float

    grade: int
    travel_min: float          # 담당하는 박이 여러 개면 **1박당 평균**이다 (UI 표시용)
    query_fit: float | None

    # 앵커가 여러 박을 담당할 때의 내역. 1박짜리면 total == travel_min, by_night 는 1개다.
    travel_min_total: float = 0.0        # 정렬에 실제로 쓰인 합계
    travel_min_by_night: list[float] = field(default_factory=list)

    unknown_fields: list[str] = field(default_factory=list)
    checks: list[ConditionCheck] = field(default_factory=list)

    # 안내 정보
    check_in_time: str = ""
    check_out_time: str = ""
    room_count: int | None = None
    room_type: str = ""
    max_guests: int | None = None

    # §4 예약 연결
    tripcom_link: str = ""
    tripcom_link_type: str = ""
    tripcom_tracked: bool = False
    tripcom_zone_id: str = ""
    price_hint: str = ""

    @property
    def needs_check(self) -> bool:
        return bool(self.unknown_fields)

    def to_dict(self) -> dict:
        return {
            "content_id": self.content_id,
            "title": self.title,
            "address": self.address,
            "region": self.region,
            "category": self.small_category,
            "lon": self.lon,
            "lat": self.lat,
            "grade": self.grade,
            "travel_min": self.travel_min,
            "travel_min_total": self.travel_min_total,
            "travel_min_by_night": self.travel_min_by_night,
            "query_fit": self.query_fit,
            "needs_check": self.needs_check,
            "unknown_fields": self.unknown_fields,
            "checks": [c.to_dict() for c in self.checks],
            "check_in_time": self.check_in_time,
            "check_out_time": self.check_out_time,
            "room_count": self.room_count,
            "room_type": self.room_type,
            "max_guests": self.max_guests,
            "tripcom_link": self.tripcom_link,
            "tripcom_link_type": self.tripcom_link_type,
            "tripcom_tracked": self.tripcom_tracked,
            "tripcom_zone_id": self.tripcom_zone_id,
            "price_hint": self.price_hint,
        }


def _dedupe_by_hotel(rows: list) -> list:
    """같은 Trip.com 호텔로 가는 후보는 하나만 남긴다. **정렬 뒤에** 부를 것.

    TourAPI 숙소 2건이 같은 `tripcom_hotel_id` 를 갖는 쌍이 실제로 있다.

        그랜드 하얏트 제주(호텔) · 제주드림타워 복합리조트(리조트)   — 같은 건물이라 매핑이 맞다
        취다선 리조트(호텔) · 취다선리조트(게스트하우스)             — TourAPI 에 같은 업소가 두 번 등록

    접지 않으면 **상위 3개 중 2장이 같은 예약 페이지로 간다** — 선택지가 셋인 줄 알았는데
    실제로는 둘이다. 취다선 쌍은 분류가 갈려 있어서 "호텔" 로 걸러도 하나, "게스트하우스"
    로 걸러도 하나 나오는데, 유형별로 다른 후보처럼 보이지만 같은 곳이다.

    남길 쪽은 **정렬 순서가 정한다** — 이미 (등급 → 이동시간 → QueryFit → Unknown 적은 순
    → content_id) 로 서 있으므로 앞선 것이 곧 "정보가 더 확실하고 더 가까운 쪽" 이다.

    `tripcom_hotel_id` 가 빈 후보(`require_deeplink=False` 일 때의 Tier 2)는 서로 다른
    숙소이므로 **묶지 않는다.**
    """
    seen: set[str] = set()
    kept = []
    for row in rows:
        hotel_id = row[0].lodging.tripcom_hotel_id
        if hotel_id:
            if hotel_id in seen:
                log.debug(
                    "같은 Trip.com 호텔(%s)로 가는 중복 후보를 접었다: %s",
                    hotel_id,
                    row[0].lodging.title,
                )
                continue
            seen.add(hotel_id)
        kept.append(row)
    return kept


@dataclass
class LodgingSegment:
    """앵커 1곳이 담당하는 구간 + 그 구간의 추천 카드.

    `SPLIT_ANCHORS_ENABLED = False` 인 동안은 박 수와 무관하게 항상 구간 1개다.
    사용자는 구간마다 카드 1장씩 고른다 — 구간 간 조합을 우리가 미리 곱해 두지 않는다
    (3장 × 3장 = 9안이 되면 고를 수 없다).
    """

    index: int                                   # 0-based 앵커 순번
    night_indexes: list[int]                     # 담당하는 박 (0 = 1박)
    checkin_date: str                            # 구간 첫 박의 체크인
    checkout_date: str                           # 구간 마지막 박의 체크아웃
    last_place_ids: list[str]                    # 담당 박들의 '그날 마지막 장소'
    cards: list[LodgingCard] = field(default_factory=list)

    @property
    def nights(self) -> int:
        return len(self.night_indexes)

    def to_dict(self) -> dict:
        return {
            "index": self.index,
            "night_indexes": self.night_indexes,
            "nights": self.nights,
            "checkin_date": self.checkin_date,
            "checkout_date": self.checkout_date,
            "last_place_ids": self.last_place_ids,
            "cards": [card.to_dict() for card in self.cards],
        }


def _grade(has_free_text: bool, unknown_count: int, in_pool: bool) -> int:
    """노션 §3 등급표.

    자유 입력 없음:  1 = 확인·충족 / 2 = 일부 Unknown
    자유 입력 있음:  1 = 충족+상위10 / 2 = 충족 / 3 = Unknown+상위10 / 4 = Unknown
    """
    confirmed = unknown_count == 0
    if not has_free_text:
        return 1 if confirmed else 2
    if confirmed:
        return 1 if in_pool else 2
    return 3 if in_pool else 4


class LodgingRecommender:
    """숙박 추천 진입점.

    `engine` 을 주입하지 않으면 `routing.HybridRoutingEngine` 을 직접 만든다
    (`travel_matrix.npz` 22MB + `place_index.parquet` 로드). 테스트나 이동시간 mocking 이
    필요하면 `travel_time_fn=lambda origin, dest: 분` 을 넘겨 엔진 없이 돌릴 수 있다.
    """

    def __init__(
        self,
        lodgings: list[Lodging] | None = None,
        engine=None,
        travel_time_fn=None,
        embedder: Embedder | None = None,
        link_builder: BookingLinkBuilder | None = None,
        require_deeplink: bool = True,
    ):
        self.all_lodgings = list(lodgings if lodgings is not None else load_lodgings())
        self.require_deeplink = require_deeplink

        # 후보 풀 정책 — 상세정보를 Trip.com 딥링크가 책임지므로, 개별 상세 페이지로 보낼 수
        # 없는 숙소(Tier 2 폴백)는 기본적으로 후보에서 뺀다. Tier 2 는 "서귀포 숙소 목록"
        # 으로 보내는 것이라 사용자가 그 숙소를 다시 찾을 수 없고, 상위 3개 슬롯만 먹는다.
        #
        # ⚠️ 이 제한은 유형에 따라 불균등하다 — Trip.com 이 소규모 업소를 안 실어서
        # 농어촌민박 5곳·한옥스테이 1곳이 **전멸**하고 펜션은 69→55 로 준다. "펜션·민박"
        # 선택 시 후보가 66→50(-24%)이 되는 반면 호텔은 -2% 다. 매칭 실패는 숙소의 품질이
        # 아니라 우리 링크의 한계이므로, 유형 편향이 문제가 되면 `require_deeplink=False`
        # 로 되돌리고 Tier 2 후보를 후순위로 내리는 쪽을 검토할 것 (README §3.8).
        self.lodgings = (
            [l for l in self.all_lodgings if l.tripcom_hotel_id]
            if require_deeplink
            else list(self.all_lodgings)
        )
        if require_deeplink and len(self.lodgings) < len(self.all_lodgings):
            log.info(
                "후보 풀 %d곳 (전체 %d곳 중 딥링크 미보유 %d곳 제외).",
                len(self.lodgings),
                len(self.all_lodgings),
                len(self.all_lodgings) - len(self.lodgings),
            )

        # QueryFit 인덱스도 후보 풀 위에서 만든다 — 추천될 수 없는 숙소가 IDF 코퍼스에만
        # 남아 상위 10 후보군 경쟁에 영향을 주면 안 된다.
        self.query_index = QueryFitIndex.build(self.lodgings, embedder=embedder)
        self.link_builder = link_builder or BookingLinkBuilder()
        self._travel_time_fn = travel_time_fn
        self._engine = engine
        if travel_time_fn is None and engine is None:
            from hybrid_engine import HybridRoutingEngine

            self._engine = HybridRoutingEngine()

    # --- §3.1 이동시간 ---------------------------------------------------

    def _travel_min(self, origin_id: str, lodging: Lodging, vehicle: str | None) -> float:
        """하루 코스 마지막 장소 → 숙소 이동시간(분).

        `vehicle` 을 주면 §5 오버헤드 보정이 적용된 `duration_min_adjusted` 를 쓴다.
        5분 격자 반올림은 하지 않는다 — 격자는 `Itinerary` 계층의 책임이고,
        여기서 반올림하면 동률이 인위적으로 늘어 2순위 정렬이 과도하게 개입한다.
        """
        if self._travel_time_fn is not None:
            return float(self._travel_time_fn(origin_id, lodging.content_id))

        result = self._engine.get_travel_time(
            origin_id, lodging.content_id, mode="osrm", vehicle=vehicle
        )
        return float(result.get("duration_min_adjusted", result["duration_min"]))

    # --- §3 최종 순위 ----------------------------------------------------

    def _prepare(
        self, request: LodgingRequest
    ) -> tuple[list[FilterResult], dict[str, float], set[str]]:
        """필터 + QueryFit. 앵커 구간이 여러 개여도 이 둘은 구간과 무관해 **한 번만** 돈다.

        (구간별로 달라지는 건 기준점과 날짜뿐이고, 날짜는 필터 조건이 아니다.)
        """
        kept, dropped = filter_candidates(self.lodgings, request)
        if len(kept) < TOP_N:
            # 사유를 **전부** 센다. 첫 사유만 세면 실제 원인이 가려진다 — 취사가 51곳을
            # 죽였는데 대표 사유로는 1곳으로만 보이는 사례가 실측에서 나온다.
            summary = exclusion_summary(dropped)
            log.warning(
                "조건을 통과한 숙소가 %d곳뿐이다 (풀 %d곳 중 %d곳 제외). 걸린 조건: %s",
                len(kept),
                len(self.lodgings),
                len(dropped),
                " · ".join(f"{name} {n}곳" for name, n in summary.items()) or "없음",
            )
        if not kept:
            return [], {}, set()

        scores = self.query_index.score(request.free_text) if request.has_free_text else {}
        pool = (
            top_query_fit(scores, [r.lodging.content_id for r in kept], PREFERRED_POOL_SIZE)
            if scores
            else set()
        )
        return kept, scores, pool

    def _rank_rows(
        self,
        kept: list[FilterResult],
        scores: dict[str, float],
        pool: set[str],
        origin_ids: list[str],
        request: LodgingRequest,
        top_n: int,
    ) -> list[tuple[FilterResult, int, list[float], float | None]]:
        """기준점 여러 개에 대한 이동시간 **합**으로 정렬한다.

        기준점 1개면 기존 하루 정렬과 결과가 완전히 같다. 여러 개일 때 합을 쓰는 이유는
        앵커 1곳이 여러 박을 담당하기 때문이다 — 어느 하루만 가깝고 나머지가 먼 숙소보다
        전 일정에 걸쳐 고르게 가까운 숙소가 이겨야 한다.
        """
        rows = []
        for result in kept:
            content_id = result.lodging.content_id
            travels = [
                self._travel_min(origin_id, result.lodging, request.vehicle)
                for origin_id in origin_ids
            ]
            fit = scores.get(content_id) if scores else None
            grade = _grade(request.has_free_text, result.unknown_count, content_id in pool)
            rows.append((result, grade, travels, fit))

        # 등급 → (이동시간·QueryFit 가중합) → Unknown 적은 순.
        # 등급 자체는 노션 04-1 §3 그대로 1순위다. 등급 내부만 사전식이 아니라
        # W_TRAVEL_MIN·W_QUERY_FIT 가중합으로 결합한다(2026-08-29 확정, 위 상수 설명 참조) —
        # 기획안 §3의 "이동시간 → QueryFit" 순서를 벗어나는 판단이라 README §4 에 남겨 뒀다.
        #
        # 유형은 정렬에 없다. §2.1-2 를 **필터링으로 확정**했으므로(2026-08-28) 여기까지
        # 온 후보는 이미 전부 유형이 맞는다 — 순위에서 다시 볼 것이 없다.
        rows.sort(
            key=lambda row: (
                row[1],
                W_TRAVEL_MIN * sum(row[2]) - W_QUERY_FIT * (row[3] or 0.0),
                row[0].unknown_count,
                row[0].lodging.content_id,
            )
        )
        return _dedupe_by_hotel(rows)[:top_n]

    def rank(
        self, last_place_id: str, request: LodgingRequest, top_n: int = TOP_N
    ) -> list[tuple[FilterResult, int, float, float | None]]:
        """(필터결과, 등급, 이동시간, QueryFit) 을 순위대로. 카드 변환 전 단계.

        하루 1건짜리 진입점이다. 여행 전체의 숙소를 정하는 건 `recommend_anchor()` 쪽이고,
        이 메서드는 하루 엔진 검증(도민 당일 나들이 449일)과 디버깅용으로 남는다.
        """
        rows = self._rank_rows(*self._prepare(request), [last_place_id], request, top_n)
        return [(result, grade, travels[0], fit) for result, grade, travels, fit in rows]

    def _build_card(
        self,
        result: FilterResult,
        grade: int,
        travels: list[float],
        fit: float | None,
        request: LodgingRequest,
    ) -> LodgingCard:
        """정렬 결과 1행 → 카드 1장 (§4 예약 연결 + §5 카드 출력)."""
        lodging = result.lodging
        link = self.link_builder.build(
            lodging.content_id,
            tripcom_hotel_id=lodging.tripcom_hotel_id,
            region=lodging.region,
            checkin=request.checkin_date,
            checkout=request.checkout_date,
            adults=request.guests,
        )
        total = sum(travels)
        return LodgingCard(
            content_id=lodging.content_id,
            title=lodging.title,
            address=lodging.address,
            region=lodging.region,
            small_category=lodging.small_category,
            lon=lodging.lon,
            lat=lodging.lat,
            grade=grade,
            # 여러 박을 담당하면 1박당 평균을 싣는다 — 카드의 "이동 12분" 은 하루치 감각이다.
            travel_min=round(total / len(travels), 1) if travels else 0.0,
            travel_min_total=round(total, 1),
            travel_min_by_night=[round(t, 1) for t in travels],
            query_fit=fit,
            unknown_fields=result.unknown_fields,
            checks=result.checks,
            check_in_time=lodging.check_in_time,
            check_out_time=lodging.check_out_time,
            room_count=lodging.room_count,
            room_type=lodging.room_type,
            max_guests=lodging.max_guests,
            tripcom_link=link.url,
            tripcom_link_type=link.link_type,
            tripcom_tracked=link.tracked,
            tripcom_zone_id=link.zone_id,
            price_hint=price_hint(lodging.min_room_price, request.budget_per_night),
        )

    def recommend(
        self, last_place_id: str, request: LodgingRequest, top_n: int = TOP_N
    ) -> list[LodgingCard]:
        """하루 1건. 상위 `top_n` 개 카드를 돌려준다."""
        rows = self._rank_rows(*self._prepare(request), [last_place_id], request, top_n)
        return [self._build_card(*row, request) for row in rows]

    # --- 앵커 선정 (여행 전체) --------------------------------------------

    def recommend_anchor(
        self,
        trip: TripContext,
        request: LodgingRequest | None = None,
        top_n: int = TOP_N,
    ) -> list[LodgingSegment]:
        """여행 전체의 숙소 앵커를 고른다. **다일 여행의 정식 진입점이다.**

        하루씩 독립으로 풀면(`recommend()` 를 날마다 호출) 2박3일에 숙소 3곳이 나오는데,
        그건 매일 짐 싸서 체크아웃하라는 뜻이다. 실측은 반대다 — 여행당 평균 1.72곳,
        41.5% 가 1곳으로 끝낸다(EDA §1.7). 그래서 앵커를 먼저 정하고 박을 배분한다.

        여행 전체 1~2곳으로 나누는 `split_nights()` 자체는 있지만, `SPLIT_ANCHORS_ENABLED
        = False` 라 지금은 **박 수·권역 변경과 무관하게 항상 1곳**을 반환한다 — 실측으로
        분할 판정이 양방향으로 불안정한 게 확인돼(README §4) 잠정 비활성화했다.

        정렬 기준은 여전히 **그날 마지막 장소까지의 이동시간**이다. 숙소가 하루 중간에도
        들르는 경유지라 코스 중심이 기준이어야 할 것 같지만, 실제로 사람들이 고른 숙소는
        마지막 장소에서 중앙 2.8km(25% 는 0.5km 이내), 코스 중심점에서는 10.5km 다.

            trip = TripContext(start_date="2026-09-12", nights=2, guests=4,
                               vehicle="rental",
                               day_last_place_ids=("126295", "126435"))
            request = LodgingRequest.from_trip_context(trip, lodging_type="펜션·민박")
            segments = recommender.recommend_anchor(trip, request)

        `request` 를 생략하면 폼 기본값(상관없음 · 취사 안 함 · 자유입력 없음)으로 만든다.
        구간이 2개면 구간마다 카드 `top_n` 장씩 나오고, 사용자는 구간당 1장을 고른다.
        """
        if request is None:
            request = LodgingRequest.from_trip_context(trip)
        if trip.nights <= 0:
            return []
        if not trip.day_last_place_ids:
            raise ValueError(
                "day_last_place_ids 가 비어 있다. 앵커는 '그날 마지막 장소' 를 기준으로 고르므로 "
                "코스 엔진 결과를 먼저 채워야 한다"
            )

        night_dates = trip.night_dates()
        kept, scores, pool = self._prepare(request)

        # SPLIT_ANCHORS_ENABLED=False 인 동안은 박 수·권역과 무관하게 항상 구간 1개다.
        night_groups = split_nights(trip) if SPLIT_ANCHORS_ENABLED else [list(range(trip.nights))]

        segments: list[LodgingSegment] = []
        for index, night_indexes in enumerate(night_groups):
            checkin = night_dates[night_indexes[0]][0]
            checkout = night_dates[night_indexes[-1]][1]
            last_ids = [trip.day_last_place_ids[i] for i in night_indexes]
            segment = LodgingSegment(
                index=index,
                night_indexes=night_indexes,
                checkin_date=checkin,
                checkout_date=checkout,
                last_place_ids=last_ids,
            )
            if kept:
                # 딥링크에는 그 앵커가 실제로 묵는 구간을 보낸다 — 2곳으로 나뉘면
                # 여행 전체 구간(from_trip_context 기본값)이 아니라 구간 날짜가 맞다.
                seg_request = replace(request, checkin_date=checkin, checkout_date=checkout)
                rows = self._rank_rows(kept, scores, pool, last_ids, seg_request, top_n)
                segment.cards = [self._build_card(*row, seg_request) for row in rows]
            segments.append(segment)
        return segments

    # --- 디버깅용 ---------------------------------------------------------

    def render(self, cards: list[LodgingCard]) -> str:
        if not cards:
            return "조건을 통과한 숙소가 없습니다."
        lines = []
        for rank, card in enumerate(cards, 1):
            fit = f" · QueryFit {card.query_fit:.1f}" if card.query_fit is not None else ""
            lines.append(
                f"{rank}. [{card.grade}등급] {card.title} ({card.small_category}/{card.region})"
                f"  이동 {card.travel_min:.0f}분{fit}"
            )
            lines.append(f"   {card.address}")
            lines.append(f"   {card.price_hint} · 입실 {card.check_in_time} / 퇴실 {card.check_out_time}")
            if card.needs_check:
                lines.append(f"   ⚠ 확인 필요: {', '.join(card.unknown_fields)}")
            lines.append(f"   예약: {card.tripcom_link_type} {card.tripcom_link}")
        return "\n".join(lines)

    def render_segments(self, segments: list[LodgingSegment]) -> str:
        if not segments:
            return "숙박이 없는 일정입니다."
        lines = []
        for segment in segments:
            nights = "·".join(str(i + 1) for i in segment.night_indexes)
            lines.append(
                f"[앵커 {segment.index + 1}] {nights}박 "
                f"({segment.checkin_date} ~ {segment.checkout_date})"
            )
            if not segment.cards:
                lines.append("   조건을 통과한 숙소가 없습니다.")
                continue
            body = self.render(segment.cards).splitlines()
            lines.extend(f"  {line}" for line in body)
        return "\n".join(lines)


__all__ = [
    "LodgingCard",
    "LodgingRecommender",
    "LodgingRequest",
    "LodgingSegment",
    "LodgingStop",
    "Tri",
    "TripContext",
    "Verdict",
    "exclusion_summary",
    "plan_stops",
    "render_stops",
    "split_nights",
]
