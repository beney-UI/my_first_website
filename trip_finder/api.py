import os
import re
from math import asin, cos, radians, sin, sqrt
from calendar import monthrange
from datetime import datetime, timedelta, timezone
from html import unescape
from html.parser import HTMLParser
from typing import TypedDict
from urllib.parse import unquote, urlparse

import requests
import streamlit as st

from trip_finder.data import Place, Review, Video


TOUR_SEARCH_URL = "https://apis.data.go.kr/B551011/KorService2/searchKeyword2"
TOUR_DETAIL_URL = "https://apis.data.go.kr/B551011/KorService2/detailCommon2"
TOUR_LDONG_URL = "https://apis.data.go.kr/B551011/KorService2/ldongCode2"
TOUR_AREA_BASED_URL = "https://apis.data.go.kr/B551011/KorService2/areaBasedList2"
TOUR_LOCATION_BASED_URL = "https://apis.data.go.kr/B551011/KorService2/locationBasedList2"
KAKAO_KEYWORD_URL = "https://dapi.kakao.com/v2/local/search/keyword.json"
KAKAO_CATEGORY_URL = "https://dapi.kakao.com/v2/local/search/category.json"
KAKAO_ADDRESS_URL = "https://dapi.kakao.com/v2/local/search/address.json"
NAVER_BLOG_URL = "https://openapi.naver.com/v1/search/blog.json"
YOUTUBE_SEARCH_URL = "https://www.googleapis.com/youtube/v3/search"
REQUEST_TIMEOUT = 8
MAX_TOUR_RESULTS = 60
TOUR_PAGE_SIZE = 20
TOUR_DETAIL_LIMIT = 8
CONTENT_PAGE_SIZE = 6
CONTENT_SEARCH_LIMIT = 30
CONTENT_CACHE_TTL = 1800
PERIOD_OPTIONS = ("최근 7일", "최근 30일", "최근 3개월", "전체 기간")
VIDEO_ORDER_OPTIONS = ("관련도순", "최신순")


class SearchResult(TypedDict, total=False):
    status: str
    places: list[Place]
    message: str
    candidates: list[str]
    location: dict[str, str]


class Destination(TypedDict, total=False):
    name: str
    address: str
    x: str
    y: str
    region1: str
    region2: str
    region3: str
    area_code: str
    sigungu_code: str
    place_url: str
    kind: str


class ContentSearchResult(TypedDict, total=False):
    status: str
    items: list[Review] | list[Video]
    unconfirmed_items: list[Video]
    comparison_items: list[Video]
    message: str
    primary_keyword: str
    expanded: bool


class NearbyPlaceGroup(TypedDict):
    attraction: Place
    restaurants: list[Place]
    cafes: list[Place]


class NearbySearchResult(TypedDict, total=False):
    status: str
    groups: list[NearbyPlaceGroup]
    restaurants: list[Place]
    cafes: list[Place]
    message: str
    skipped_attractions: int


class _TextParser(HTMLParser):
    def __init__(self) -> None:
        super().__init__()
        self.parts: list[str] = []
        self.ignored_tags: list[str] = []

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        if tag.lower() in ("script", "style"):
            self.ignored_tags.append(tag.lower())

    def handle_endtag(self, tag: str) -> None:
        if self.ignored_tags and tag.lower() == self.ignored_tags[-1]:
            self.ignored_tags.pop()

    def handle_data(self, data: str) -> None:
        if data.strip() and not self.ignored_tags:
            self.parts.append(data.strip())


def _plain_text(value: object) -> str:
    if not isinstance(value, str):
        return ""
    parser = _TextParser()
    parser.feed(unescape(value))
    return " ".join(parser.parts).strip()


def _api_key(name: str) -> str | None:
    try:
        secret_value = st.secrets[name]
    except Exception:
        secret_value = None
    if secret_value and str(secret_value).strip():
        return str(secret_value).strip()
    environment_value = os.getenv(name)
    return environment_value.strip() if environment_value and environment_value.strip() else None


def get_tour_api_key() -> str | None:
    return _api_key("TOUR_API_KEY")


def get_kakao_api_key() -> str | None:
    return _api_key("KAKAO_REST_API_KEY")


def get_naver_api_credentials() -> tuple[str | None, str | None]:
    return _api_key("NAVER_CLIENT_ID"), _api_key("NAVER_CLIENT_SECRET")


def get_youtube_api_key() -> str | None:
    return _api_key("YOUTUBE_API_KEY")


def _safe_url(value: object) -> str | None:
    if not isinstance(value, str):
        return None
    parsed = urlparse(value.strip())
    return value.strip() if parsed.scheme in ("http", "https") and parsed.netloc else None


def _date_cutoff(period: str) -> datetime | None:
    now = datetime.now(timezone.utc)
    if period == "최근 7일":
        return now - timedelta(days=7)
    if period == "최근 30일":
        return now - timedelta(days=30)
    if period == "최근 3개월":
        month_index = now.month - 3
        year = now.year
        while month_index <= 0:
            month_index += 12
            year -= 1
        day = min(now.day, monthrange(year, month_index)[1])
        return now.replace(year=year, month=month_index, day=day)
    return None


def _parse_naver_date(value: object) -> datetime | None:
    if not isinstance(value, str) or not re.fullmatch(r"\d{8}", value):
        return None
    try:
        return datetime.strptime(value, "%Y%m%d").replace(tzinfo=timezone.utc)
    except ValueError:
        return None


def _parse_youtube_date(value: object) -> datetime | None:
    if not isinstance(value, str):
        return None
    try:
        return datetime.fromisoformat(value.replace("Z", "+00:00")).astimezone(timezone.utc)
    except ValueError:
        return None


def _video_description_summary(value: object) -> str:
    description = _plain_text(value)
    if not description:
        return "설명 정보가 제공되지 않았습니다."
    summary = " ".join(re.split(r"(?<=[.!?。！？])\s+", description)[:2])
    if len(summary) > 240:
        summary = summary[:237].rsplit(" ", 1)[0] + "..."
    return summary


def _within_period(published_at: object, period: str, *, youtube: bool = False) -> bool:
    cutoff = _date_cutoff(period)
    if cutoff is None:
        return True
    if youtube:
        published = _parse_youtube_date(published_at)
        return published is not None and published >= cutoff
    published = _parse_naver_date(published_at)
    return published is not None and published.date() >= cutoff.date()


def _companion_query(city: str, companion: str, *, youtube: bool) -> str:
    if youtube:
        companion_terms = {
            "아빠와 여행": "아빠와 여행",
            "엄마와 여행": "엄마와 여행",
            "가족여행": "가족여행",
            "아이와 여행": "아이와 여행",
            "연인과 여행": "연인과 여행",
            "혼자여행": "혼자 여행",
        }
        return f"{city} {companion_terms.get(companion, '여행')} 브이로그"
    return f"{city} {companion} 후기"


