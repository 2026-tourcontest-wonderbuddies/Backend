import requests
from decouple import config

TOUR_API_KEY = config("TOUR_API_KEY")
TOUR_API_BASE = "http://apis.data.go.kr/B551011/KorService2"

# 콘텐츠타입별로 실제 필드명이 다름 — 우리가 쓸 5개 항목만 매핑
FIELD_MAP = {
    "12": {"parking": "parking", "contact": "infocenter", "hours": "usetime", "closed": "restdate", "fee": "usefee"},
    "14": {"parking": "parkingculture", "contact": "infocenterculture", "hours": "usetimeculture", "closed": "restdateculture", "fee": "usefee"},
    "39": {"parking": "parkingfood", "contact": "infocenterfood", "hours": "opentimefood", "closed": "restdatefood", "fee": None},  # 음식점은 fee 대신 대표메뉴로 대체 가능
    # 필요시 15(행사), 25(코스), 28(레포츠), 32(숙박), 38(쇼핑)도 추가
}


def fetch_place_realtime_info(content_id: str, content_type_id: str) -> dict:
    if not TOUR_API_KEY:
        return {}

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
            timeout=3,
        )
        resp.raise_for_status()
        body = resp.json()["response"]["body"]

        if body.get("totalCount", 0) == 0:
            return {}

        item = body["items"]["item"][0]

        result = {
            "parking": item.get(field_map["parking"], ""),
            "contact": item.get(field_map["contact"], ""),
            "hours_raw": item.get(field_map["hours"], ""),
            "closed_days_raw": item.get(field_map["closed"], ""),
        }
        # fee 필드가 실제로 존재하는 콘텐츠타입만 fees를 응답에 포함 — 없는 곳은
        # 기존 DB 값을 건드리지 않도록 아예 키를 안 넣는다.
        if field_map.get("fee"):
            result["fees"] = item.get(field_map["fee"], "")

        return result
    except Exception:
        return {}