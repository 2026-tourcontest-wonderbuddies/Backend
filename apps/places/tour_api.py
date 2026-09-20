import requests
from decouple import config

TOUR_API_KEY = config("TOUR_API_KEY")
TOUR_API_BASE = "http://apis.data.go.kr/B551011/KorService2"

# 콘텐츠타입별로 실제 필드명이 다름 — 우리가 쓸 5개 항목만 매핑
FIELD_MAP = {
    "12": {"parking": "parking", "contact": "infocenter", "hours": "usetime", "closed": "restdate", "fee": "usefee"},
    "14": {"parking": "parkingculture", "contact": "infocenterculture", "hours": "usetimeculture", "closed": "restdateculture", "fee": "usefee"},
    "39": {"parking": "parkingfood", "contact": "infocenterfood", "hours": "opentimefood", "closed": "restdatefood", "fee": "firstmenu"},  # 음식점은 fee 대신 대표메뉴로 대체 가능
    # 필요시 15(행사), 25(코스), 28(레포츠), 32(숙박), 38(쇼핑)도 추가
}


def fetch_place_realtime_info(content_id: str, content_type_id: str) -> dict:
    """
    TourAPI detailIntro2로 주차/연락처/운영시간/휴무일/이용요금을 실시간 조회.
    실패 시 빈 dict 반환 — 호출부에서 DB 데이터로 폴백 가능하게.
    """
    field_map = FIELD_MAP.get(str(content_type_id))
    if not field_map:
        return {}

    try:
        resp = requests.get(
            f"{TOUR_API_BASE}/detailIntro2",
            params={
                "serviceKey": TOUR_API_KEY,
                "contentId": content_id,
                "contentTypeId": content_type_id,
                "MobileOS": "ETC",
                "MobileApp": "JejuTravelApp",
                "_type": "json",
            },
            timeout=3,   # 외부 API 지연이 우리 서비스 전체를 늦추지 않도록 필수
        )
        resp.raise_for_status()
        body = resp.json()["response"]["body"]

        if body.get("totalCount", 0) == 0:
            return {}

        item = body["items"]["item"][0]

        return {
            "parking": item.get(field_map["parking"], ""),
            "contact": item.get(field_map["contact"], ""),
            "hours_raw": item.get(field_map["hours"], ""),
            "closed_days_raw": item.get(field_map["closed"], ""),
            "fees": item.get(field_map["fee"], ""),
        }
    except Exception:
        # 타임아웃, 네트워크 오류, 파싱 실패 등 — 조용히 실패하고 DB 데이터로 폴백
        return {}