def _youtube_search_query(city: str, companion: str, travel_duration: str) -> str:
    companion_terms = {
        "아빠와 여행": "아빠와",
        "엄마와 여행": "엄마와",
        "가족여행": "가족",
        "아이와 여행": "아이와",
        "연인과 여행": "연인과",
        "혼자여행": "혼자",
    }
    duration_terms = {
        "당일치기": "당일치기",
        "1박 2일": "1박 2일",
        "2박 3일": "2박 3일",
        "3박 4일": "3박 4일",
    }
    companion_term = companion_terms.get(companion, companion)
    duration_term = duration_terms.get(travel_duration, travel_duration)
    return f"{city} {companion_term} {duration_term} 여행 브이로그"


def _video_duration_mentions(title: str, description: str) -> set[str]:
    text = re.sub(r"\s+", "", f"{title} {description}")
    patterns = {
        "당일치기": r"당일치기",
        "1박 2일": r"1박2일",
        "2박 3일": r"2박3일",
        "3박 4일": r"3박4일",
    }
    return {duration for duration, pattern in patterns.items() if re.search(pattern, text)}


def _video_duration_status(title: str, description: str, selected_duration: str) -> str:
    mentions = _video_duration_mentions(title, description)
    if len(mentions) > 1:
        return "comparison"
    if not mentions:
        return "unconfirmed"
    return "match" if selected_duration in mentions else "conflict"


def _video_mentions_destination(text: str, city: str) -> bool:
    compact_text = re.sub(r"\s+", "", text).casefold()
    city_parts = [re.sub(r"\s+", "", part).casefold() for part in city.split()]
    for part in city_parts:
        if part.endswith(("시", "군", "구", "읍", "면", "동")):
            part = part[:-1]
        if part and part not in compact_text:
            return False
    return bool(city_parts)


def _video_mentions_companion(text: str, companion: str) -> bool:
    compact_text = re.sub(r"\s+", "", text).casefold()
    companion_terms = {
        "혼자여행": ("혼자", "혼행", "나홀로", "솔로"),
        "가족여행": ("가족", "온가족"),
        "아이와 여행": ("아이와", "아이랑", "아이와함께", "아기", "자녀"),
        "아빠와 여행": ("아빠와", "아빠랑", "아버지와"),
        "엄마와 여행": ("엄마와", "엄마랑", "어머니와"),
        "연인과 여행": ("연인과", "커플", "데이트"),
    }
    terms = companion_terms.get(companion, (companion,))
    return any(re.sub(r"\s+", "", term).casefold() in compact_text for term in terms)


def _sort_videos(videos: list[Video], order: str) -> None:
    if order == "최신순":
        videos.sort(key=lambda item: item.get("published_at") or "", reverse=True)


def _http_json(
    url: str, *, params: dict[str, object], headers: dict[str, str] | None = None
) -> tuple[dict[str, object] | None, str | None]:
    try:
        response = requests.get(
            url, params=params, headers=headers, timeout=REQUEST_TIMEOUT
        )
    except requests.Timeout:
        return None, "API 응답 시간이 초과되었습니다. 잠시 후 다시 검색해 주세요."
    except requests.RequestException:
        return None, "네트워크 연결에 실패했습니다. 인터넷 연결을 확인해 주세요."

    if not response.ok:
        try:
            error_payload = response.json()
        except ValueError:
            error_payload = {}
        if "data.go.kr" in url and isinstance(error_payload, dict):
            service_response = error_payload.get("OpenAPI_ServiceResponse", {})
            message_header = (
                service_response.get("cmmMsgHeader", {})
                if isinstance(service_response, dict)
                else {}
            )
            reason_code = (
                message_header.get("returnReasonCode")
                if isinstance(message_header, dict)
                else None
            )
            if reason_code is not None:
                return None, f"한국관광공사 TourAPI 요청에 실패했습니다 (오류 코드 {reason_code})."
        error_data = error_payload.get("error", {}) if isinstance(error_payload, dict) else {}
        reasons: set[str] = set()
        error_info_reasons: set[str] = set()
        if isinstance(error_data, dict):
            raw_errors = error_data.get("errors", [])
            if isinstance(raw_errors, list):
                reasons = {
                    str(item.get("reason"))
                    for item in raw_errors
                    if isinstance(item, dict) and item.get("reason")
                }
            raw_details = error_data.get("details", [])
            if isinstance(raw_details, list):
                error_info_reasons = {
                    str(item.get("reason"))
                    for item in raw_details
                    if isinstance(item, dict)
                    and str(item.get("@type", "")).endswith("ErrorInfo")
                    and item.get("reason")
                }
        if reasons & {"quotaExceeded", "dailyLimitExceeded", "userRateLimitExceeded", "rateLimitExceeded"}:
            return None, "YouTube API 할당량을 초과했습니다. Google Cloud Console의 할당량을 확인해 주세요."
        if "kakao" in url and isinstance(error_payload, dict):
            kakao_error_type = str(error_payload.get("errorType") or "")
            if kakao_error_type == "NotAuthorizedError":
                return None, (
                    "카카오 앱에서 로컬 API 서비스가 비활성화되어 있습니다. "
                    "Kakao Developers의 앱 설정에서 Local 서비스를 사용 설정해 주세요."
                )
        if response.status_code in (401, 403) or "keyInvalid" in reasons:
            if "youtube" in url:
                if "API_KEY_SERVICE_BLOCKED" in error_info_reasons:
                    message = (
                        "YouTube Data API v3가 현재 API 키의 Google Cloud 프로젝트에서 차단되었습니다. "
                        "API 키가 My First Project 소속인지 확인하고, API 제한에서 YouTube Data API v3를 허용해 주세요."
                    )
                elif "accessNotConfigured" in reasons:
                    message = "YouTube Data API v3가 API 키의 Google Cloud 프로젝트에서 사용 설정되어 있는지 확인해 주세요."
                elif "keyInvalid" in reasons:
                    message = "YouTube API 키가 유효하지 않습니다. My First Project의 API 키와 secrets 설정을 대조해 주세요."
                else:
                    message = (
                        f"YouTube API 요청 권한이 거부되었습니다 (HTTP {response.status_code}, "
                        f"reason: {', '.join(sorted(reasons)) or 'unknown'}). "
                        "프로젝트 연결과 API 키 제한을 확인해 주세요."
                    )
            elif "naver" in url:
                message = "네이버 검색 API 인증에 실패했습니다. Client ID와 Client Secret을 확인해 주세요."
            elif "kakao" in url:
                message = "카카오 로컬 API 인증에 실패했습니다. REST API 키와 앱 설정을 확인해 주세요."
            elif "data.go.kr" in url:
                message = "한국관광공사 TourAPI 인증에 실패했습니다. Decoding 키와 활용 신청 상태를 확인해 주세요."
            else:
                message = "외부 API 인증에 실패했습니다. 해당 API 키와 사용 설정을 확인해 주세요."
            return None, message
        if response.status_code == 429:
            return None, "API 요청 한도를 초과했습니다. 잠시 후 다시 검색해 주세요."
        if response.status_code >= 500:
            return None, "외부 API 서비스에 일시적인 문제가 있습니다. 잠시 후 다시 검색해 주세요."
        return None, f"API 요청에 실패했습니다 (HTTP {response.status_code})."

    try:
        payload = response.json()
    except ValueError:
        return None, "API 응답을 JSON으로 읽을 수 없습니다. 인증키 또는 응답 형식을 확인해 주세요."
    if not isinstance(payload, dict):
        return None, "API 응답 형식이 예상과 다릅니다."
    return payload, None


def _tour_request(
    endpoint: str, api_key: str, params: dict[str, object]
) -> tuple[dict[str, object] | None, str | None]:
    service_key = (
        unquote(api_key)
        if re.search(r"%[0-9a-fA-F]{2}", api_key)
        else api_key
    )
    request_params: dict[str, object] = {
        "serviceKey": service_key,
        "MobileOS": "ETC",
        "MobileApp": "TripFinder",
        "_type": "json",
        **params,
    }
    payload, error = _http_json(endpoint, params=request_params)
    if error:
        return None, error

    response_data = payload.get("response", {}) if payload else {}
    if not isinstance(response_data, dict):
        return None, "한국관광공사 API 응답 형식이 예상과 다릅니다."
    header = response_data.get("header", {})
    if isinstance(header, dict):
        result_code = str(header.get("resultCode", "0000"))
        if result_code not in ("0000", "00", "0"):
            if result_code in ("0003", "03"):
                return {"items": {"item": []}}, None
            if result_code in ("0200", "0210", "0220", "0230", "30", "31"):
                return None, "한국관광공사 API 인증 또는 활용 신청 상태를 확인해 주세요."
            if result_code in ("0400", "429"):
                return None, "한국관광공사 API 요청 한도 또는 사용량을 확인해 주세요."
            return None, f"한국관광공사 API가 오류를 반환했습니다 ({result_code})."
    body = response_data.get("body", {})
    if not isinstance(body, dict):
        return None, "한국관광공사 API 응답 데이터가 비어 있거나 형식이 다릅니다."
    return body, None


def _as_items(body: dict[str, object]) -> list[dict[str, object]] | None:
    items = body.get("items", {})
    if items in (None, ""):
        return []
    if not isinstance(items, dict):
        return None
    raw_items = items.get("item", [])
    if raw_items in (None, ""):
        return []
    if isinstance(raw_items, dict):
        return [raw_items]
    if isinstance(raw_items, list):
        return [item for item in raw_items if isinstance(item, dict)]
    return None


def _compact_region(value: object) -> str:
    if not isinstance(value, str):
        return ""
    compact = re.sub(r"\s+", "", value)
    for full_name, short_name in (
        ("충청북도", "충북"),
        ("충청남도", "충남"),
        ("전라북도", "전북"),
        ("전라남도", "전남"),
        ("경상북도", "경북"),
        ("경상남도", "경남"),
    ):
        compact = compact.replace(full_name, short_name, 1)
    for suffix in (
        "특별자치도",
        "특별자치시",
        "특별시",
        "광역시",
        "자치구",
        "도",
        "시",
        "군",
        "구",
        "읍",
        "면",
        "동",
    ):
        if compact.endswith(suffix):
            compact = compact[: -len(suffix)]
            break
    return compact


@st.cache_data(ttl=86400, show_spinner=False)
def _tour_area_items_cached(
    endpoint: str, api_key: str, region_code: str = ""
) -> list[dict[str, object]]:
    params: dict[str, object] = {"numOfRows": 1000, "pageNo": 1}
    if region_code:
        params["lDongRegnCd"] = region_code
    body, error = _tour_request(endpoint, api_key, params)
    if error or not body:
        return []
    return _as_items(body) or []


def _resolve_tour_region(
    api_key: str | None, query: str, region1: str, region2: str
) -> tuple[str, str, str, str]:
    if not api_key:
        return "", "", "", ""

    area1_items = _tour_area_items_cached(TOUR_LDONG_URL, api_key)
    query_compact = _compact_region(query)
    selected_area: dict[str, object] | None = None
    for item in area1_items:
        name = str(item.get("name") or "")
        normalized_name = _compact_region(name)
        if region1 and normalized_name == _compact_region(region1):
            selected_area = item
            break
        if normalized_name and normalized_name in query_compact:
            if selected_area is None or len(normalized_name) > len(
                _compact_region(selected_area.get("name"))
            ):
                selected_area = item
    if selected_area is not None:
        area_code = str(selected_area.get("code") or "")
        area_name = str(selected_area.get("name") or "")
        if not area_code:
            return "", "", "", ""
        parent_compact = _compact_region(area_name)
        area2_items = _tour_area_items_cached(TOUR_LDONG_URL, api_key, area_code)
        target = _compact_region(region2)
        matching_sigungu = []
        for item in area2_items:
            normalized_name = _compact_region(item.get("name"))
            if target and (normalized_name == target or normalized_name in target or target in normalized_name):
                matching_sigungu.append(item)
            elif (
                not target
                and len(query_compact) > len(parent_compact)
                and normalized_name != parent_compact
                and normalized_name in query_compact
            ):
                matching_sigungu.append(item)
        selected_sigungu = max(
            matching_sigungu,
            key=lambda item: len(_compact_region(item.get("name"))),
            default=None,
        )
        return (
            "",
            "",
            area_name,
            str(selected_sigungu.get("name") or "") if selected_sigungu else "",
        )

    matching_regions: list[tuple[str, dict[str, object], dict[str, object]]] = []
    for area in area1_items:
        area_code = str(area.get("code") or "")
        if not area_code:
            continue
        for item in _tour_area_items_cached(TOUR_LDONG_URL, api_key, area_code):
            normalized_name = _compact_region(item.get("name"))
            if normalized_name and normalized_name in query_compact:
                matching_regions.append((normalized_name, area, item))
    if not matching_regions:
        return "", "", "", ""

    longest_match = max(len(match[0]) for match in matching_regions)
    best_matches = [match for match in matching_regions if len(match[0]) == longest_match]
    if len({str(match[1].get("code") or "") for match in best_matches}) != 1:
        return "", "", "", ""
    _, area, sigungu = best_matches[0]
    return (
        "",
        "",
        str(area.get("name") or ""),
        str(sigungu.get("name") or ""),
    )


def _kakao_location(item: dict[str, object], *, kind: str) -> Destination:
    address = item.get("address")
    road_address = item.get("road_address")
    address_data = address if isinstance(address, dict) else {}
    road_data = road_address if isinstance(road_address, dict) else {}
    region1 = str(
        address_data.get("region_1depth_name")
        or road_data.get("region_1depth_name")
        or item.get("region_1depth_name")
        or ""
    )
    region2 = str(
        address_data.get("region_2depth_name")
        or road_data.get("region_2depth_name")
        or item.get("region_2depth_name")
        or ""
    )
    region3 = str(
        address_data.get("region_3depth_name")
        or road_data.get("region_3depth_name")
        or item.get("region_3depth_name")
        or ""
    )
    address_text = str(
        item.get("address_name")
        or item.get("road_address_name")
        or address_data.get("address_name")
        or road_data.get("address_name")
        or ""
    )
    return {
        "name": str(item.get("place_name") or address_text or ""),
        "address": address_text,
        "x": str(item.get("x") or address_data.get("x") or ""),
        "y": str(item.get("y") or address_data.get("y") or ""),
        "region1": region1,
        "region2": region2,
        "region3": region3,
        "place_url": str(item.get("place_url") or ""),
        "kind": kind,
    }


def resolve_destination(
    query: str,
    kakao_api_key: str | None,
    tour_api_key: str | None = None,
    selected_region: str | None = None,
) -> SearchResult:
    """Resolve an administrative area or landmark without a built-in city list."""
    search_query = " ".join(part for part in (selected_region, query) if part).strip()
    if not kakao_api_key:
        if not tour_api_key:
            return {
                "status": "unresolved",
                "places": [],
                "message": "관광지 검색을 위한 TourAPI 키가 설정되지 않았습니다.",
                "location": {"name": search_query, "kind": "text"},
            }
        _, _, region1, region2 = _resolve_tour_region(
            tour_api_key, search_query, "", ""
        )
        region3_match = re.search(r"([가-힣]+(?:읍|면|동))", search_query)
        location: Destination = {
            "name": search_query,
            "kind": "region" if region1 or region3_match else "text",
            "region1": region1,
            "region2": region2,
        }
        if region3_match:
            location["region3"] = region3_match.group(1)
        return {
            "status": "success" if region1 or region3_match else "unresolved",
            "places": [],
            "message": "",
            "location": location,
        }

    keyword_payload, keyword_error = _http_json(
        KAKAO_KEYWORD_URL,
        params={"query": search_query, "size": 15},
        headers={"Authorization": f"KakaoAK {kakao_api_key}"},
    )
    if keyword_error:
        keyword_payload = None

    keyword_meta = keyword_payload.get("meta", {}) if keyword_payload else {}
    same_name = keyword_meta.get("same_name", {}) if isinstance(keyword_meta, dict) else {}
    raw_candidates = same_name.get("region", []) if isinstance(same_name, dict) else []
    candidates = [str(value) for value in raw_candidates if value] if isinstance(raw_candidates, list) else []
    if len(candidates) > 1 and not selected_region:
        return {
            "status": "ambiguous",
            "places": [],
            "candidates": candidates,
            "message": "같은 이름의 지역이 여러 곳에 있습니다. 검색할 지역을 선택해 주세요.",
        }

    address_payload, _ = _http_json(
        KAKAO_ADDRESS_URL,
        params={"query": search_query, "analyze_type": "similar", "size": 10},
        headers={"Authorization": f"KakaoAK {kakao_api_key}"},
    )
    address_documents = address_payload.get("documents", []) if address_payload else []
    location: Destination | None = None
    if isinstance(address_documents, list):
        for document in address_documents:
            if isinstance(document, dict):
                address_data = document.get("address")
                road_data = document.get("road_address")
                candidate_data = address_data if isinstance(address_data, dict) else road_data
                if isinstance(candidate_data, dict) and candidate_data.get("x") and candidate_data.get("y"):
                    location = _kakao_location(document, kind="region")
                    break

    if location is None and keyword_payload:
        documents = keyword_payload.get("documents", [])
        if isinstance(documents, list):
            query_compact = _compact_region(search_query)
            valid_documents = [item for item in documents if isinstance(item, dict) and item.get("x") and item.get("y")]
            valid_documents.sort(
                key=lambda item: (
                    _compact_region(item.get("place_name")) != query_compact,
                    not _compact_region(item.get("place_name", "")).startswith(query_compact),
                )
            )
            if valid_documents:
                location = _kakao_location(valid_documents[0], kind="place")

    if location is None:
        location = {"name": search_query, "kind": "text"}
    if tour_api_key:
        _, _, region1, region2 = _resolve_tour_region(
            tour_api_key,
            search_query,
            location.get("region1", ""),
            location.get("region2", ""),
        )
        if not location.get("region1"):
            location["region1"] = region1
        if not location.get("region2"):
            location["region2"] = region2
    if not location.get("region3"):
        region3_match = re.search(r"([가-힣]+(?:읍|면|동))", search_query)
        if region3_match:
            location["region3"] = region3_match.group(1)
    return {
        "status": "success" if location.get("x") and location.get("y") else "unresolved",
        "places": [],
        "location": location,
        "message": "" if location.get("x") else (keyword_error or "좌표를 찾지 못해 검색어 기반으로 계속 검색합니다."),
    }


def _detail_homepage(value: object) -> str | None:
    if not isinstance(value, str):
        return None
    match = re.search(r"href\s*=\s*['\"]([^'\"]+)['\"]", value, re.IGNORECASE)
    return unescape(match.group(1)) if match else None


def _recommendation_metadata(
    name: str, category: str, description: str
) -> tuple[list[str], list[str], list[str], list[str], str]:
    text = f"{name} {category} {description}"
    styles = ["관광 중심"] if category else []
    companions: list[str] = []
    tags: list[str] = []
    basis: list[str] = []

    if category:
        basis.append("관광지 유형")
    if description:
        basis.append("관광지 소개")
    if any(word in text for word in ("공원", "해변", "호수", "산책", "휴양", "자연")):
        styles.append("휴식 중심")
        tags.append("parent_pace_match")
    if any(word in text for word in ("체험", "박물관", "전시", "테마", "어린이")):
        styles.append("체험 중심")
    if any(word in text for word in ("어린이", "키즈", "가족 체험")):
        companions.extend(("가족여행", "아이와 여행"))
        tags.append("child_activity")

    reason = (
        "관광지 유형과 소개 문구를 기준으로 정렬합니다. 편의시설과 접근성은 확인되지 않았습니다."
        if basis
        else "유형·소개 정보가 부족해 동행자 맞춤 추천 근거가 제한적입니다."
    )
    return styles, companions, tags, basis, reason


def _tour_place(item: dict[str, object], detail: dict[str, object] | None) -> Place:
    name = str(item.get("title") or "관광지명 미제공")
    content_type = str(item.get("contenttypeid") or "")
    category_names = {
        "12": "관광지",
        "14": "문화시설",
        "15": "축제·행사",
        "25": "여행코스",
        "28": "레포츠",
    }
    category_code = str(item.get("cat1") or "")
    category_labels = {
        "A01": "자연 관광지",
        "A02": "문화·역사 관광지",
        "A03": "레포츠",
        "A07": "축제·행사",
        "A08": "여행코스",
    }
    category = category_labels.get(
        category_code, category_names.get(content_type, "관광지 유형 미제공")
    )
    description = _plain_text((detail or {}).get("overview") or item.get("overview"))
    styles, companions, tags, basis, reason = _recommendation_metadata(
        name, category, description
    )
    address_parts = [str(item.get(key) or "").strip() for key in ("addr1", "addr2")]
    address = " ".join(part for part in address_parts if part)
    image_url = item.get("firstimage") or item.get("firstimage2")
    return {
        "name": name,
        "x": str(item.get("mapx") or ""),
        "y": str(item.get("mapy") or ""),
        "region": address or str(item.get("areacode") or "지역 정보 미제공"),
        "category": category,
        "recommended_companions": companions,
        "travel_styles": styles,
        "description": description,
        "recommendation_reason": reason,
        "image_url": str(image_url) if image_url else None,
        "source": "한국관광공사 TourAPI",
        "verified": False,
        "address": address,
        "detail_url": _detail_homepage((detail or {}).get("homepage")),
        "recommendation_tags": tags,
        "recommendation_basis": basis,
        "data_kind": "actual",
    }


def _tour_detail(content_id: object, api_key: str) -> dict[str, object] | None:
    if not content_id:
        return None
    body, _ = _tour_request(
        TOUR_DETAIL_URL,
        api_key,
        {
            "contentId": str(content_id),
            "defaultYN": "Y",
            "firstImageYN": "N",
            "areacodeYN": "N",
            "catcodeYN": "N",
            "addrinfoYN": "N",
            "mapinfoYN": "N",
            "overviewYN": "Y",
            "numOfRows": 1,
            "pageNo": 1,
        },
    )
    if not body:
        return None
    items = _as_items(body)
    return items[0] if items else None


def _distance_meters(x1: object, y1: object, x2: object, y2: object) -> float | None:
    try:
        lon1, lat1, lon2, lat2 = map(float, (x1, y1, x2, y2))
    except (TypeError, ValueError):
        return None
    earth_radius = 6_371_000
    latitude_delta = radians(lat2 - lat1)
    longitude_delta = radians(lon2 - lon1)
    value = (
        sin(latitude_delta / 2) ** 2
        + cos(radians(lat1)) * cos(radians(lat2)) * sin(longitude_delta / 2) ** 2
    )
    return earth_radius * 2 * asin(min(1, sqrt(value)))


@st.cache_data(ttl=1800, show_spinner=False)
def _search_tourism_cached(
    query: str,
    api_key: str,
    area_code: str,
    sigungu_code: str,
    x: str,
    y: str,
    location_kind: str,
    region1: str,
    region2: str,
    region3: str,
    max_results: int,
) -> SearchResult:
    area_params: dict[str, object] = {}
    if area_code:
        area_params["areaCode"] = area_code
    if sigungu_code:
        area_params["sigunguCode"] = sigungu_code
    # 여행 기간에 따라 검색 결과를 여러 페이지에서 확보합니다.
    target = max(1, min(int(max_results), MAX_TOUR_RESULTS))
    search_items: list[dict[str, object]] = []
    seen_ids: set[str] = set()
    errors: list[str] = []

    def collect(endpoint: str, params: dict[str, object]) -> None:
        for page in range(1, (target + TOUR_PAGE_SIZE - 1) // TOUR_PAGE_SIZE + 2):
            body, error = _tour_request(
                endpoint, api_key,
                {**params, "numOfRows": TOUR_PAGE_SIZE, "pageNo": page},
            )
            if error:
                errors.append(error)
                break
            items = _as_items(body or {})
            if items is None:
                errors.append("관광지 검색 결과 형식이 올바르지 않습니다.")
                break
            if not items:
                break
            for item in items:
                item_id = str(item.get("contentid") or "")
                if not item_id:
                    item_id = str(item.get("title") or "") + str(item.get("addr1") or "")
                if item_id and item_id not in seen_ids:
                    seen_ids.add(item_id)
                    search_items.append(item)
            if len(items) < TOUR_PAGE_SIZE or len(search_items) >= target * 2:
                break

    collect(TOUR_SEARCH_URL, {
        "keyword": query, "contentTypeId": 12, "arrange": "Q", **area_params,
    })
    area_endpoint = (
        TOUR_AREA_BASED_URL
        if area_code and location_kind == "region" and not region3
        else None
    )
    nearby_endpoint = TOUR_LOCATION_BASED_URL if x and y else None
    fallback_endpoint = area_endpoint or nearby_endpoint
    if fallback_endpoint and len(search_items) < target * 2:
        fallback_params: dict[str, object] = {
            "contentTypeId": 12, "arrange": "Q", **area_params,
        }
        if fallback_endpoint == TOUR_LOCATION_BASED_URL:
            fallback_params.update({
                "mapX": x, "mapY": y,
                "radius": 10000 if region3 else 20000,
            })
        collect(fallback_endpoint, fallback_params)

    places = []
    for item in search_items:
        if x and y and (location_kind != "region" or region3):
            distance = _distance_meters(x, y, item.get("mapx"), item.get("mapy"))
            if distance is None or distance > (10000 if region3 else 20000):
                continue
        if location_kind == "region":
            address = " ".join(
                str(item.get(key) or "") for key in ("addr1", "addr2")
            )
            normalized_address = _compact_region(address)
            if any(
                _compact_region(region) not in normalized_address
                for region in (region1, region2, region3)
                if region
            ):
                continue
        detail = _tour_detail(item.get("contentid"), api_key) if len(places) < TOUR_DETAIL_LIMIT else None
        places.append(_tour_place(item, detail))
        if len(places) >= target:
            break
    if not places and errors:
        return {"status": "error", "places": [], "message": errors[0]}
    if not places:
        return {
            "status": "empty",
            "places": [],
            "message": "해당 지역의 관광지 검색 결과가 없습니다.",
        }
    return {"status": "success", "places": places, "message": ("일부 관광지 검색 요청에 실패해 확인된 장소만 표시합니다." if errors else "")}


def search_tourism(
    query: str,
    api_key: str | None,
    location: Destination | None = None,
    max_results: int = 6,
) -> SearchResult:
    if not api_key:
        return {
            "status": "not_configured",
            "places": [],
            "message": "TourAPI 인증키가 설정되지 않았습니다.",
        }
    resolved = location or {}
    return _search_tourism_cached(
        query.strip(),
        api_key,
        str(resolved.get("area_code") or ""),
        str(resolved.get("sigungu_code") or ""),
        str(resolved.get("x") or ""),
        str(resolved.get("y") or ""),
        str(resolved.get("kind") or "text"),
        str(resolved.get("region1") or ""),
        str(resolved.get("region2") or ""),
        str(resolved.get("region3") or ""),
        max_results,
    )


def _local_place(item: dict[str, object]) -> Place:
    name = str(item.get("place_name") or "음식점 이름 미제공")
    category = str(item.get("category_name") or "음식점 유형 미제공")
    road_address = str(item.get("road_address_name") or "").strip()
    address = road_address or str(item.get("address_name") or "").strip()
    region = " ".join(
        str(item.get(key) or "").strip()
        for key in ("region_1depth_name", "region_2depth_name")
        if item.get(key)
    )
    return {
        "name": name,
        "region": region or address or "지역 정보 미제공",
        "category": category,
        "recommended_companions": [],
        "travel_styles": ["맛집 중심"] if item.get("category_name") else [],
        "description": "",
        "recommendation_reason": "카카오 로컬 API 음식점 분류와 지역 정보를 표시합니다.",
        "image_url": None,
        "source": "카카오 로컬 API",
        "verified": False,
        "address": address,
        "phone": str(item.get("phone") or ""),
        "detail_url": str(item.get("place_url") or "") or None,
        "recommendation_tags": [],
        "recommendation_basis": ["음식점 분류"] if item.get("category_name") else [],
        "data_kind": "actual",
    }


@st.cache_data(ttl=900, show_spinner=False)
def _search_restaurants_cached(
    query: str,
    api_key: str,
    selected_region: str | None,
    x: str,
    y: str,
    radius: int,
    region1: str,
    region2: str,
    region3: str,
    location_kind: str,
) -> SearchResult:
    search_query = query
    if selected_region:
        search_query = selected_region
        if query not in selected_region:
            search_query = f"{selected_region} {query}"
    use_region_filter = location_kind == "region" and any(
        (region1, region2, region3)
    )
    region_query = " ".join(part for part in (region1, region2, region3) if part)
    params: dict[str, object] = {
        "query": f"{region_query} 음식점" if use_region_filter else (
            "음식점" if x and y else f"{search_query} 음식점"
        ),
        "category_group_code": "FD6",
        "size": 15,
    }
    if x and y and not use_region_filter:
        params.update({"x": x, "y": y, "radius": radius, "sort": "distance"})
    keyword_payload, keyword_error = _http_json(
        KAKAO_KEYWORD_URL,
        params=params,
        headers={"Authorization": f"KakaoAK {api_key}"},
    )
    category_payload = None
    category_error = None
    if x and y:
        category_payload, category_error = _http_json(
            KAKAO_CATEGORY_URL,
            params={
                "category_group_code": "FD6",
                "x": x,
                "y": y,
                "radius": radius,
                "sort": "distance",
                "size": 15,
            },
            headers={"Authorization": f"KakaoAK {api_key}"},
        )

    if keyword_payload is None and category_payload is None:
        return {
            "status": "error",
            "places": [],
            "message": keyword_error or category_error or "카카오 로컬 API 검색에 실패했습니다.",
        }

    keyword_documents = keyword_payload.get("documents", []) if keyword_payload else []
    category_documents = category_payload.get("documents", []) if category_payload else []
    meta = keyword_payload.get("meta", {}) if keyword_payload else {}
    if not isinstance(keyword_documents, list) or not isinstance(category_documents, list) or not isinstance(meta, dict):
        return {
            "status": "error",
            "places": [],
            "message": "카카오 로컬 API 응답 데이터 형식이 예상과 다릅니다.",
        }

    same_name = meta.get("same_name", {})
    regions = same_name.get("region", []) if isinstance(same_name, dict) else []
    candidates = [str(region) for region in regions if region] if isinstance(regions, list) else []
    if len(candidates) > 1 and not selected_region:
        return {
            "status": "ambiguous",
            "places": [],
            "candidates": candidates,
            "message": "같은 지역명이 여러 곳에서 검색되어 정확한 지역을 선택해 주세요.",
        }

    documents_by_id: dict[str, dict[str, object]] = {}
    for item in [*keyword_documents, *category_documents]:
        if not isinstance(item, dict):
            continue
        item_id = str(item.get("id") or item.get("place_url") or "")
        if not item_id:
            item_id = " ".join(
                str(item.get(key) or "").strip()
                for key in ("place_name", "address_name", "road_address_name")
            )
        if item_id:
            documents_by_id.setdefault(item_id, item)

    places = []
    for item in documents_by_id.values():
        if use_region_filter:
            address_text = " ".join(
                str(item.get(key) or "")
                for key in ("address_name", "road_address_name")
            )
            matches_region = True
            for expected, key in (
                (region1, "region_1depth_name"),
                (region2, "region_2depth_name"),
                (region3, "region_3depth_name"),
            ):
                if not expected:
                    continue
                expected_name = _compact_region(expected)
                actual_name = _compact_region(item.get(key))
                candidate_name = actual_name or _compact_region(address_text)
                if expected_name and expected_name not in candidate_name and candidate_name not in expected_name:
                    matches_region = False
                    break
            if not matches_region:
                continue
        distance = item.get("distance")
        if x and y:
            try:
                distance_meters = int(str(distance)) if distance not in (None, "") else None
            except ValueError:
                distance_meters = None
            if distance_meters is None:
                distance_meters = _distance_meters(
                    x, y, item.get("x"), item.get("y")
                )
            if distance_meters is None or distance_meters > radius:
                continue
        places.append(_local_place(item))
    if not places:
        if keyword_error and category_error:
            message = keyword_error
        elif category_error:
            message = f"카테고리 검색에 실패해 키워드 결과만 확인했습니다: {category_error}"
        else:
            message = "해당 지역의 주변 음식점 검색 결과가 없습니다."
        return {
            "status": "empty",
            "places": [],
            "message": message,
        }
    message = (
        f"카테고리 검색에 실패해 키워드 결과만 표시합니다: {category_error}"
        if category_error
        else ""
    )
    return {"status": "success", "places": places, "message": message}


def search_restaurants(
    query: str,
    api_key: str | None,
    selected_region: str | None = None,
    location: Destination | None = None,
) -> SearchResult:
    if not api_key:
        return {
            "status": "not_configured",
            "places": [],
            "message": "카카오 로컬 API REST 키가 설정되지 않았습니다.",
        }
    resolved = location or {}
    radius = 5000 if resolved.get("kind") == "place" else 20000
    return _search_restaurants_cached(
        query.strip(),
        api_key,
        selected_region,
        str(resolved.get("x") or ""),
        str(resolved.get("y") or ""),
        radius,
        str(resolved.get("region1") or ""),
        str(resolved.get("region2") or ""),
        str(resolved.get("region3") or ""),
        str(resolved.get("kind") or "text"),
    )


@st.cache_data(ttl=900, show_spinner=False)
def _kakao_category_items_cached(
    category_code: str,
    x: str,
    y: str,
    radius: int,
    api_key: str,
) -> tuple[list[dict[str, object]], str | None]:
    payload, error = _http_json(
        KAKAO_CATEGORY_URL,
        params={
            "category_group_code": category_code,
            "x": x,
            "y": y,
            "radius": radius,
            "sort": "distance",
            "size": 15,
        },
        headers={"Authorization": f"KakaoAK {api_key}"},
    )
    if error:
        return [], error
    documents = payload.get("documents", []) if payload else []
    if not isinstance(documents, list):
        return [], "카카오 로컬 API 카테고리 검색 응답 형식이 예상과 다릅니다."
    return [item for item in documents if isinstance(item, dict)], None


@st.cache_data(ttl=86400, show_spinner=False)
def _kakao_geocode_address_cached(
    address: str, api_key: str
) -> tuple[Destination | None, str | None]:
    payload, error = _http_json(
        KAKAO_ADDRESS_URL,
        params={"query": address, "analyze_type": "similar", "size": 5},
        headers={"Authorization": f"KakaoAK {api_key}"},
    )
    if error:
        return None, error
    documents = payload.get("documents", []) if payload else []
    if not isinstance(documents, list):
        return None, "카카오 주소 검색 응답 형식이 예상과 다릅니다."
    for item in documents:
        if not isinstance(item, dict):
            continue
        candidate = _kakao_location(item, kind="region")
        if candidate.get("x") and candidate.get("y"):
            return candidate, None
    return None, None


def _kakao_result_matches_region(
    item: dict[str, object], region1: str, region2: str, region3: str
) -> bool:
    address = " ".join(
        str(item.get(field) or "") for field in ("address_name", "road_address_name")
    )
    for expected, field in (
        (region1, "region_1depth_name"),
        (region2, "region_2depth_name"),
        (region3, "region_3depth_name"),
    ):
        if not expected:
            continue
        expected_name = _compact_region(expected)
        actual_name = _compact_region(item.get(field)) or _compact_region(address)
        if expected_name and expected_name not in actual_name and actual_name not in expected_name:
            return False
    return True


def search_nearby_places(
    attractions: list[Place],
    api_key: str | None,
    location: Destination | None,
    transport: str,
    travel_style: str,
) -> NearbySearchResult:
    if not api_key:
        return {
            "status": "not_configured",
            "groups": [],
            "restaurants": [],
            "cafes": [],
            "message": "카카오 로컬 API 키가 없어 관광지 주변 음식점·카페를 검색하지 않았습니다.",
        }
    if not attractions:
        return {
            "status": "empty",
            "groups": [],
            "restaurants": [],
            "cafes": [],
            "message": "검색된 관광지가 없어 주변 장소를 조회하지 않았습니다.",
        }

    resolved = location or {}
    region1 = str(resolved.get("region1") or "")
    region2 = str(resolved.get("region2") or "")
    region3 = str(resolved.get("region3") or "")
    radii = (800, 1500, 3000) if transport == "대중교통" else (1500, 3000, 5000)
    restaurant_target = 2 if travel_style == "맛집 중심" else 1
    cafe_target = 2 if travel_style == "휴식 중심" else 1
    category_specs = (
        ("FD6", "restaurants", restaurant_target, "음식점"),
        ("CE7", "cafes", cafe_target, "카페"),
    )
    groups: list[NearbyPlaceGroup] = []
    all_restaurants: list[Place] = []
    all_cafes: list[Place] = []
    seen_ids: dict[str, set[str]] = {"FD6": set(), "CE7": set()}
    errors: list[str] = []
    skipped_attractions = 0

    for attraction in attractions:
        x = str(attraction.get("x") or "")
        y = str(attraction.get("y") or "")
        attraction_address = str(attraction.get("address") or "").strip()
        if (not x or not y) and attraction_address:
            geocoded, geocode_error = _kakao_geocode_address_cached(
                attraction_address, api_key
            )
            if geocoded:
                x = str(geocoded.get("x") or "")
                y = str(geocoded.get("y") or "")
                region1 = region1 or str(geocoded.get("region1") or "")
                region2 = region2 or str(geocoded.get("region2") or "")
                region3 = region3 or str(geocoded.get("region3") or "")
            elif geocode_error:
                errors.append(geocode_error)
        if not x or not y:
            skipped_attractions += 1
            groups.append({"attraction": attraction, "restaurants": [], "cafes": []})
            continue

        group: NearbyPlaceGroup = {
            "attraction": attraction,
            "restaurants": [],
            "cafes": [],
        }
        for category_code, group_key, target_count, kind_label in category_specs:
            selected_places: list[Place] = []
            for radius in radii:
                documents, error = _kakao_category_items_cached(
                    category_code, x, y, radius, api_key
                )
                if error:
                    errors.append(error)
                    break
                for item in documents:
                    place_id = str(item.get("id") or item.get("place_url") or "")
                    if not place_id:
                        continue
                    if place_id in seen_ids[category_code]:
                        continue
                    if not _kakao_result_matches_region(item, region1, region2, region3):
                        continue
                    distance = _distance_meters(x, y, item.get("x"), item.get("y"))
                    if distance is None or distance > radius:
                        continue
                    place = _local_place(item)
                    place["x"] = str(item.get("x") or "")
                    place["y"] = str(item.get("y") or "")
                    place["distance_meters"] = round(distance)
                    place["nearby_category"] = kind_label
                    place["recommendation_reason"] = (
                        f"관광지 좌표 기준 반경 {radius}m 내 {kind_label} 검색 결과입니다. "
                        f"직선거리 참고: 약 {round(distance)}m이며 실제 경로·이동시간은 확인되지 않았습니다."
                    )
                    seen_ids[category_code].add(place_id)
                    selected_places.append(place)
                    if len(selected_places) >= target_count:
                        break
                if len(selected_places) >= target_count:
                    break
            group[group_key] = selected_places
            if group_key == "restaurants":
                all_restaurants.extend(selected_places)
            else:
                all_cafes.extend(selected_places)
        groups.append(group)

    total = len(all_restaurants) + len(all_cafes)
    if total:
        status = "partial" if errors else "success"
        message = (
            "일부 주변 장소 API 요청에 실패했습니다. 확인된 실제 결과만 일정에 사용합니다."
            if errors
            else ""
        )
    elif errors:
        status = "error"
        message = "주변 장소 검색에 실패했습니다. 관광지 일정은 계속 표시합니다."
    else:
        status = "empty"
        message = "주변 장소 검색 결과 없음"
    if skipped_attractions:
        message = (message + " " if message else "") + (
            f"관광지 좌표를 얻지 못해 {skipped_attractions}곳은 주변 검색을 건너뛰었습니다."
        )
    return {
        "status": status,
        "groups": groups,
        "restaurants": all_restaurants,
        "cafes": all_cafes,
        "message": message,
        "skipped_attractions": skipped_attractions,
    }


def _naver_blog_items(
    keyword: str,
    client_id: str,
    client_secret: str,
    period: str,
    expanded: bool,
) -> tuple[list[Review], str | None]:
    payload, error = _http_json(
        NAVER_BLOG_URL,
        params={"query": keyword, "display": CONTENT_SEARCH_LIMIT, "sort": "date"},
        headers={
            "X-Naver-Client-Id": client_id,
            "X-Naver-Client-Secret": client_secret,
        },
    )
    if error:
        return [], error
    raw_items = payload.get("items", []) if payload else []
    if not isinstance(raw_items, list):
        return [], "네이버 블로그 API 응답 데이터 형식이 예상과 다릅니다."

    reviews: list[Review] = []
    for raw_item in raw_items:
        if not isinstance(raw_item, dict):
            continue
        postdate = raw_item.get("postdate")
        if not _within_period(postdate, period):
            continue
        title = _plain_text(raw_item.get("title"))
        summary = _plain_text(raw_item.get("description"))
        link = _safe_url(raw_item.get("link"))
        if not title or not link:
            continue
        parsed_date = _parse_naver_date(postdate)
        reviews.append(
            {
                "title": title,
                "summary": summary or "네이버 검색 API에서 요약을 제공하지 않았습니다.",
                "published_at": parsed_date.strftime("%Y-%m-%d") if parsed_date else None,
                "source_name": _plain_text(raw_item.get("bloggername")) or "네이버 블로그",
                "url": link,
                "search_keyword": keyword,
                "expanded_search": expanded,
                "content_kind": "actual",
            }
        )
    return reviews, None


@st.cache_data(ttl=CONTENT_CACHE_TTL, show_spinner=False)
def _search_blogs_cached(
    city: str,
    companion: str,
    period: str,
    client_id: str,
    client_secret: str,
) -> ContentSearchResult:
    primary_keyword = _companion_query(city, companion, youtube=False)
    primary_items, error = _naver_blog_items(
        primary_keyword, client_id, client_secret, period, False
    )
    if error:
        return {"status": "error", "items": [], "message": error, "primary_keyword": primary_keyword}

    items = list(primary_items)
    expanded = False
    if len(items) < CONTENT_PAGE_SIZE:
        fallback_keyword = f"{city} 여행 후기"
        fallback_items, error = _naver_blog_items(
            fallback_keyword, client_id, client_secret, period, True
        )
        if error:
            return {
                "status": "success" if items else "error",
                "items": items,
                "message": f"일반 여행 후기 확장 검색에 실패했습니다: {error}",
                "primary_keyword": primary_keyword,
            }
        existing_urls = {item.get("url") for item in items}
        added_items = []
        for item in fallback_items:
            item_url = item.get("url")
            if item_url and item_url not in existing_urls:
                added_items.append(item)
                existing_urls.add(item_url)
        items.extend(added_items)
        expanded = bool(added_items)

    items.sort(key=lambda item: item.get("published_at") or "", reverse=True)
    return {
        "status": "success" if items else "empty",
        "items": items,
        "message": "" if items else "선택한 기간에 관련 블로그 게시글이 없습니다.",
        "primary_keyword": primary_keyword,
        "expanded": expanded,
    }


def search_blogs(
    city: str,
    companion: str,
    period: str,
    client_id: str | None,
    client_secret: str | None,
) -> ContentSearchResult:
    keyword = _companion_query(city, companion, youtube=False)
    if not client_id or not client_secret:
        return {
            "status": "not_configured",
            "items": [],
            "primary_keyword": keyword,
            "message": "네이버 검색 API 키가 설정되지 않았습니다. API_SETUP.md를 확인해 주세요.",
        }
    return _search_blogs_cached(city.strip(), companion, period, client_id, client_secret)


def _youtube_blog_items(
    keyword: str,
    api_key: str,
    period: str,
    order: str,
    travel_duration: str,
    city: str,
    companion: str,
) -> tuple[list[Video], list[Video], list[Video], str | None]:
    params: dict[str, object] = {
        "part": "snippet",
        "q": keyword,
        "type": "video",
        "order": "date" if order == "최신순" else "relevance",
        "maxResults": CONTENT_PAGE_SIZE,
        "regionCode": "KR",
        "relevanceLanguage": "ko",
        "key": api_key,
    }
    cutoff = _date_cutoff(period)
    if cutoff:
        params["publishedAfter"] = cutoff.isoformat(timespec="seconds").replace("+00:00", "Z")
    payload, error = _http_json(YOUTUBE_SEARCH_URL, params=params)
    if error:
        return [], [], [], error
    raw_items = payload.get("items", []) if payload else []
    if not isinstance(raw_items, list):
        return [], [], [], "YouTube Data API 응답 데이터 형식이 예상과 다릅니다."

    videos: list[Video] = []
    unconfirmed_videos: list[Video] = []
    comparison_videos: list[Video] = []
    for raw_item in raw_items:
        if not isinstance(raw_item, dict):
            continue
        video_id = raw_item.get("id", {})
        snippet = raw_item.get("snippet", {})
        if not isinstance(video_id, dict) or not isinstance(snippet, dict):
            continue
        identifier = video_id.get("videoId")
        if not isinstance(identifier, str) or not identifier.strip():
            continue
        published = snippet.get("publishedAt")
        if not _within_period(published, period, youtube=True):
            continue
        title = _plain_text(snippet.get("title")) or "영상 제목 미제공"
        description = _video_description_summary(snippet.get("description"))
        duration_text = _plain_text(snippet.get("description"))
        if not _video_mentions_destination(f"{title} {duration_text}", city):
            continue
        duration_status = _video_duration_status(title, duration_text, travel_duration)
        if duration_status == "conflict":
            continue
        companion_match = _video_mentions_companion(f"{title} {duration_text}", companion)
        thumbnails = snippet.get("thumbnails", {})
        if not isinstance(thumbnails, dict):
            thumbnails = {}
        thumbnail = None
        for size in ("high", "medium", "default"):
            image_data = thumbnails.get(size, {})
            if isinstance(image_data, dict):
                thumbnail = _safe_url(image_data.get("url"))
                if thumbnail:
                    break
        published_date = _parse_youtube_date(published)
        video: Video = {
            "title": title,
            "published_at": published_date.strftime("%Y-%m-%d") if published_date else None,
            "thumbnail_url": thumbnail,
            "channel_name": _plain_text(snippet.get("channelTitle")) or "채널명 미제공",
            "description": description,
            "url": f"https://www.youtube.com/watch?v={identifier}",
            "search_keyword": keyword,
            "content_kind": "actual",
            "travel_duration_status": duration_status,
            "companion_match": companion_match,
        }
        if duration_status == "match":
            video["match_priority"] = 1 if companion_match else 2
            videos.append(video)
        elif duration_status == "unconfirmed":
            video["match_priority"] = 3
            unconfirmed_videos.append(video)
        else:
            comparison_videos.append(video)
    if order == "최신순":
        videos.sort(key=lambda item: item.get("published_at") or "", reverse=True)
    videos.sort(key=lambda item: item.get("match_priority", 99))
    _sort_videos(unconfirmed_videos, order)
    _sort_videos(comparison_videos, order)
    return videos, unconfirmed_videos, comparison_videos, None


@st.cache_data(ttl=CONTENT_CACHE_TTL, show_spinner=False)
def _search_videos_cached(
    city: str,
    companion: str,
    period: str,
    order: str,
    travel_duration: str,
    api_key: str,
) -> ContentSearchResult:
    primary_keyword = _youtube_search_query(city, companion, travel_duration)
    primary_items, unconfirmed_items, comparison_items, error = _youtube_blog_items(
        primary_keyword, api_key, period, order, travel_duration, city, companion
    )
    if error:
        return {"status": "error", "items": [], "message": error, "primary_keyword": primary_keyword}

    has_videos = bool(primary_items or unconfirmed_items or comparison_items)
    has_matching_duration = bool(primary_items)
    return {
        "status": "success" if has_videos else "empty",
        "items": primary_items,
        "unconfirmed_items": unconfirmed_items,
        "comparison_items": comparison_items,
        "message": "" if has_matching_duration else "선택하신 여행 기간에 맞는 영상을 찾지 못했습니다.",
        "primary_keyword": primary_keyword,
    }


def search_videos(
    city: str,
    companion: str,
    period: str,
    order: str,
    api_key: str | None,
    travel_duration: str = "당일치기",
) -> ContentSearchResult:
    keyword = _youtube_search_query(city, companion, travel_duration)
    if not api_key:
        return {
            "status": "not_configured",
            "items": [],
            "primary_keyword": keyword,
            "message": "YouTube Data API 키가 설정되지 않았습니다. API_SETUP.md를 확인해 주세요.",
        }
    selected_order = order if order in VIDEO_ORDER_OPTIONS else "관련도순"
    return _search_videos_cached(
        city.strip(), companion, period, selected_order, travel_duration, api_key
    )


def companion_match_is_limited(place: Place) -> bool:
    return not place.get("recommendation_basis") or not place.get(
        "recommended_companions"
    )