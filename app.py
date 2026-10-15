from html import escape
from copy import deepcopy
from math import isfinite
from pathlib import Path
from urllib.parse import quote, urlencode
from urllib.request import Request, urlopen
from urllib.error import HTTPError, URLError
import json
import requests
from io import BytesIO
from datetime import datetime

import folium
import streamlit as st
from streamlit_folium import st_folium

from trip_finder.api import (
    CONTENT_PAGE_SIZE,
    PERIOD_OPTIONS,
    ContentSearchResult,
    companion_match_is_limited,
    get_kakao_api_key,
    get_naver_api_credentials,
    get_tour_api_key,
    get_youtube_api_key,
    VIDEO_ORDER_OPTIONS,
    search_blogs,
    resolve_destination,
    search_restaurants,
    search_nearby_places,
    search_tourism,
    search_videos,
)
from trip_finder.data import (
    CHILD_AGE_GROUPS,
    COMPANIONS,
    TRAVEL_DURATIONS,
    TRAVEL_STYLES,
    TRANSPORT_MODES,
    Place,
)


ASSET_DIR = Path(__file__).resolve().parent
# 직접 입력한 여행 일수와 이전 버전에서 저장한 여행 기간을 모두 지원합니다.
DURATION_DAYS = {
    "당일치기": 1,
    "1박 2일": 2,
    "2박 3일": 3,
    "3박 4일": 4,
}
MAX_TRAVEL_DAYS = 30

# 5-7: 간결한 동행 유형과 여행 구성에 따른 일정 추천
TRAVEL_PARTIES = ("혼자 여행", "연인·친구와 여행", "우리 가족 여행", "부모님과 여행", "3대 가족 여행")
FAMILY_CHILD_OPTIONS = ("아이 없음", "영유아 (0~5세)", "초등학생 (6~12세)", "중·고등학생")
CHILD_AGE_OPTIONS = ("0~2세", "3~5세", "6~9세", "10~12세", "13~15세", "16~18세", "19세 이상")


def child_age_bucket(age: str) -> str:
    if age in ("0~2세", "3~5세"):
        return "영유아 (0~5세)"
    if age in ("6~9세", "10~12세"):
        return "초등학생 (6~12세)"
    if age in ("13~15세", "16~18세"):
        return "중·고등학생"
    return "성인"


def child_profiles_score(place: Place, ages: list[str]) -> int:
    """여러 연령대가 함께 즐길 수 있는 장소를 우선합니다. 실제 입장 연령 제한 판정은 아닙니다."""
    if not ages:
        return 0
    text = " ".join(str(place.get(k) or "") for k in ("name", "category", "description"))
    recommended = place.get("recommended_age_groups", []) or []
    if isinstance(recommended, str):
        recommended = [recommended]
    tags = place.get("recommendation_tags", []) or []
    if isinstance(tags, str):
        tags = [tags]
    scores = []
    for age in ages:
        bucket = child_age_bucket(age)
        score = 0
        if bucket in recommended or age in recommended:
            score += 12
        if bucket == "영유아 (0~5세)":
            if any(w in text for w in ("수족관", "아쿠아리움", "동물", "공원", "정원", "키즈", "어린이", "유아")):
                score += 14
            if any(w in text for w in ("등산", "암벽", "트레킹", "익스트림", "급경사")):
                score -= 22
            if "child_friendly" in tags or "easy_access" in tags:
                score += 8
        elif bucket == "초등학생 (6~12세)":
            if any(w in text for w in ("체험", "과학", "박물관", "동물", "수족관", "놀이", "자연", "전시")):
                score += 13
        elif bucket == "중·고등학생":
            if any(w in text for w in ("체험", "역사", "박물관", "전망", "케이블카", "전시", "레저", "과학")):
                score += 13
            if any(w in text for w in ("유아전용", "영아전용", "키즈카페")):
                score -= 10
        else:
            if any(w in text for w in ("역사", "미술", "전시", "박물관", "문화", "전망", "산책")):
                score += 10
        scores.append(score)
    # 한 아이에게만 맞는 장소보다 전원이 함께 즐길 수 있는 장소를 우선합니다.
    return round(0.65 * min(scores) + 0.35 * sum(scores) / len(scores))



def companion_for_media(party: str, child_group: str | None) -> str:
    """기존 후기/영상 API의 동행 유형과 호환합니다."""
    if party == "혼자 여행":
        return "혼자여행"
    if party == "연인·친구와 여행":
        return "연인과 여행"
    if party == "부모님과 여행":
        return "엄마와 여행"
    if party == "3대 가족 여행":
        return "가족여행"
    return "아이와 여행" if child_group and child_group != "아이 없음" else "가족여행"


def attractions_per_day_for(party: str, style: str, child_group: str | None) -> int:
    """스타일별 관광지 목표 수. 영유아·부모님 동행은 방문 부담을 줄입니다."""
    if style == "관광 중심":
        count = 3
    elif style == "체험 중심":
        count = 2
    else:
        count = 1
    if party in ("부모님과 여행", "3대 가족 여행") or (child_group and "영유아 (0~5세)" in child_group):
        count = min(count, 2 if style in ("관광 중심", "체험 중심") else 1)
    return count


STYLE_NAMES = ("관광 중심", "휴식 중심", "맛집 중심", "체험 중심")


def style_components(style: str) -> list[str]:
    """기존 단일 스타일과 신규 복합 스타일 모두 지원."""
    return [part.strip() for part in style.split(" + ") if part.strip()]


def combined_style_plan(style: str, party: str, child_group: str | None) -> dict:
    """주요/보조 스타일별 목표 개수. 실제 후보가 부족하면 더 적게 표시."""
    parts = style_components(style)
    main = parts[0] if parts else "관광 중심"
    secondary = parts[1] if len(parts) > 1 else None
    if secondary is None:
        return {"attractions": attractions_per_day_for(party, main, child_group),
                "foods": 2 if main == "맛집 중심" else 1,
                "cafes": 2 if main == "휴식 중심" else 1,
                "experience": main == "체험 중심"}
    attractions = 2 if "관광 중심" in parts or "체험 중심" in parts else 1
    if main == "관광 중심" and secondary == "체험 중심":
        attractions = 3
    if main == "체험 중심" and secondary == "관광 중심":
        attractions = 3
    if party in ("부모님과 여행", "3대 가족 여행") or (child_group and "영유아 (0~5세)" in child_group):
        attractions = min(attractions, 2)
    return {"attractions": attractions,
            "foods": 2 if "맛집 중심" in parts else 1,
            "cafes": 2 if "휴식 중심" in parts else 1,
            "experience": "체험 중심" in parts}


EXPERIENCE_KEYWORDS = ("체험", "공방", "만들기", "승마", "목장", "농장", "박물관", "과학관", "전시", "액티비티", "레저", "수족관", "아쿠아리움", "테마파크", "쿠킹", "도예", "교육")


def prioritize_experiences(places: list[Place]) -> list[Place]:
    """실제 관광지 후보 내에서 체험 관련 명칭·설명이 있는 곳을 우선합니다."""
    return [p for _, p in sorted(enumerate(places), key=lambda item: (
        -sum(1 for word in EXPERIENCE_KEYWORDS if word in " ".join(str(item[1].get(k) or "") for k in ("name", "category", "description"))),
        item[0],
    ))]


def party_place_score(place: Place, party: str, child_group: str | None) -> int:
    """실제 장소명/유형/소개 기반으로 동행 유형별 선호를 구분합니다."""
    text = " ".join(str(place.get(k) or "") for k in ("name", "category", "description"))
    tags = place.get("recommendation_tags", []) or []
    def has(*words):
        return any(word in text for word in words)
    if party == "혼자 여행":
        return (16 if has("등산", "트레킹", "둘레길", "역사", "유적", "전망대", "박물관") else 0) + (7 if has("해변", "산책") else 0)
    if party == "연인·친구와 여행":
        return (16 if has("해변", "전망", "야경", "케이블카", "체험", "포토", "테마") else 0) + (6 if has("공원", "산책") else 0)
    if party == "부모님과 여행":
        return (18 if has("정원", "공원", "휴양", "해변", "호수", "전망", "문화") else 0) + (8 if "parent_pace_match" in tags else 0) - (15 if has("등산", "암벽", "트레킹", "익스트림") else 0)
    if party == "우리 가족 여행":
        if child_group and child_group != "아이 없음":
            return (22 if has("체험", "박물관", "어린이", "동물", "수족관", "테마", "공원") else 0) + (8 if "child_activity" in tags else 0)
        return (12 if has("해변", "공원", "산책", "전망", "문화") else 0)
    if party == "3대 가족 여행":
        return (20 if has("공원", "정원", "해변", "박물관", "수족관", "전망") else 0) + (6 if "parent_pace_match" in tags else 0) - (18 if has("등산", "암벽", "트레킹") else 0)
    return 0


def rank_places_for_party(places, companion, style, transport, age, party, child_group, child_ages=None):
    return [place for _, place in sorted(enumerate(places), key=lambda item: (
        -(score_place(item[1], companion, style, transport, age) + party_place_score(item[1], party, child_group) + child_profiles_score(item[1], child_ages or [])),
        item[0],
    ))]


def duration_to_days(duration: str) -> int:
    """'4박 5일' 등 기간 문구를 실제 여행 일수로 변환합니다."""
    import re

    if duration in DURATION_DAYS:
        return DURATION_DAYS[duration]
    match = re.fullmatch(r"\s*(\d+)박\s*(\d+)일\s*", str(duration))
    if match:
        nights, days = map(int, match.groups())
        if nights == days - 1 and 1 <= days <= MAX_TRAVEL_DAYS:
            return days
    return 1


def days_to_duration(days: int) -> str:
    return "당일치기" if days == 1 else f"{days - 1}박 {days}일"


def score_place(
    place: Place,
    companion: str,
    travel_style: str,
    transport: str,
    child_age: str | None,
) -> int:
    score = 0
    if companion in place.get("recommended_companions", []):
        score += 3
    if travel_style in place.get("travel_styles", []):
        score += 4
    if child_age and child_age in place.get("recommended_age_groups", []):
        score += 3
    if transport in place.get("travel_modes", []):
        score += 2

    tags = place.get("recommendation_tags", [])
    if companion in ("아빠와 여행", "엄마와 여행") and any(
        tag in tags for tag in ("parent_comfort", "easy_access", "parent_pace_match")
    ):
        score += 2
    if companion == "아이와 여행" and any(
        tag in tags for tag in ("child_activity", "child_friendly")
    ):
        score += 2
    return score


def rank_places(
    places: list[Place],
    companion: str,
    travel_style: str,
    transport: str,
    child_age: str | None,
) -> list[Place]:
    return [
        place
        for _, place in sorted(
            enumerate(places),
            key=lambda item: (
                -score_place(item[1], companion, travel_style, transport, child_age),
                item[0],
            ),
        )
    ]


def build_itinerary(
    attractions: list[Place], restaurants: list[Place], duration: str
) -> list[tuple[int, list[tuple[str, str, str]]]]:
    itinerary = []
    attraction_index = 0
    restaurant_index = 0

    for day in range(1, duration_to_days(duration) + 1):
        activities = []
        for label in ("오전 관광", "오후 관광"):
            if attraction_index < len(attractions):
                place = attractions[attraction_index]
                activities.append(
                    (label, place.get("name", "장소 정보 없음"), place.get("description", ""))
                )
                attraction_index += 1
        if restaurant_index < len(restaurants):
            place = restaurants[restaurant_index]
            activities.insert(
                1,
                ("식사", place.get("name", "장소 정보 없음"), place.get("description", "")),
            )
            restaurant_index += 1
        activities.append(("휴식", "자유 휴식 (장소 미지정)", "휴식 장소와 이동 시간은 별도로 정하지 않았습니다."))
        itinerary.append((day, activities))
    return itinerary


def order_attractions_by_distance(attractions: list[Place]) -> list[Place]:
    remaining = list(attractions)
    if len(remaining) < 2:
        return remaining

    ordered = [remaining.pop(0)]
    while remaining:
        current = ordered[-1]
        current_x = current.get("x")
        current_y = current.get("y")
        if not current_x or not current_y:
            ordered.extend(remaining)
            break
        distances = [
            (
                _straight_distance_meters(current_x, current_y, place.get("x"), place.get("y")),
                index,
            )
            for index, place in enumerate(remaining)
        ]
        reachable = [(distance, index) for distance, index in distances if distance is not None]
        if not reachable:
            ordered.extend(remaining)
            break
        _, nearest_index = min(reachable, key=lambda item: item[0])
        ordered.append(remaining.pop(nearest_index))
    return ordered


def _straight_distance_meters(x1: object, y1: object, x2: object, y2: object) -> float | None:
    from math import asin, cos, radians, sin, sqrt

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


def _valid_map_coordinates(place: Place) -> tuple[float, float] | None:
    try:
        longitude = float(place.get("x", ""))
        latitude = float(place.get("y", ""))
    except (TypeError, ValueError):
        return None
    if not isfinite(latitude) or not isfinite(longitude):
        return None
    if not -90 <= latitude <= 90 or not -180 <= longitude <= 180:
        return None
    return latitude, longitude


def _place_distance(a: dict, b: dict) -> float:
    distance = _straight_distance_meters(a.get("x"), a.get("y"), b.get("x"), b.get("y"))
    return distance if distance is not None else float("inf")


def _closest_unused(candidates: list[dict], anchor: dict, used: set[str]) -> dict | None:
    available = [p for p in candidates if str(p.get("name", "")) + "|" + str(p.get("address", "")) not in used]
    if not available:
        return None
    selected = min(enumerate(available), key=lambda pair: (_place_distance(anchor, pair[1]), pair[0]))[1]
    used.add(str(selected.get("name", "")) + "|" + str(selected.get("address", "")))
    return selected


# 지역별 대표 음식 키워드. 실제 메뉴 제공 여부는 검색 결과만으로 확인할 수 없습니다.
REGIONAL_FOOD_KEYWORDS = {
    "제주": ("흑돼지", "고기국수", "갈치", "옥돔", "전복", "몸국", "돔베", "보말", "해물뚝배기", "성게", "오메기"),
    "속초": ("물회", "오징어순대", "아바이순대", "닭강정", "생선구이", "대게", "홍게", "회", "섭국"),
    "강릉": ("초당순두부", "순두부", "장칼국수", "막국수", "감자옹심이", "물회", "회"),
    "부산": ("돼지국밥", "밀면", "어묵", "낙곱새", "씨앗호떡", "복국", "회"),
    "전주": ("비빔밥", "콩나물국밥", "한정식", "피순대", "순대국"),
    "경주": ("쌈밥", "한우", "떡갈비", "순두부", "교리김밥"),
    "여수": ("게장", "서대회", "갓김치", "장어탕", "돌게", "하모"),
    "통영": ("충무김밥", "굴", "멍게", "도다리", "물회", "해물"),
    "목포": ("세발낙지", "홍어", "민어", "꽃게", "갈치"),
    "춘천": ("닭갈비", "막국수"),
    "안동": ("찜닭", "간고등어", "헛제삿밥"),
    "대구": ("막창", "뭉티기", "따로국밥", "납작만두", "동인동찜갈비"),
    "대전": ("칼국수", "두부두루치기", "성심당"),
    "인천": ("짜장면", "쫄면", "밴댕이", "물텀벙"),
    "포항": ("과메기", "물회", "대게"),
    "울릉": ("오징어", "따개비", "명이나물", "약소"),
    "담양": ("떡갈비", "대통밥", "죽순"),
    "보성": ("꼬막", "녹차", "벌교"),
    "순천": ("꼬막", "짱뚱어", "국밥"),
    "남원": ("추어탕"),
    "군산": ("짬뽕", "꽃게", "박대", "아귀"),
}
CAFE_MOOD_KEYWORDS = (
    "감성", "뷰", "오션뷰", "바다뷰", "숲뷰", "한옥", "정원", "루프탑", "테라스",
    "로스터리", "스페셜티", "브런치", "베이커리", "디저트", "빈티지", "갤러리",
    "북카페", "통창", "노을", "풍경", "가든", "독채", "힐링", "포토", "핸드드립",
)


def _local_food_keywords(destination: str) -> tuple[str, ...]:
    for region, keywords in REGIONAL_FOOD_KEYWORDS.items():
        if region in destination:
            return keywords
    return ()


# 6-11: 기존 주변 후보만 정렬하지 않고 지역 메뉴/감성 카페를 별도로 검색합니다.
# 카카오 장소명/분류에 기반한 후보 검색이며 실제 메뉴나 분위기 검증은 아닙니다.
REGIONAL_SEARCH_TERMS = {
    "제주": ("고기국수", "흑돼지", "갈치조림", "보말칼국수", "해물뚝배기"),
    "속초": ("물회", "아바이순대", "생선구이", "섭국", "오징어순대"),
    "강릉": ("초당순두부", "장칼국수", "감자옹심이", "막국수"),
    "부산": ("돼지국밥", "밀면", "낙곱새", "복국"),
    "전주": ("비빔밥", "콩나물국밥", "한정식"),
    "경주": ("쌈밥", "떡갈비", "한우"),
    "여수": ("게장", "서대회", "장어탕"),
    "통영": ("충무김밥", "굴국밥", "멍게비빔밥"),
}
FRANCHISE_HINTS = ("처갓집", "교촌", "bhc", "bbq", "굽네", "맘스터치", "버거킹", "맥도날드", "롯데리아", "도미노", "피자헛", "스타벅스", "투썸", "이디야", "메가커피", "컴포즈")


@st.cache_data(ttl=3600, show_spinner=False)
def _kakao_thematic_query(api_key: str, query: str, category_code: str, longitude: str, latitude: str) -> list[dict]:
    if not api_key or not longitude or not latitude:
        return []
    params = {"query": query, "category_group_code": category_code, "x": longitude, "y": latitude,
              "radius": 12000, "sort": "distance", "size": 15}
    try:
        req = Request("https://dapi.kakao.com/v2/local/search/keyword.json?" + urlencode(params),
                      headers={"Authorization": f"KakaoAK {api_key}"})
        with urlopen(req, timeout=7) as response:
            documents = json.load(response).get("documents", [])
    except (HTTPError, URLError, TimeoutError, ValueError, OSError):
        return []
    return [d for d in documents if d.get("category_group_code") == category_code]


def _extra_thematic_candidates(anchor: dict, destination: str, kind: str) -> list[dict]:
    coords = _valid_map_coordinates(anchor)
    if coords is None:
        return []
    lat, lon = coords
    if kind == "food":
        region_terms = next((v for region, v in REGIONAL_SEARCH_TERMS.items() if region in destination), None)
        terms = region_terms or _local_food_keywords(destination)[:4]
        code = "FD6"
    else:
        terms = ("오션뷰 카페", "감성 카페", "베이커리 카페", "정원 카페")
        code = "CE7"
    key = get_kakao_api_key()
    found = []
    seen = set()
    for term in terms[:5]:
        for d in _kakao_thematic_query(key, term, code, str(lon), str(lat)):
            ident = d.get("id") or (d.get("place_name"), d.get("address_name"))
            if ident in seen:
                continue
            seen.add(ident)
            found.append({"name": d.get("place_name", ""), "address": d.get("road_address_name") or d.get("address_name", ""),
                          "category": d.get("category_name", ""), "description": term,
                          "x": d.get("x"), "y": d.get("y"), "detail_url": d.get("place_url", ""),
                          "recommendation_reason": "지역 음식/카페 키워드로 검색된 후보입니다. 실제 메뉴·분위기·영업시간은 확인해 주세요."})
    return found


def _select_thematic_nearby(
    candidates: list[dict], anchor: dict, used: set[str], kind: str, destination: str
) -> tuple[dict | None, bool]:
    """Prefer region-specific dishes or atmospheric cafes, while penalizing distant candidates.

    Keyword matching is heuristic: no review, menu, operating hours, or quality verification.
    """
    available = [p for p in candidates if str(p.get("name", "")) + "|" + str(p.get("address", "")) not in used]
    if not available:
        return None, False
    keywords = _local_food_keywords(destination) if kind == "food" else CAFE_MOOD_KEYWORDS

    def evaluate(item):
        index, place = item
        text = " ".join(str(place.get(k) or "") for k in ("name", "category", "description"))
        matched = sum(1 for word in keywords if word in text)
        franchise = any(word.casefold() in str(place.get("name", "")).casefold() for word in FRANCHISE_HINTS)
        distance = _place_distance(anchor, place)
        # 3 km ≈ 20 points, 10 km ≈ 67 points: avoid extreme detours for thematic places.
        distance_penalty = min(distance / 150.0, 100.0) if distance != float("inf") else 60.0
        # 지역 음식/감성 후보가 있으면 일반 프랜차이즈보다 우선합니다.
        return (matched * 120.0 - distance_penalty - (250 if franchise else 0), -index)

    selected = max(enumerate(available), key=evaluate)[1]
    text = " ".join(str(selected.get(k) or "") for k in ("name", "category", "description"))
    matched = any(word in text for word in keywords)
    used.add(str(selected.get("name", "")) + "|" + str(selected.get("address", "")))
    return selected, matched



# 6-15: 동일 장소 중복 제거 및 실제 좌표 기반 일정 품질 검증
MAX_RECOMMENDATION_DETOUR_M = 10000


def _transport_profile(transport: str) -> dict:
    """직선거리 기반의 보수적 휴리스틱. 대중교통 노선·시간을 검증하지 않습니다."""
    public = any(token in str(transport) for token in ("대중교통", "버스", "지하철", "기차"))
    return {
        "public": public,
        "attractions_radius": 4500 if public else 18000,
        "food_radius": 1600 if public else 10000,
        "cafe_radius": 1600 if public else 10000,
        "max_attractions": 2 if public else 4,
        "warning_radius": 4500 if public else 18000,
    }


def _transport_order_attractions(places: list[dict], transport: str, days: int, count: int) -> list[dict]:
    """일자별 권역을 형성해 장거리 이동을 줄입니다. 추천 상위 후보 우선."""
    profile = _transport_profile(transport)
    remaining = list(places)
    result = []
    for _ in range(days):
        if not remaining:
            break
        anchor = remaining.pop(0)
        day_places = [anchor]
        for _ in range(count - 1):
            if not remaining:
                break
            distances = [(_place_distance(anchor, place), i) for i, place in enumerate(remaining)]
            near = [(dist, i) for dist, i in distances if dist <= profile["attractions_radius"]]
            if near:
                _, idx = min(near)
            else:
                # 반경 안 후보가 없다면 가장 가까운 후보로 대체하며 품질 점검에 표시합니다.
                _, idx = min(distances)
            day_places.append(remaining.pop(idx))
        result.extend(day_places)
    return result


def _place_identity(place: dict) -> str:
    """동일 상호·주소는 한 여행 일정에서 한 번만 추천합니다."""
    import re
    name = re.sub(r"\s+", "", str(place.get("name") or "")).casefold()
    address = re.sub(r"\s+", "", str(place.get("address") or "")).casefold()
    if name:
        return name + "|" + address
    coords = _valid_map_coordinates(place)
    return f"coord:{coords}" if coords else ""


def _unique_places(places: list[dict]) -> list[dict]:
    seen = set()
    result = []
    for place in places:
        identity = _place_identity(place)
        if identity and identity in seen:
            continue
        if identity:
            seen.add(identity)
        result.append(place)
    return result


def _itinerary_quality_notes(itinerary: list, transport: str = "자차") -> list[str]:
    """추천 품질 참고 사항만 표시하며, 실제 도로 이동시간은 추정하지 않습니다."""
    notes = []
    seen = set()
    warning_radius = _transport_profile(transport)["warning_radius"]
    for day, activities in itinerary:
        visits = [a for a in activities if a.get("type") in EDITABLE_PLACE_TYPES and a.get("name") != "주변 장소 검색 결과 없음"]
        for activity in visits:
            identity = _place_identity(activity)
            if identity in seen:
                notes.append(f"{day}일 차: 다른 날짜와 중복된 방문 장소가 있습니다 ({activity.get('name', '')}).")
            seen.add(identity)
        for before, after in zip(visits, visits[1:]):
            dist = _straight_distance_meters(before.get("longitude"), before.get("latitude"), after.get("longitude"), after.get("latitude"))
            if dist is not None and dist > warning_radius:
                notes.append(f"{day}일 차: {before.get('name')} → {after.get('name')} 직선거리 약 {dist / 1000:.1f}km로 이동 부담이 있을 수 있습니다.")
        if not any(a.get("type") in ("식사", "점심 식사", "저녁 식사") for a in visits):
            notes.append(f"{day}일 차: 식사 장소가 배치되지 않았습니다.")
    return notes

def _cluster_attraction_groups(groups: list[dict], count_per_day: int) -> list[dict]:
    """Keep the highest-ranked attraction as each day's anchor, then prefer nearby attractions."""
    remaining = list(groups)
    ordered = []
    while remaining:
        anchor = remaining.pop(0)
        ordered.append(anchor)
        current = anchor["attraction"]
        for _ in range(max(0, count_per_day - 1)):
            if not remaining:
                break
            index = min(range(len(remaining)), key=lambda i: (_place_distance(current, remaining[i]["attraction"]), i))
            nearest = remaining.pop(index)
            ordered.append(nearest)
            current = nearest["attraction"]
    return ordered


def build_location_itinerary(
    groups: list[dict], duration: str, travel_style: str, transport: str,
    party: str = "혼자 여행", child_group: str | None = None, destination: str = ""
) -> list[tuple[int, list[dict[str, str]]]]:
    days = duration_to_days(duration)
    plan = combined_style_plan(travel_style, party, child_group)
    attractions_per_day = min(plan["attractions"], _transport_profile(transport)["max_attractions"])
    # 같은 관광지가 검색 결과에 반복 등장해도 하루/다른 날짜에 중복 배치하지 않습니다.
    unique_groups = []
    seen_attractions = set()
    for group in groups:
        identity = _place_identity(group.get("attraction", {}))
        if identity and identity in seen_attractions:
            continue
        if identity:
            seen_attractions.add(identity)
        unique_groups.append(group)
    # 선택 이동수단에 맞춰 일자별 관광지 권역을 묶습니다.
    ordered_attractions = _transport_order_attractions(
        [g["attraction"] for g in unique_groups], transport, days, attractions_per_day
    )
    group_lookup = {_place_identity(g["attraction"]): g for g in unique_groups}
    groups = [group_lookup[_place_identity(a)] for a in ordered_attractions if _place_identity(a) in group_lookup]
    itinerary = []
    group_index = 0
    used_food: set[str] = set()
    used_cafes: set[str] = set()

    for day in range(1, days + 1):
        day_groups = groups[group_index:group_index + attractions_per_day]
        group_index += len(day_groups)
        activities: list[dict[str, str]] = []

        def add_activity(kind: str, place: Place, fallback_reason: str) -> None:
            name = str(place.get("name") or "장소 정보 없음")
            address = str(place.get("address") or "주소 정보가 제공되지 않았습니다.")
            url = str(place.get("detail_url") or f"https://map.kakao.com/link/search/{quote(name + ' ' + address)}")
            reason = str(place.get("recommendation_reason") or fallback_reason)
            if place.get("distance_meters") is not None:
                reason += f" 직선거리 참고 약 {place['distance_meters']}m이며 실제 경로·이동시간은 확인되지 않았습니다."
            activity = {"type": kind, "name": name, "address": address, "reason": reason, "url": url}
            coords = _valid_map_coordinates(place)
            if coords is not None:
                activity["latitude"], activity["longitude"] = map(str, coords)
            activities.append(activity)

        if day_groups:
            attractions = [g["attraction"] for g in day_groups]
            restaurants = [p for g in day_groups for p in g.get("restaurants", [])]
            cafes = [p for g in day_groups for p in g.get("cafes", [])]
            first = attractions[0]
            last = attractions[-1]
            regional_food = _extra_thematic_candidates(first, destination, "food")
            mood_cafes = _extra_thematic_candidates(last, destination, "cafe")
            food_candidates = _unique_places(regional_food + restaurants)
            cafe_candidates = _unique_places(mood_cafes + cafes)
            # 자차/대중교통별 주변 후보 탐색 반경을 구분합니다.
            profile = _transport_profile(transport)
            nearby_food = [p for p in food_candidates if _place_distance(first, p) <= profile["food_radius"]]
            nearby_cafes = [p for p in cafe_candidates if _place_distance(last, p) <= profile["cafe_radius"]]
            if nearby_food:
                food_candidates = nearby_food
            elif profile["public"]:
                food_candidates = []  # 먼 음식점을 무리하게 추천하지 않음
            if nearby_cafes:
                cafe_candidates = nearby_cafes
            elif profile["public"]:
                cafe_candidates = []

            def add_food(anchor: dict, kind: str) -> dict | None:
                place, matched = _select_thematic_nearby(food_candidates, anchor, used_food, "food", destination)
                if place:
                    reason = ("지역 대표 음식 키워드가 확인된 후보입니다." if matched else
                              "지역 음식 후보가 부족해 동선상 가까운 음식점을 선택했습니다.")
                    add_activity(kind, place, reason + " 메뉴·영업시간은 확인이 필요합니다.")
                return place

            def add_cafe(anchor: dict) -> dict | None:
                place, matched = _select_thematic_nearby(cafe_candidates, anchor, used_cafes, "cafe", destination)
                if place:
                    reason = ("감성·전망·디저트 키워드가 확인된 카페 후보입니다." if matched else
                              "감성 키워드 후보가 부족해 동선상 가까운 카페를 선택했습니다.")
                    add_activity("카페·휴식", place, reason + " 실제 분위기·영업시간은 확인이 필요합니다.")
                return place

            # 주요 스타일은 관광지 순서, 보조 스타일은 음식점/카페 비중에 반영합니다.
            add_activity("관광", first, "여행 조건에 맞춰 선정한 관광지입니다.")
            lunch = add_food(first, "점심 식사")
            for attraction in attractions[1:]:
                add_activity("관광", attraction,
                             "체험형 장소 후보입니다. 실제 체험 운영 여부는 확인이 필요합니다." if plan["experience"]
                             else "관광지 간 거리와 여행 스타일을 고려한 장소입니다.")
            cafe_anchor = last if len(attractions) > 1 else (lunch or first)
            cafe = add_cafe(cafe_anchor)
            if plan["cafes"] > 1:
                add_cafe(cafe or cafe_anchor)
            if plan["foods"] > 1:
                add_food(cafe or last, "저녁 식사")

            # 검색 결과가 부족해 음식점·카페가 누락되면 안내를 표시합니다.
            if not any(a["type"] in ("식사", "점심 식사", "저녁 식사") for a in activities):
                activities.append({"type": "안내", "name": "음식점 후보 부족", "address": "", "reason": "주변 음식점 검색 결과가 없습니다. 직접 추가해 주세요.", "url": ""})
            if not any(a["type"] == "카페·휴식" for a in activities):
                activities.append({"type": "안내", "name": "카페 후보 부족", "address": "", "reason": "주변 카페 검색 결과가 없습니다. 직접 추가해 주세요.", "url": ""})
        else:
            activities.append({"type": "안내", "name": "추가 장소 필요", "address": "", "reason": "해당 날짜에 배치할 관광지가 부족합니다. 장소 검색에서 직접 추가해 주세요.", "url": ""})

        activities.append({
            "type": "이동 참고", "name": transport, "address": "", "url": "",
            "reason": f"{travel_style} / {transport} 기준으로 이동 권역과 방문 밀도를 조정했습니다. 직선거리만 참고하며 실제 대중교통 노선·환승·운행시간, 도로 경로·영업시간은 확인되지 않았습니다.",
        })
        itinerary.append((day, activities))
    return itinerary


EDITABLE_PLACE_TYPES = {"관광", "식사", "점심 식사", "저녁 식사", "카페·휴식"}


def is_editable_visit(activity: dict) -> bool:
    return (
        activity.get("type") in EDITABLE_PLACE_TYPES
        and activity.get("name") != "주변 장소 검색 결과 없음"
    )


KAKAO_EDIT_CATEGORY_CODES = {"관광지": "AT4", "음식점": "FD6", "카페": "CE7"}
KAKAO_EDIT_CATEGORY_WORDS = {"관광지": "관광명소", "음식점": "음식점", "카페": "카페"}


@st.cache_data(ttl=600, show_spinner=False)
def search_kakao_edit_places(keyword: str, api_key: str, category: str) -> tuple[list[dict], str]:
    """지역명+장소 종류로 검색하고 카카오 공식 분류 코드로 엄격하게 필터링합니다."""
    if not api_key:
        return [], "카카오 REST API 키가 설정되지 않았습니다."
    category_code = KAKAO_EDIT_CATEGORY_CODES.get(category)
    if not category_code:
        return [], "지원하지 않는 장소 종류입니다."
    query = f"{keyword.strip()} {KAKAO_EDIT_CATEGORY_WORDS[category]}".strip()
    params = {"query": query, "size": 15, "page": 1, "sort": "accuracy", "category_group_code": category_code}
    request = Request(
        "https://dapi.kakao.com/v2/local/search/keyword.json?" + urlencode(params),
        headers={"Authorization": f"KakaoAK {api_key}"},
    )
    try:
        with urlopen(request, timeout=10) as response:
            payload = json.load(response)
    except HTTPError as exc:
        return [], f"카카오 검색 실패 (HTTP {exc.code}). API 설정을 확인하세요."
    except (URLError, TimeoutError, ValueError, OSError):
        return [], "카카오 검색 연결에 실패했습니다. 잠시 후 다시 시도해 주세요."
    places = [place for place in payload.get("documents", [])
              if place.get("category_group_code") == category_code]
    return places, ""


def render_place_search_editor(itinerary: list, destination: str) -> None:
    """카카오 장소 검색 결과를 기존 일정에 추가하거나 기존 장소와 교체합니다."""
    st.markdown("#### 장소 검색 · 추가 · 교체")
    st.caption("관광지·음식점·카페 중 선택한 종류만 카카오 공식 분류 기준으로 검색합니다. 예: 음식점 선택 후 제주도 입력. 결과는 일정·지도·후기·영상에 반영됩니다.")
    if not itinerary:
        return
    with st.form("trip_place_search_form"):
        category = st.selectbox("장소 종류", ["관광지", "음식점", "카페"], key="trip_edit_category")
        keyword = st.text_input(
            "검색할 지역 또는 장소", placeholder="예: 제주도, 서귀포, 속초, 영금정",
            key="trip_edit_keyword",
        )
        search_clicked = st.form_submit_button("장소 검색", type="primary")
    if search_clicked:
        if not keyword.strip():
            st.warning("검색할 지역 또는 장소를 입력해 주세요.")
            st.session_state.trip_edit_search_results = []
            st.session_state.trip_edit_search_error = ""
        else:
            # 현재 일정 좌표를 반경 기준으로 강제하지 않습니다.
            # 제주도를 검색하면 기존 여행지가 어디든 제주도 결과를 받을 수 있습니다.
            with st.spinner("선택한 장소 종류만 검색하고 있습니다..."):
                places, error = search_kakao_edit_places(
                    keyword.strip(), get_kakao_api_key(), category
                )
            st.session_state.trip_edit_search_results = places
            st.session_state.trip_edit_search_category = category
            st.session_state.trip_edit_search_keyword = keyword.strip()
            st.session_state.trip_edit_search_error = error
    if st.session_state.get("trip_edit_search_error"):
        st.warning(st.session_state.trip_edit_search_error)
    places = st.session_state.get("trip_edit_search_results", [])
    if not places:
        if st.session_state.get("trip_edit_search_keyword") and not st.session_state.get("trip_edit_search_error"):
            st.info("선택한 종류에 해당하는 검색 결과가 없습니다. 지역명이나 장소명을 바꿔 다시 검색해 주세요.")
        return
    st.caption(
        f"{st.session_state.get('trip_edit_search_category', '관광지')}만 표시 · "
        f"검색 결과 {len(places)}곳 · 영업시간 및 실제 위치는 선택 전 확인해 주세요."
    )
    choices = list(range(len(places)))
    choice = st.selectbox(
        "추가하거나 교체할 장소 선택", choices,
        format_func=lambda i: f"{places[i].get('place_name', '이름 없음')} — {places[i].get('road_address_name') or places[i].get('address_name') or '주소 미제공'}",
        key="trip_edit_place_choice",
    )
    selected = places[choice]
    category = st.session_state.get("trip_edit_search_category", "관광지")
    kind = {"관광지": "관광", "음식점": "식사", "카페": "카페·휴식"}[category]
    new_activity = {
        "type": kind,
        "name": str(selected.get("place_name") or "장소 정보 없음"),
        "address": str(selected.get("road_address_name") or selected.get("address_name") or "주소 미제공"),
        "reason": "사용자가 카카오 로컬 검색 결과에서 직접 선택한 장소입니다. 영업 여부는 확인되지 않았습니다.",
        "url": str(selected.get("place_url") or "https://map.kakao.com/link/search/" + quote(str(selected.get("place_name", "")))),
        "latitude": str(selected.get("y", "")),
        "longitude": str(selected.get("x", "")),
    }
    day_index = st.selectbox(
        "일정을 편집할 날짜", list(range(len(itinerary))),
        format_func=lambda i: f"{itinerary[i][0]}일 차", key="trip_edit_target_day"
    )
    activities = itinerary[day_index][1]
    candidates = [i for i, a in enumerate(activities) if is_editable_visit(a)]
    actions = ["일정에 추가"] + (["기존 장소 교체"] if candidates else [])
    action = st.radio("편집 방법", actions, horizontal=True, key="trip_edit_action")
    replace_index = None
    if action == "기존 장소 교체":
        replace_index = st.selectbox(
            "교체할 기존 장소", candidates,
            format_func=lambda i: f"{activities[i].get('type', '')} · {activities[i].get('name', '')}",
            key="trip_edit_replace_index"
        )
    if st.button("선택한 장소 적용", type="primary", key="trip_edit_apply"):
        if not selected.get("x") or not selected.get("y"):
            st.error("이 장소는 지도 좌표가 없어 일정에 추가할 수 없습니다.")
            return
        if action == "기존 장소 교체" and replace_index is not None:
            # 교체된 장소의 실제 분류를 유지합니다. 관광지→카페 교체도 정확히 반영합니다.
            activities[replace_index] = new_activity
        else:
            # 이동 참고 문구 앞에 추가합니다.
            insert_at = next((i for i, a in enumerate(activities) if a.get("type") == "이동 참고"), len(activities))
            activities.insert(insert_at, new_activity)
        # 편집 결과를 세션의 단일 원본에 명시적으로 저장합니다.
        # 지도와 일정표, 후기/영상이 모두 이 상태를 읽습니다.
        st.session_state.trip_editable_itinerary = deepcopy(itinerary)
        st.session_state.trip_edit_last_change = (
            f"{itinerary[day_index][0]}일 차 · {new_activity['name']} "
            + ("교체" if action == "기존 장소 교체" else "추가")
        )
        st.session_state.trip_edit_search_results = []
        st.session_state.trip_edit_search_error = ""
        st.rerun()


def render_editable_itinerary(itinerary: list[tuple[int, list[dict[str, str]]]]) -> None:
    st.markdown("#### 일정 편집 · 방문 순서 변경 및 삭제")
    st.caption("방문 장소만 이동하거나 삭제할 수 있습니다. 안내 문구는 편집 대상이 아닙니다.")
    if st.button("원래 추천 일정으로 초기화", key="trip_reset_edit"):
        st.session_state.trip_editable_itinerary = deepcopy(st.session_state.trip_original_itinerary)
        st.rerun()

    for day_index, (day, activities) in enumerate(itinerary):
        st.markdown(f"#### {day}일 차")
        editable_indices = [i for i, a in enumerate(activities) if is_editable_visit(a)]
        if not editable_indices:
            st.info("이 날짜에 남아 있는 방문 장소가 없습니다.")
        for visit_number, index in enumerate(editable_indices, start=1):
            activity = activities[index]
            with st.container(border=True):
                st.markdown(f"**{visit_number}. {activity.get('type', '일정')} · {activity.get('name', '장소 정보 없음')}**")
                if activity.get("address"):
                    st.caption(f"주소: {activity['address']}")
                if activity.get("reason"):
                    st.caption(activity["reason"])
                if activity.get("url"):
                    st.link_button("카카오맵에서 보기", activity["url"])
                up_col, down_col, delete_col = st.columns(3)
                with up_col:
                    up = st.button("↑ 위로", key=f"trip_up_{day_index}_{index}", disabled=visit_number == 1, use_container_width=True)
                with down_col:
                    down = st.button("↓ 아래로", key=f"trip_down_{day_index}_{index}", disabled=visit_number == len(editable_indices), use_container_width=True)
                with delete_col:
                    delete = st.button("삭제", key=f"trip_delete_{day_index}_{index}", use_container_width=True)
                if up:
                    previous = editable_indices[visit_number - 2]
                    activities[previous], activities[index] = activities[index], activities[previous]
                    st.session_state.trip_editable_itinerary = deepcopy(itinerary)
                    st.rerun()
                if down:
                    following = editable_indices[visit_number]
                    activities[following], activities[index] = activities[index], activities[following]
                    st.session_state.trip_editable_itinerary = deepcopy(itinerary)
                    st.rerun()
                if delete:
                    activities.pop(index)
                    st.session_state.trip_editable_itinerary = deepcopy(itinerary)
                    st.session_state.trip_edit_last_change = f"{day}일 차 · {activity.get('name', '장소')} 삭제"
                    st.rerun()
        for activity in activities:
            if not is_editable_visit(activity) and activity.get("type") == "이동 참고":
                st.caption(activity.get("reason", ""))


# 5단계: 일정 파일 저장·불러오기·PDF 공유
SAVE_FORMAT_VERSION = 1


def trip_json_bytes(itinerary: list, destination: str, duration: str) -> bytes:
    payload = {
        "format": "TRIP_FINDER_ITINERARY",
        "version": SAVE_FORMAT_VERSION,
        "destination": destination,
        "duration": duration,
        "saved_at": datetime.now().astimezone().isoformat(timespec="seconds"),
        "itinerary": [
            {"day": int(day), "activities": activities}
            for day, activities in itinerary
        ],
    }
    return json.dumps(payload, ensure_ascii=False, indent=2).encode("utf-8")


def parse_trip_json(uploaded_bytes: bytes) -> tuple[str, str, list]:
    if len(uploaded_bytes) > 2_000_000:
        raise ValueError("파일 크기가 2MB를 초과합니다.")
    try:
        data = json.loads(uploaded_bytes.decode("utf-8-sig"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise ValueError("올바른 UTF-8 JSON 파일이 아닙니다.") from exc
    if not isinstance(data, dict) or data.get("format") != "TRIP_FINDER_ITINERARY":
        raise ValueError("TRIP FINDER에서 저장한 일정 JSON 파일만 불러올 수 있습니다.")
    if data.get("version") != SAVE_FORMAT_VERSION:
        raise ValueError("지원하지 않는 일정 파일 버전입니다.")
    destination = data.get("destination", "")
    duration = data.get("duration", "")
    raw_days = data.get("itinerary")
    if not isinstance(destination, str) or len(destination) > 100 or not isinstance(duration, str) or len(duration) > 50:
        raise ValueError("여행지 또는 여행 기간 형식이 올바르지 않습니다.")
    if not isinstance(raw_days, list) or not 1 <= len(raw_days) <= MAX_TRAVEL_DAYS:
        raise ValueError("일정은 1~30일만 불러올 수 있습니다.")
    result = []
    for day_item in raw_days:
        if not isinstance(day_item, dict) or type(day_item.get("day")) is not int or not 1 <= day_item["day"] <= MAX_TRAVEL_DAYS:
            raise ValueError("날짜 데이터가 올바르지 않습니다.")
        activities = day_item.get("activities")
        if not isinstance(activities, list) or len(activities) > 60:
            raise ValueError("하루 방문 장소 데이터가 올바르지 않습니다.")
        clean = []
        for item in activities:
            if not isinstance(item, dict):
                raise ValueError("장소 데이터가 올바르지 않습니다.")
            allowed = ("type", "name", "address", "reason", "url", "latitude", "longitude")
            if any(key in item and (not isinstance(item[key], str) or len(item[key]) > 2000) for key in allowed):
                raise ValueError("장소 정보에 허용되지 않는 값이 있습니다.")
            entry = {key: item[key] for key in allowed if key in item}
            if entry.get("url") and not entry["url"].startswith(("https://", "http://")):
                entry["url"] = ""
            clean.append(entry)
        result.append((day_item["day"], clean))
    if len({day for day, _ in result}) != len(result):
        raise ValueError("중복된 날짜가 포함되어 있습니다.")
    return destination, duration, result


def trip_pdf_bytes(itinerary: list, destination: str, duration: str) -> bytes:
    """ReportLab을 사용해 한글 일정표 PDF를 메모리에서 생성합니다."""
    from reportlab.lib import colors
    from reportlab.lib.enums import TA_CENTER
    from reportlab.lib.styles import ParagraphStyle, getSampleStyleSheet
    from reportlab.lib.pagesizes import A4
    from reportlab.lib.units import mm
    from reportlab.pdfbase import pdfmetrics
    from reportlab.pdfbase.cidfonts import UnicodeCIDFont
    from reportlab.platypus import SimpleDocTemplate, Paragraph, Spacer, KeepTogether

    font_name = "HYGothic-Medium"
    pdfmetrics.registerFont(UnicodeCIDFont(font_name))
    buffer = BytesIO()
    doc = SimpleDocTemplate(buffer, pagesize=A4, leftMargin=19*mm, rightMargin=19*mm,
                            topMargin=18*mm, bottomMargin=18*mm)
    styles = getSampleStyleSheet()
    title = ParagraphStyle("TripTitle", parent=styles["Title"], fontName=font_name,
                           fontSize=19, leading=27, textColor=colors.HexColor("#183b43"), alignment=TA_CENTER)
    body = ParagraphStyle("TripBody", fontName=font_name, fontSize=10, leading=17, spaceAfter=4, wordWrap="CJK")
    heading = ParagraphStyle("TripHeading", parent=body, fontSize=13, leading=22,
                             spaceBefore=13, spaceAfter=8, textColor=colors.HexColor("#087f75"))
    small = ParagraphStyle("TripSmall", parent=body, fontSize=8, leading=13,
                           textColor=colors.HexColor("#60777b"))
    story = [Paragraph("TRIP FINDER 여행 일정", title),
             Paragraph(f"여행지: {escape(destination)}  |  기간: {escape(duration)}", body), Spacer(1, 5*mm)]
    for day, activities in itinerary:
        story.append(Paragraph(f"{day}일 차", heading))
        visits = [a for a in activities if is_editable_visit(a)]
        if not visits:
            story.append(Paragraph("방문 장소 없음", small))
        for index, item in enumerate(visits, 1):
            name = escape(str(item.get("name") or "장소 정보 없음"))
            kind = escape(str(item.get("type") or "장소"))
            address = escape(str(item.get("address") or "주소 정보 없음"))
            url = str(item.get("url") or "")
            rows = [Paragraph(f"{index}. [{kind}] {name}", body), Paragraph(f"주소: {address}", small)]
            if url.startswith(("https://", "http://")):
                # 링크 대상은 허용된 http(s) 주소로만 한정
                rows.append(Paragraph(f"지도 링크: {escape(url)}", small))
            story.append(KeepTogether(rows))
            story.append(Spacer(1, 3*mm))
    story.append(Spacer(1, 4*mm))
    story.append(Paragraph("안내: 실제 도로 경로·이동시간·영업 여부는 별도로 확인해 주세요.", small))
    doc.build(story)
    return buffer.getvalue()


def render_trip_exports(itinerary: list, destination: str, duration: str, key_prefix: str) -> None:
    st.subheader("여행 일정 저장 · 공유")
    st.caption("JSON 파일은 TRIP FINDER에서 다시 불러올 수 있습니다. PDF는 가족·지인에게 공유할 수 있는 일정표입니다.")
    if not itinerary:
        st.info("저장할 여행 일정이 없습니다.")
        return
    safe_name = "".join(c for c in destination if c.isalnum() or c in ("-", "_"))[:30] or "travel"
    col_json, col_pdf = st.columns(2)
    with col_json:
        st.download_button("일정 저장 (JSON)", data=trip_json_bytes(itinerary, destination, duration),
                           file_name=f"TRIP_FINDER_{safe_name}.json", mime="application/json",
                           key=f"{key_prefix}_json", use_container_width=True)
    with col_pdf:
        try:
            pdf_data = trip_pdf_bytes(itinerary, destination, duration)
            st.download_button("일정표 다운로드 (PDF)", data=pdf_data,
                               file_name=f"TRIP_FINDER_{safe_name}.pdf", mime="application/pdf",
                               key=f"{key_prefix}_pdf", use_container_width=True)
        except ImportError:
            st.warning("PDF 다운로드를 위해 터미널에서 python -m pip install reportlab 을 실행해 주세요.")
        except Exception as exc:
            st.error(f"PDF 생성 중 오류가 발생했습니다: {type(exc).__name__}")


def render_imported_trip() -> None:
    st.markdown("---")
    st.subheader("저장한 여행 일정 불러오기")
    st.caption("이전에 저장한 TRIP FINDER JSON 파일을 선택하면 일정과 지도를 다시 볼 수 있습니다. 업로드한 내용은 현재 앱 세션에서만 유지됩니다.")
    uploaded = st.file_uploader("저장된 일정 JSON 선택", type=["json"], key="trip_import_upload")
    if uploaded is not None and st.button("선택한 일정 불러오기", key="trip_import_apply", type="primary"):
        try:
            destination, duration, itinerary = parse_trip_json(uploaded.getvalue())
        except ValueError as exc:
            st.error(str(exc))
        else:
            st.session_state.trip_import_destination = destination
            st.session_state.trip_import_duration = duration
            st.session_state.trip_import_original = deepcopy(itinerary)
            st.session_state.trip_import_itinerary = deepcopy(itinerary)
            st.success("저장된 여행 일정을 불러왔습니다.")
    imported = st.session_state.get("trip_import_itinerary")
    if not imported:
        return
    destination = st.session_state.get("trip_import_destination", "여행")
    duration = st.session_state.get("trip_import_duration", "")
    st.markdown(f"#### 불러온 일정: {destination} · {duration}")
    if st.button("불러온 원본으로 초기화", key="trip_import_reset"):
        st.session_state.trip_import_itinerary = deepcopy(st.session_state.trip_import_original)
        st.rerun()
    for day_index, (day, activities) in enumerate(imported):
        st.markdown(f"**{day}일 차**")
        indices = [i for i, a in enumerate(activities) if is_editable_visit(a)]
        if not indices:
            st.caption("방문 장소 없음")
        for number, index in enumerate(indices, 1):
            activity = activities[index]
            with st.container(border=True):
                st.write(f"{number}. {activity.get('type', '')} · {activity.get('name', '')}")
                st.caption(activity.get("address", ""))
                c1, c2, c3 = st.columns(3)
                if c1.button("↑ 위로", key=f"imp_up_{day_index}_{index}", disabled=number == 1, use_container_width=True):
                    previous = indices[number - 2]
                    activities[previous], activities[index] = activities[index], activities[previous]
                    st.session_state.trip_editable_itinerary = deepcopy(itinerary)
                    st.rerun()
                if c2.button("↓ 아래로", key=f"imp_down_{day_index}_{index}", disabled=number == len(indices), use_container_width=True):
                    following = indices[number]
                    activities[following], activities[index] = activities[index], activities[following]
                    st.session_state.trip_editable_itinerary = deepcopy(itinerary)
                    st.rerun()
                if c3.button("삭제", key=f"imp_delete_{day_index}_{index}", use_container_width=True):
                    activities.pop(index)
                    st.session_state.trip_editable_itinerary = deepcopy(itinerary)
                    st.session_state.trip_edit_last_change = f"{day}일 차 · {activity.get('name', '장소')} 삭제"
                    st.rerun()
    render_trip_exports(imported, destination, duration, "imported")
    # 지도의 위젯 키는 본 검색 결과 지도와 구분합니다.
    render_itinerary_map(imported, widget_prefix="imported")


def render_itinerary_map(
    itinerary: list[tuple[int, list[dict[str, str]]]],
    widget_prefix: str = "main",
) -> None:
    st.subheader("여행 동선 지도")
    if not itinerary or not any(activities for _, activities in itinerary):
        st.info("표시할 여행 일정이 없습니다.")
        return

    day_numbers = [day for day, _ in itinerary]
    selected_day = st.selectbox(
        "지도에 표시할 날짜",
        ["전체 일정", *[f"{day}일 차" for day in day_numbers]],
        key=f"{widget_prefix}_itinerary_map_day",
    )
    selected_days = (
        itinerary
        if selected_day == "전체 일정"
        else [entry for entry in itinerary if f"{entry[0]}일 차" == selected_day]
    )
    place_types = EDITABLE_PLACE_TYPES
    candidates = [
        (day, activity)
        for day, activities in selected_days
        for activity in activities
        if activity.get("type") in place_types
        and activity.get("name") != "주변 장소 검색 결과 없음"
    ]
    map_places: list[tuple[int, int, dict[str, str], float, float]] = []
    for day, activities in selected_days:
        visit_order = 0
        for activity in activities:
            if (
                activity.get("type") not in place_types
                or activity.get("name") == "주변 장소 검색 결과 없음"
            ):
                continue
            visit_order += 1
            coordinates = _valid_map_coordinates(
                {"x": activity.get("longitude", ""), "y": activity.get("latitude", "")}
            )
            if coordinates is not None:
                latitude, longitude = coordinates
                map_places.append((day, visit_order, activity, latitude, longitude))

    if not map_places:
        st.info("선택한 일정에 지도에 표시할 좌표가 있는 장소가 없습니다.")
        st.caption(f"방문 장소 {len(candidates)}곳 · 좌표 미확인 {len(candidates)}곳 제외")
        return

    colors = ("#087f75", "#d65b45", "#3467a5", "#9b5a91", "#8b6b2e")
    color_by_day = {
        day: colors[index % len(colors)] for index, day in enumerate(day_numbers)
    }
    center = (
        sum(point[3] for point in map_places) / len(map_places),
        sum(point[4] for point in map_places) / len(map_places),
    )
    map_view = folium.Map(
        location=center,
        zoom_start=12,
        tiles="OpenStreetMap",
        control_scale=True,
        width="100%",
        height=520,
    )
    line_points: dict[int, list[tuple[float, float]]] = {}
    marker_colors = {
        "관광": "#087f75",
        "식사": "#d65b45",
        "점심 식사": "#d65b45",
        "저녁 식사": "#a63f30",
        "카페·휴식": "#3467a5",
    }
    for day, visit_order, activity, latitude, longitude in map_places:
        line_points.setdefault(day, []).append((latitude, longitude))
        kind = activity.get("type", "장소")
        label_color = marker_colors.get(kind, color_by_day[day])
        marker = folium.Marker(
            location=(latitude, longitude),
            tooltip=f"{day}일 차 · {visit_order}번 · {escape(kind)}",
            icon=folium.DivIcon(
                html=(
                    '<div style="background:' + label_color
                    + ';color:white;border:2px solid white;border-radius:50%;'
                    + 'width:30px;height:30px;line-height:26px;text-align:center;'
                    + 'font-weight:700;box-shadow:0 1px 4px #444">'
                    + str(visit_order)
                    + "</div>"
                ),
                icon_size=(30, 30),
                icon_anchor=(15, 15),
            ),
        )
        name = escape(activity.get("name", "장소 정보 없음"))
        address = escape(activity.get("address", "주소 정보가 제공되지 않았습니다."))
        url = escape(activity.get("url", ""), quote=True)
        popup_html = (
            f"<b>{escape(kind)} · {day}일 차 {visit_order}번</b><br>"
            f"{name}<br>{address}<br>"
            f"<a href=\"{url}\" target=\"_blank\" rel=\"noopener noreferrer\">카카오맵 열기</a>"
        )
        marker.add_child(folium.Popup(popup_html, max_width=300))
        marker.add_to(map_view)

    for day, points in line_points.items():
        if len(points) >= 2:
            folium.PolyLine(
                points,
                color=color_by_day[day],
                weight=4,
                opacity=0.75,
                tooltip=f"{day}일 차 동선 · 직선 연결 (실제 경로 아님)",
            ).add_to(map_view)

    if len(map_places) > 1:
        map_view.fit_bounds(
            [(point[3], point[4]) for point in map_places], padding=(18, 18)
        )
    st.caption(
        "마커 색은 장소 유형(관광·식사·카페)을, 선 색은 날짜를 구분합니다. "
        "선은 방문 순서의 직선 연결이며 실제 도로·대중교통 경로가 아닙니다."
    )
    st_folium(
        map_view,
        height=520,
        use_container_width=True,
        returned_objects=[],
        key=f"{widget_prefix}_itinerary_folium_map",
    )
    st.caption(
        f"선택 일정 방문 장소 {len(candidates)}곳 · 지도 표시 {len(map_places)}곳 · "
        f"좌표 미확인/오류 {len(candidates) - len(map_places)}곳 제외"
    )


def run_search(
    query: str,
    companion: str,
    period: str,
    video_order: str,
    location: dict[str, str],
    selected_region: str | None = None,
    travel_duration: str = TRAVEL_DURATIONS[0],
) -> tuple[dict, dict, dict, dict]:
    search_topic = " ".join(part for part in (selected_region, query) if part).strip()
    needed = duration_to_days(travel_duration) * 2
    tourism_response = search_tourism(query, get_tour_api_key(), location, max_results=needed)
    restaurant_response = search_restaurants(
        query, get_kakao_api_key(), selected_region, location
    )
    naver_client_id, naver_client_secret = get_naver_api_credentials()
    blog_response = search_blogs(
        search_topic, companion, period, naver_client_id, naver_client_secret
    )
    video_response = search_videos(
        search_topic,
        companion,
        period,
        video_order,
        get_youtube_api_key(),
        travel_duration=travel_duration,
    )
    return tourism_response, restaurant_response, blog_response, video_response


def render_place_cards(places: list[Place], city: str, section: str) -> None:
    if not places:
        st.info("표시할 예시 데이터가 없습니다.")
        return

    columns = st.columns(min(len(places), 3), gap="medium")
    for index, place in enumerate(places):
        with columns[index % len(columns)]:
            with st.container(border=True):
                data_kind = place.get("data_kind", "example")
                image_url = place.get("image_url")
                if image_url:
                    st.image(str(image_url), use_container_width=True)
                elif data_kind == "actual":
                    st.info("이미지가 제공되지 않았습니다.")
                else:
                    image_path = place.get("image_path") or "assets/destination-illustration.svg"
                    st.image(
                        str(ASSET_DIR / image_path),
                        caption="예시 일러스트 · 실제 장소 사진 아님",
                        use_container_width=True,
                    )
                st.caption(place.get("category", "유형 정보가 제공되지 않았습니다."))
                st.markdown(f"**{place.get('name', '장소 정보 없음')}**")
                st.caption(f"주소: {place.get('address') or '주소 정보가 제공되지 않았습니다.'}")
                st.write(place.get("description") or "소개 정보가 제공되지 않았습니다.")
                if place.get("phone"):
                    st.caption(f"전화번호: {place.get('phone')}")
                with st.popover("상세보기"):
                    st.markdown(
                        f"**{place.get('region', city)} · {section} · "
                        f"{place.get('category', '유형 정보 미제공')}**"
                    )
                    st.write(
                        place.get(
                            "recommendation_reason", "추천 근거 정보가 제공되지 않았습니다."
                        )
                    )
                    st.caption(f"출처: {place.get('source', '미제공')}")
                    if place.get("detail_url"):
                        link_label = "카카오맵 상세정보" if place.get("source") == "카카오 로컬 API" else "원본 상세정보"
                        st.link_button(link_label, place.get("detail_url"))
                    if data_kind == "example":
                        st.warning("예시 데이터이며 실제 검색 결과가 아닙니다.")
                    else:
                        st.caption("API 검색 결과이며 추천 적합성과 운영 정보가 별도로 검증되지는 않았습니다.")
                        if companion_match_is_limited(place):
                            st.info(
                                "동행자 맞춤 기준 정보가 제한적입니다. 편의시설과 접근성은 확인되지 않았습니다."
                            )


def render_review_cards(result: ContentSearchResult) -> None:
    if result.get("status") != "success":
        st.info(result.get("message", "관련 블로그 게시글을 찾지 못했습니다."))
        if result.get("status") == "not_configured":
            st.caption("네이버 검색 API 키 설정 방법은 API_SETUP.md를 확인해 주세요.")
        return
    if result.get("message"):
        st.warning(result.get("message"))

    raw_items = result.get("items", [])
    reviews = [item for item in raw_items if isinstance(item, dict)] if isinstance(raw_items, list) else []
    st.caption(f"최신순 · 검색 키워드: {result.get('primary_keyword', '미제공')}")
    if result.get("expanded"):
        st.info("동행자 조건의 게시글이 부족해 일반 여행 후기 검색 결과도 포함했습니다. 카드별 검색 범위를 확인하세요.")
    if not reviews:
        st.info("선택한 기간에 관련 블로그 게시글이 없습니다.")
        return

    page_key = "blog_offset"
    offset = st.session_state.get(page_key, 0)
    visible_reviews = reviews[offset : offset + CONTENT_PAGE_SIZE]
    columns = st.columns(min(len(visible_reviews), 3), gap="medium")
    for index, review in enumerate(visible_reviews):
        with columns[index % len(columns)]:
            with st.container(border=True):
                st.caption(
                    "일반 여행 후기 검색 결과"
                    if review.get("expanded_search")
                    else "관련 블로그 게시글 · 방문 경험 미검증"
                )
                st.write(review.get("title") or "게시글 제목 미제공")
                st.write(review.get("summary") or "요약 정보가 제공되지 않았습니다.")
                st.caption(f"작성일: {review.get('published_at') or '미제공'}")
                st.caption(f"출처: {review.get('source_name') or '네이버 블로그'}")
                st.caption(f"검색 키워드: {review.get('search_keyword') or '미제공'}")
                if review.get("url"):
                    st.link_button("원본 후기", review.get("url"))

    next_offset = offset + len(visible_reviews)
    if next_offset < len(reviews) and st.button(
        f"더 보기 ({next_offset}/{len(reviews)})", key="more_blog_results"
    ):
        st.session_state[page_key] = next_offset
        st.rerun()


def render_video_cards(
    result: ContentSearchResult, order: str, travel_duration: str
) -> None:
    if result.get("status") != "success":
        st.info(result.get("message", "관련 YouTube 영상을 찾지 못했습니다."))
        if result.get("status") == "not_configured":
            st.caption("YouTube Data API 키 설정 방법은 API_SETUP.md를 확인해 주세요.")
        return
    if result.get("message"):
        st.warning(result.get("message"))

    raw_items = result.get("items", [])
    videos = [item for item in raw_items if isinstance(item, dict)] if isinstance(raw_items, list) else []
    raw_unconfirmed = result.get("unconfirmed_items", [])
    unconfirmed_videos = [item for item in raw_unconfirmed if isinstance(item, dict)] if isinstance(raw_unconfirmed, list) else []
    raw_comparisons = result.get("comparison_items", [])
    comparison_videos = [item for item in raw_comparisons if isinstance(item, dict)] if isinstance(raw_comparisons, list) else []
    st.caption(
        f"{order} · 선택 기간: {travel_duration} · "
        f"검색 키워드: {result.get('primary_keyword', '미제공')}"
    )
    st.caption("게시일은 영상 업로드 날짜이며 촬영일이나 현재 여행 중임을 의미하지 않습니다.")
    st.caption("여행 기간 분류는 제목과 설명을 기준으로 하며 영상 내용 자체를 확인한 것은 아닙니다.")
    if result.get("message"):
        st.info(result.get("message"))

    all_videos = videos + unconfirmed_videos + comparison_videos
    if not all_videos:
        st.info("선택하신 여행 기간에 맞는 영상을 찾지 못했습니다.")
        return

    page_key = "video_offset"
    offset = st.session_state.get(page_key, 0)
    visible_videos = all_videos[offset : offset + CONTENT_PAGE_SIZE]
    visible_ids = {video.get("url") for video in visible_videos}
    visible_groups = (
        ("여행 기간 일치", [video for video in videos if video.get("url") in visible_ids]),
        ("여행 기간 미확인", [video for video in unconfirmed_videos if video.get("url") in visible_ids]),
        ("여러 여행 일정 비교", [video for video in comparison_videos if video.get("url") in visible_ids]),
    )
    for group_title, group_videos in visible_groups:
        if not group_videos:
            continue
        st.markdown(f"#### {group_title}")
        columns = st.columns(min(len(group_videos), 3), gap="medium")
        for index, video in enumerate(group_videos):
            with columns[index % len(columns)]:
                with st.container(border=True):
                    if video.get("thumbnail_url"):
                        st.image(video.get("thumbnail_url"), use_container_width=True)
                    else:
                        st.info("썸네일이 제공되지 않았습니다.")
                    if group_title == "여행 기간 일치":
                        match_label = (
                            "여행지·동행자·기간 일치"
                            if video.get("match_priority") == 1
                            else "여행지·기간 일치"
                        )
                    elif group_title == "여행 기간 미확인":
                        match_label = "여행 기간 미확인"
                    else:
                        match_label = "여러 기간 일정이 함께 언급되어 별도 분류"
                    st.caption(match_label)
                    st.write(video.get("title") or "영상 제목 미제공")
                    st.caption(f"채널: {video.get('channel_name') or '미제공'}")
                    st.caption(f"게시일: {video.get('published_at') or '미제공'}")
                    st.write(video.get("description") or "설명 정보가 제공되지 않았습니다.")
                    st.caption(f"검색 키워드: {video.get('search_keyword') or '미제공'}")
                    if video.get("url"):
                        st.link_button("영상 보기", video.get("url"))
                        with st.expander("앱에서 재생"):
                            st.video(video.get("url"))

    next_offset = offset + len(visible_videos)
    if next_offset < len(all_videos) and st.button(
        f"더 보기 ({next_offset}/{len(all_videos)})", key="more_video_results"
    ):
        st.session_state[page_key] = next_offset
        st.rerun()


@st.cache_data(ttl=3600, show_spinner=False)
def fetch_place_youtube_v56(destination: str, place_name: str, place_type: str, api_key: str | None) -> dict:
    """Search by the actual selected place, without unrelated trip-duration filtering."""
    if not api_key:
        return {"status": "not_configured", "items": [], "message": "YouTube API 키가 설정되지 않았습니다."}
    kind = "맛집 리뷰" if place_type == "식사" else ("카페 후기" if "카페" in place_type else "방문 후기")
    query = f"{destination} {place_name} {kind}".strip()
    try:
        response = requests.get(
            "https://www.googleapis.com/youtube/v3/search",
            params={"part": "snippet", "q": query, "type": "video", "maxResults": 8,
                    "regionCode": "KR", "relevanceLanguage": "ko", "order": "relevance", "key": api_key},
            timeout=12,
        )
        if not response.ok:
            if response.status_code == 403:
                msg = "YouTube API 요청이 거부되었습니다. API 할당량 및 키 제한을 확인하세요."
            else:
                msg = f"YouTube API 응답 오류 (HTTP {response.status_code})"
            return {"status": "error", "items": [], "message": msg}
        data = response.json()
        items = []
        for item in data.get("items", []):
            video_id = item.get("id", {}).get("videoId")
            snippet = item.get("snippet", {})
            if not video_id or not snippet.get("title"):
                continue
            items.append({"title": snippet["title"], "channel": snippet.get("channelTitle", ""),
                          "url": f"https://www.youtube.com/watch?v={video_id}",
                          "published": snippet.get("publishedAt", "")[:10]})
        return {"status": "success" if items else "empty", "items": items,
                "message": "" if items else "이 장소의 검색 결과가 없습니다.", "query": query}
    except (requests.RequestException, ValueError):
        return {"status": "error", "items": [], "message": "YouTube 연결 또는 응답 처리에 실패했습니다."}


def render_itinerary_place_media(
    itinerary: list, destination: str, companion: str,
    period: str, video_order: str, duration: str,
) -> None:
    """Show media for the actual editable itinerary, including restaurants and cafes."""
    st.subheader("내 여행 일정에 맞는 후기 · 영상")
    st.caption("현재 일정에 포함된 관광지·음식점·카페를 선택하세요. 장소를 추가·교체·삭제하면 검색 대상도 바뀝니다.")
    visits = []
    for day, activities in itinerary:
        for index, activity in enumerate(activities):
            if is_editable_visit(activity) and str(activity.get("name", "")).strip():
                visits.append((day, index, activity))
    if not visits:
        st.info("일정에 장소를 추가하면 후기와 영상을 검색할 수 있습니다.")
        return

    options = [f"{day}일 차 · {a.get('type', '방문')} · {a.get('name', '')}" for day, _, a in visits]
    signature = tuple(options)
    if st.session_state.get("trip_media_options_signature") != signature:
        st.session_state.trip_media_options_signature = signature
        st.session_state.trip_media_selected = options[0]
    selected_label = st.selectbox("후기·영상을 확인할 방문 장소", options, key="trip_media_selected")
    day, _, place = visits[options.index(selected_label)]
    place_name = str(place.get("name", "")).strip()
    place_type = str(place.get("type", ""))
    st.caption(f"선택: {day}일 차 · {place_type} · {place_name}")

    review_tab, video_tab = st.tabs(["블로그 후기", "YouTube 영상"])
    if place_type == "식사":
        review_suffix, button_label = "맛 메뉴 가격 방문 후기", "네이버에서 음식점 맛 후기 보기 ↗"
    elif "카페" in place_type:
        review_suffix, button_label = "커피 디저트 분위기 후기", "네이버에서 카페 후기 보기 ↗"
    else:
        review_suffix, button_label = "방문 여행 후기", "네이버에서 관광지 후기 보기 ↗"
    review_query = f"{destination} {place_name} {review_suffix}".strip()
    with review_tab:
        st.caption(f"검색 장소: {place_name} · 네이버 블로그/VIEW 검색")
        st.link_button(button_label, "https://search.naver.com/search.naver?" + urlencode({
            "where": "view", "query": review_query,
        }))
        naver_id, naver_secret = get_naver_api_credentials()
        if naver_id and naver_secret:
            # Existing Naver API helper is retained; search is scoped to the selected place.
            with st.spinner("네이버 후기 검색 중..."):
                try:
                    result = search_blogs(f"{destination} {place_name}", companion, period, naver_id, naver_secret)
                    render_review_cards(result)
                except Exception:
                    st.warning("네이버 API 결과를 표시하지 못했습니다. 위 검색 링크를 이용해 주세요.")
        else:
            st.info("네이버 API 키 없이 이용합니다. 버튼을 누르면 장소별 검색 결과가 열립니다.")

    with video_tab:
        youtube_query = f"{destination} {place_name} " + (
            "맛집 리뷰" if place_type == "식사" else ("카페 후기" if "카페" in place_type else "방문 후기")
        )
        st.link_button("YouTube에서 영상 더 찾아보기 ↗", "https://www.youtube.com/results?" + urlencode({
            "search_query": youtube_query,
        }))
        with st.spinner("선택한 장소의 YouTube 영상 검색 중..."):
            result = fetch_place_youtube_v56(destination, place_name, place_type, get_youtube_api_key())
        if result.get("message"):
            st.info(result["message"])
        for idx, video in enumerate(result.get("items", [])):
            st.markdown(f"**{idx + 1}. {video['title']}**")
            st.caption(f"채널: {video['channel']} · 게시일: {video['published'] or '미제공'}")
            st.video(video["url"])
        st.caption("영상은 검색 결과입니다. 실제 방문 여부나 영상 내용의 정확성은 별도로 검증하지 않았습니다.")


def render_section_heading(number: str, label: str, title: str) -> None:
    st.markdown(f'<div class="section-label">{number} · {label}</div>', unsafe_allow_html=True)
    st.subheader(title)


st.set_page_config(
    page_title="TRIP FINDER | 국내 여행 검색",
    page_icon="✦",
    layout="wide",
)

st.markdown(
    """
    <style>
    :root {
        --ink: #183b43;
        --muted: #60777b;
        --accent: #087f75;
        --coral: #e87558;
        --line: #dce8e6;
    }
    .stApp { background: #f7faf9; color: var(--ink); }
    section.main > div.block-container {
        max-width: 1160px;
        padding-top: 2rem;
        padding-bottom: 4rem;
    }
    h1, h2, h3 { color: var(--ink); }
    .brand-mark {
        color: var(--accent);
        font-size: 0.8rem;
        font-weight: 800;
        letter-spacing: 0.12em;
        margin-bottom: 0.5rem;
    }
    .intro-copy { color: var(--muted); font-size: 1.05rem; }
    .demo-note {
        border-left: 4px solid var(--coral);
        background: #fff3ee;
        border-radius: 4px;
        color: #66473f;
        padding: 0.85rem 1rem;
        margin: 1rem 0 1.5rem;
    }
    .section-label {
        color: var(--accent);
        font-size: 0.76rem;
        font-weight: 800;
        letter-spacing: 0.08em;
        text-transform: uppercase;
    }
    .results-title { margin-top: 2rem; }
    [data-testid="stImage"] img { border-radius: 5px; }
    [data-testid="stForm"] {
        background: white;
        border: 1px solid var(--line);
        border-radius: 8px;
        padding: 1.2rem 1.25rem 0.5rem;
    }
    [data-testid="stVerticalBlockBorderWrapper"] {
        border-color: var(--line);
        background: white;
    }
    /* 폼 라벨과 입력창을 명확히 구분하여 라이트/다크 테마 대비 유지 */
    [data-testid="stForm"] [data-testid="stWidgetLabel"] p,
    [data-testid="stForm"] label p {
        color: #24414a !important;
        opacity: 1 !important;
        font-weight: 650;
    }
    [data-testid="stForm"] [data-testid="stCaptionContainer"] p {
        color: #52666c !important;
        opacity: 1 !important;
    }
    [data-testid="stForm"] [data-testid="stVerticalBlock"] { gap: 0.65rem; }
    [data-testid="stForm"] [data-testid="stHorizontalBlock"] { align-items: start; }
    [data-testid="stForm"] [data-testid="stColumn"] { min-width: 0; }
    [data-testid="stForm"] [data-testid="stExpander"] { margin-top: 0.25rem; }
    [data-testid="stForm"] [data-testid="stFormSubmitButton"] button {
        min-height: 46px;
        margin-top: 0.35rem;
        font-weight: 700;
    }
    /* 모바일 브라우저: 가로 넘침 방지 및 터치 조작 최적화 */
    html, body, [data-testid="stAppViewContainer"] { overflow-x: clip; }
    [data-testid="stImage"] img { max-width: 100%; height: auto; }
    [data-testid="stForm"] input, [data-testid="stForm"] button,
    [data-testid="stForm"] [role="combobox"] { min-height: 44px; }
    @media (max-width: 768px) {
        section.main > div.block-container {
            padding: 1rem 0.75rem 3rem;
            max-width: 100%;
        }
        h1 { font-size: 1.9rem !important; }
        h2 { font-size: 1.35rem !important; }
        h3 { font-size: 1.15rem !important; }
        .intro-copy { font-size: 0.95rem; }
        .demo-note { font-size: 0.8rem; padding: 0.65rem 0.75rem; }
        [data-testid="stForm"] { padding: 0.8rem 0.7rem; }
        [data-testid="stForm"] [data-testid="stHorizontalBlock"] {
            flex-direction: column !important;
            align-items: stretch !important;
            gap: 0.65rem !important;
        }
        [data-testid="stForm"] [data-testid="stHorizontalBlock"] > [data-testid="stColumn"] {
            min-width: 0 !important;
            width: 100% !important;
            flex: 1 1 auto !important;
        }
        [data-testid="stButton"] button, [data-testid="stDownloadButton"] button {
            min-height: 44px;
        }
        iframe { max-width: 100%; }
    }
    .stButton > button[kind="primary"] {
        background: var(--accent);
        border-color: var(--accent);
        min-height: 2.7rem;
    }
    .stButton > button[kind="primary"]:hover {
        background: #06685f;
        border-color: #06685f;
    }
    </style>
    """,
    unsafe_allow_html=True,
)

st.markdown('<div class="brand-mark">✦ 국내 여행 검색</div>', unsafe_allow_html=True)
st.title("TRIP FINDER")
st.markdown(
    '<p class="intro-copy">가고 싶은 곳을 검색하고, 여행 아이디어를 한 화면에서 살펴보세요.</p>',
    unsafe_allow_html=True,
)
st.markdown(
    '<div class="demo-note"><strong>검색 정보 안내</strong> · API 연결 시 관광지와 주변 음식점은 '
    '외부 검색 결과를 표시합니다. API 미설정·오류·결과 없음으로 예시 데이터를 표시할 때는 '
    '별도 표기하며, 카드 추천은 방문 후기나 접근성 검증을 의미하지 않습니다. '
    '블로그 게시일은 방문일이 아니며 YouTube 게시일은 촬영일을 의미하지 않습니다.</div>',
    unsafe_allow_html=True,
)

# PC에서는 3열 × 2행 균등 배열, 모바일에서는 한 열로 표시합니다.
# Streamlit 폼은 제출 전 선택 변경을 반영하지 않으므로 자녀 연령을 항상 표시하되
# 가족 여행 유형일 때만 일정 추천에 적용합니다.
with st.container():
    row1_destination, row1_party, row1_days = st.columns(3, gap="medium")
    with row1_destination:
        destination_query = st.text_input(
            "여행지",
            placeholder="예: 서울, 경기 용인시, 제주 애월읍, 불국사",
            help="지역명이나 관광지명을 입력하면 TourAPI와 카카오 로컬 API에서 검색합니다.",
        )
    with row1_party:
        party = st.selectbox("누구와 여행하나요?", TRAVEL_PARTIES)
    with row1_days:
        travel_days = st.number_input(
            "여행 일수 (직접 선택)",
            min_value=1,
            max_value=MAX_TRAVEL_DAYS,
            value=2,
            step=1,
            help="1~30일 중 선택하세요. 1일은 당일치기, 5일은 4박 5일로 표시됩니다.",
        )
        duration = days_to_duration(int(travel_days))

    row2_transport, row2_style, row2_child = st.columns(3, gap="medium")
    with row2_transport:
        transport = st.selectbox("이동 수단", TRANSPORT_MODES)
    with row2_style:
        travel_style_primary = st.selectbox("주요 여행 스타일", STYLE_NAMES, key="trip_style_primary_v614")
        travel_style_secondary = st.selectbox(
            "보조 여행 스타일 (선택)",
            ("선택 안 함",) + tuple(s for s in STYLE_NAMES if s != travel_style_primary),
            key="trip_style_secondary_v614",
            help="선택하지 않으면 기존 단일 스타일로 일정이 만들어집니다.",
        )
        travel_style = (travel_style_primary if travel_style_secondary == "선택 안 함"
                        else travel_style_primary + " + " + travel_style_secondary)
    with row2_child:
        family_party = party in ("우리 가족 여행", "3대 가족 여행")
        child_count = st.number_input(
            "자녀 수 (가족 여행만 적용)", min_value=0, max_value=10,
            value=0, step=1, key="trip_child_count_v66",
            disabled=not family_party,
            help="0~10명까지 입력할 수 있습니다. 가족 여행 유형에만 반영됩니다.",
        )

    child_ages = []
    if family_party and int(child_count) > 0:
        st.markdown("**자녀별 연령 선택**")
        age_columns = st.columns(3, gap="medium")
        for idx in range(int(child_count)):
            with age_columns[idx % 3]:
                age = st.selectbox(
                    f"자녀 {idx + 1} 연령", CHILD_AGE_OPTIONS,
                    key=f"trip_child_age_{idx}_v66",
                )
                child_ages.append(age)
        st.caption("여러 연령대가 함께 즐길 수 있는 관광지를 우선 추천합니다. 실제 입장·관람 연령 제한은 방문 전 확인하세요.")

    st.caption(f"선택한 여행 기간: {duration}")
    with st.expander("후기 · 영상 상세 옵션", expanded=False):
        advanced_left, advanced_right = st.columns(2, gap="medium")
        with advanced_left:
            content_period = st.selectbox("후기·영상 기간", PERIOD_OPTIONS, index=1)
        with advanced_right:
            video_order = st.selectbox("YouTube 정렬", VIDEO_ORDER_OPTIONS)

    child_groups = list(dict.fromkeys(child_age_bucket(age) for age in child_ages))
    child_group = ", ".join(child_groups) if child_groups else None
    companion = companion_for_media(party, child_group)
    child_age = child_groups[0] if len(child_groups) == 1 else None
    submitted = st.button("여행지 검색", type="primary", use_container_width=True)

if "search_result" not in st.session_state:
    st.session_state.search_result = None
if "search_error" not in st.session_state:
    st.session_state.search_error = None
if "tourism_response" not in st.session_state:
    st.session_state.tourism_response = None
if "restaurant_response" not in st.session_state:
    st.session_state.restaurant_response = None
if "blog_response" not in st.session_state:
    st.session_state.blog_response = None
if "video_response" not in st.session_state:
    st.session_state.video_response = None
if "restaurant_region_candidates" not in st.session_state:
    st.session_state.restaurant_region_candidates = []
if "pending_search" not in st.session_state:
    st.session_state.pending_search = None
if "destination_location" not in st.session_state:
    st.session_state.destination_location = {}

if submitted:
    st.session_state.trip_search_version = st.session_state.get("trip_search_version", 0) + 1
    st.session_state.trip_edit_search_results = []
    st.session_state.trip_edit_search_error = ""
    if not destination_query.strip():
        st.session_state.search_result = None
        st.session_state.tourism_response = None
        st.session_state.restaurant_response = None
        st.session_state.blog_response = None
        st.session_state.video_response = None
        st.session_state.restaurant_region_candidates = []
        st.session_state.pending_search = None
        st.session_state.destination_location = {}
        st.session_state.search_error = "여행지를 입력한 뒤 검색해 주세요."
    else:
        query = destination_query.strip()
        pending_search = {
            "query": query,
            "companion": companion,
            "party": party,
            "child_group": child_group,
            "child_ages": child_ages,
            "duration": duration,
            "transport": transport,
            "travel_style": travel_style,
            "child_age": child_age,
            "content_period": content_period,
            "video_order": video_order,
        }
        st.session_state.restaurant_region_candidates = []
        st.session_state.selected_kakao_region = None
        location_response = resolve_destination(
            query, get_kakao_api_key(), get_tour_api_key()
        )
        if location_response.get("status") == "ambiguous":
            st.session_state.search_result = None
            st.session_state.pending_search = pending_search
            st.session_state.destination_candidates = location_response.get(
                "candidates", []
            )
            st.session_state.search_error = location_response.get("message")
        else:
            st.session_state.pending_search = None
            st.session_state.search_result = pending_search
            st.session_state.destination_location = location_response.get(
                "location", {}
            )
            with st.spinner("실제 관광지와 주변 음식점을 검색하고 있습니다..."):
                tourism_response, restaurant_response, blog_response, video_response = run_search(
                    query,
                    companion,
                    content_period,
                    video_order,
                    st.session_state.destination_location,
                    travel_duration=duration,
                )
            st.session_state.tourism_response = tourism_response
            st.session_state.restaurant_response = restaurant_response
            st.session_state.blog_response = blog_response
            st.session_state.video_response = video_response
            st.session_state.blog_offset = 0
            st.session_state.video_offset = 0
            st.session_state.restaurant_region_candidates = restaurant_response.get(
                "candidates", []
            )
            st.session_state.search_error = location_response.get("message") or None

if st.session_state.search_error:
    st.warning(st.session_state.search_error)

pending_search = st.session_state.pending_search
if isinstance(pending_search, dict):
    with st.form("destination_region_selection"):
        st.info("같은 이름의 지역이 여러 곳에 있어 정확한 위치를 선택해 주세요.")
        selected_destination_region = st.selectbox(
            "검색할 지역",
            st.session_state.get("destination_candidates", []),
        )
        destination_region_submitted = st.form_submit_button(
            "선택한 지역 검색", type="primary"
        )
    if destination_region_submitted:
        st.session_state.trip_search_version = st.session_state.get("trip_search_version", 0) + 1
        st.session_state.trip_edit_search_results = []
        st.session_state.trip_edit_search_error = ""
        location_response = resolve_destination(
            pending_search.get("query", ""),
            get_kakao_api_key(),
            get_tour_api_key(),
            selected_destination_region,
        )
        st.session_state.search_result = pending_search
        st.session_state.destination_location = location_response.get(
            "location", {}
        )
        with st.spinner("선택한 지역의 관광지와 여행 정보를 검색하고 있습니다..."):
            tourism_response, restaurant_response, blog_response, video_response = run_search(
                pending_search.get("query", ""),
                pending_search.get("companion", COMPANIONS[0]),
                pending_search.get("content_period", PERIOD_OPTIONS[0]),
                pending_search.get("video_order", VIDEO_ORDER_OPTIONS[0]),
                st.session_state.destination_location,
                selected_destination_region,
                travel_duration=pending_search.get("duration", TRAVEL_DURATIONS[0]),
            )
        st.session_state.tourism_response = tourism_response
        st.session_state.restaurant_response = restaurant_response
        st.session_state.blog_response = blog_response
        st.session_state.video_response = video_response
        st.session_state.restaurant_region_candidates = restaurant_response.get(
            "candidates", []
        )
        st.session_state.selected_kakao_region = selected_destination_region
        st.session_state.pending_search = None
        st.session_state.search_error = location_response.get("message") or None
        st.session_state.blog_offset = 0
        st.session_state.video_offset = 0
        st.rerun()

search_result = st.session_state.search_result
if not isinstance(search_result, dict):
    search_result = None
if search_result and st.session_state.restaurant_region_candidates:
    with st.form("restaurant_region_selection"):
        st.info(
            st.session_state.restaurant_response.get(
                "message", "검색 지역을 선택해 주세요."
            )
        )
        selected_region = st.selectbox(
            "정확한 음식점 검색 지역",
            st.session_state.restaurant_region_candidates,
            key=f"kakao_region_{search_result.get('query', '')}",
        )
        region_submitted = st.form_submit_button(
            "선택한 지역의 주변 음식점 검색", type="primary"
        )
    if region_submitted:
        st.session_state.trip_search_version = st.session_state.get("trip_search_version", 0) + 1
        st.session_state.trip_edit_search_results = []
        st.session_state.trip_edit_search_error = ""
        with st.spinner(f"{selected_region} 주변 음식점을 검색하고 있습니다..."):
            restaurant_response = search_restaurants(
                search_result.get("query", ""),
                get_kakao_api_key(),
                selected_region,
                st.session_state.destination_location,
            )
        st.session_state.restaurant_response = restaurant_response
        st.session_state.restaurant_region_candidates = restaurant_response.get(
            "candidates", []
        )
        st.session_state.selected_kakao_region = selected_region

if search_result:
    query = search_result.get("query", "")
    selected_companion = search_result.get("companion", COMPANIONS[0])
    selected_duration = search_result.get("duration", TRAVEL_DURATIONS[0])
    selected_transport = search_result.get("transport", TRANSPORT_MODES[0])
    selected_style = search_result.get("travel_style", TRAVEL_STYLES[0])
    selected_period = search_result.get("content_period", PERIOD_OPTIONS[0])
    selected_video_order = search_result.get("video_order", VIDEO_ORDER_OPTIONS[0])
    selected_age = search_result.get("child_age")
    tourism_response = st.session_state.tourism_response or {}
    restaurant_response = st.session_state.restaurant_response or {}
    actual_attractions = tourism_response.get("places", [])
    actual_restaurants = restaurant_response.get("places", [])
    selected_party = search_result.get("party", "우리 가족 여행")
    selected_child_group = search_result.get("child_group")
    selected_child_ages = search_result.get("child_ages", [])
    ranked_attractions = rank_places_for_party(
        actual_attractions,
        selected_companion,
        selected_style,
        selected_transport,
        selected_age,
        selected_party, selected_child_group, selected_child_ages,
    )
    ranked_restaurants = rank_places_for_party(
        actual_restaurants,
        selected_companion,
        selected_style,
        selected_transport,
        selected_age,
        selected_party, selected_child_group, selected_child_ages,
    )

    st.subheader(f"{query} 여행 일정")
    # 6-12: 여행 조건을 취소선처럼 보이는 한 줄 caption 대신 구분된 정보 칩으로 표시합니다.
    # HTML 값은 escape 처리하고 텍스트 장식은 명시적으로 제거합니다.
    trip_summary_items = [
        ("동행 유형", selected_party),
        ("여행 기간", selected_duration),
        ("이동 수단", selected_transport),
        ("여행 스타일", selected_style),
    ]
    if selected_child_ages:
        trip_summary_items.append(("자녀", f"{len(selected_child_ages)}명 · {', '.join(selected_child_ages)}"))
    summary_chips = "".join(
        '<span style="display:inline-block;background:#ffffff;border:1px solid #dce8e6;'
        'border-radius:7px;padding:7px 11px;color:#183b43;'
        'text-decoration:none !important;line-height:1.5;white-space:normal;">'
        f'<strong style="text-decoration:none !important;">{escape(label)}</strong> '
        f'<span style="text-decoration:none !important;">{escape(str(value))}</span></span>'
        for label, value in trip_summary_items
    )
    st.markdown(
        '<div style="display:flex;flex-wrap:wrap;gap:8px;margin:8px 0 22px;'
        'text-decoration:none !important;">' + summary_chips + '</div>',
        unsafe_allow_html=True,
    )

    location_itinerary = []
    if ranked_attractions:
        days = duration_to_days(selected_duration)
        plan = combined_style_plan(selected_style, selected_party, selected_child_group)
        attractions_per_day = min(plan["attractions"], _transport_profile(selected_transport)["max_attractions"])
        # 전체 거리순 재정렬은 동행 유형별 추천 순위를 무너뜨릴 수 있습니다.
        # 우선순위에 따라 장소를 선택하고, 하루 단위에서만 가까운 순서를 참고합니다.
        style_ranked_attractions = prioritize_experiences(ranked_attractions) if plan["experience"] else ranked_attractions
        style_ranked_attractions = _unique_places(style_ranked_attractions)
        course_attractions = style_ranked_attractions[: days * attractions_per_day]
        # 주변 장소 조회 후 그룹 단위로 가까운 관광지를 묶습니다.
        # 이 단계에서는 동행 유형별 추천 점수 순서를 유지합니다.
        # 검색 버전별로 원본 일정을 한 번만 생성합니다.
        # 편집 버튼으로 Streamlit이 재실행되어도 API를 반복 호출하지 않습니다.
        itinerary_key = st.session_state.get("trip_search_version", 0)
        if st.session_state.get("trip_itinerary_version") != itinerary_key:
            nearby_response = search_nearby_places(
                course_attractions,
                get_kakao_api_key(),
                st.session_state.destination_location,
                selected_transport,
                style_components(selected_style)[0],
            )
            original_itinerary = build_location_itinerary(
                nearby_response.get("groups", []),
                selected_duration,
                selected_style,
                selected_transport,
                selected_party,
                selected_child_group,
                query,
            )
            st.session_state.trip_original_itinerary = deepcopy(original_itinerary)
            st.session_state.trip_editable_itinerary = deepcopy(original_itinerary)
            st.session_state.trip_nearby_message = nearby_response.get("message", "")
            st.session_state.trip_itinerary_version = itinerary_key

        if st.session_state.get("trip_nearby_message"):
            st.info(st.session_state.trip_nearby_message)

        st.subheader("나만의 여행 일정")
        st.caption(
            f"맞춤 일정 기준: {selected_party} / 하루 관광지 {attractions_per_day}곳 · "
            f"선택한 스타일: {selected_style} · 목표 식사 {plan['foods']}곳 · 카페 {plan['cafes']}곳. "
            "후보가 부족하면 목표보다 적게 배치하며, 가까운 관광지끼리 날짜별로 묶습니다. "
            "거리 표기는 직선거리 참고이며 실제 경로 또는 이동시간이 아닙니다. "
            "실제 영업 여부는 확인하지 않았습니다."
        )
        location_itinerary = st.session_state.trip_editable_itinerary
        # 장소 검색/적용을 먼저 처리해 아래 일정표가 최신 세션 상태를 표시하도록 합니다.
        with st.expander("장소 검색 · 일정에 추가 또는 교체", expanded=False):
            render_place_search_editor(location_itinerary, query)
        if st.session_state.get("trip_edit_last_change"):
            st.success("일정 반영 완료: " + st.session_state.trip_edit_last_change)
            st.session_state.trip_edit_last_change = ""
        location_itinerary = st.session_state.trip_editable_itinerary
        render_editable_itinerary(location_itinerary)
        with st.expander("일정 추천 품질 점검", expanded=False):
            st.caption("방문 장소 중복·긴 직선 이동·식사 누락을 점검합니다. 실제 도로 경로, 영업시간, 리뷰 평점은 검증하지 않습니다.")
            quality_notes = _itinerary_quality_notes(location_itinerary, selected_transport)
            if quality_notes:
                for note in quality_notes:
                    st.warning(note)
            else:
                st.success("점검한 항목에서 중복 장소, 이동 수단별 권장 거리 초과, 식사 누락이 발견되지 않았습니다.")
        if len(course_attractions) < days * attractions_per_day:
            st.info(
                f"{days}일 일정에 필요한 관광지가 충분히 검색되지 않았습니다. "
                "확인된 장소만 배치했으며, 비어 있는 날짜는 '장소 검색 · 추가 · 교체'에서 "
                "직접 추가할 수 있습니다."
            )
    else:
        st.info("실제 관광지 검색 결과가 없어 여행 코스를 생성하지 않았습니다.")

    render_itinerary_map(location_itinerary)
    if location_itinerary:
        render_trip_exports(location_itinerary, query, selected_duration, "current")
        st.markdown("---")
        render_itinerary_place_media(
            location_itinerary, query, selected_companion,
            search_result.get("content_period", PERIOD_OPTIONS[0]),
            selected_video_order, selected_duration,
        )

    st.markdown("---")
    with st.expander("추가 여행 참고 정보 · 관광지 / 음식점", expanded=False):
        st.caption("여행 일정을 먼저 확인하고 편집하세요. 아래 정보는 여행 준비 시 참고할 수 있는 전체 지역 검색 결과입니다.")
        if tourism_response.get("status") == "success" and ranked_attractions:
            render_section_heading("01", "TourAPI 실제 결과", "관광지 검색 결과")
            st.caption(
                "추천 순서는 TourAPI 관광지 유형·소개와 기존 동행자/스타일 점수를 이용합니다. "
                "접근성·편의시설 정보는 추정하지 않습니다."
            )
            render_place_cards(ranked_attractions, query, "관광지")
        else:
            st.warning(tourism_response.get("message", "관광지 API 검색 결과가 없습니다."))
            if tourism_response.get("status") == "not_configured":
                st.caption("API 키 설정 방법은 API_SETUP.md를 확인해 주세요.")
            st.info("실제 관광지 검색 결과가 없어 예시 장소를 대신 표시하지 않습니다.")

        if restaurant_response.get("status") == "success" and ranked_restaurants:
            render_section_heading("02", "Kakao Local 실제 결과", "주변 음식점")
            if restaurant_response.get("message"):
                st.warning(restaurant_response.get("message"))
            st.caption(
                "카카오 로컬 API가 제공한 주변 음식점입니다. 평점·방문자 리뷰·영업시간은 표시하거나 추정하지 않습니다."
            )
            render_place_cards(ranked_restaurants, query, "주변 음식점")
        else:
            if restaurant_response.get("status") == "not_configured":
                st.info("카카오 로컬 API가 설정되지 않아 음식점 검색을 건너뜁니다.")
            else:
                st.warning(
                    restaurant_response.get("message", "주변 음식점 API 검색 결과가 없습니다.")
                )

else:
    st.markdown("### 어디로 떠나볼까요?")
    st.caption("검색하면 실제 관광지·주변 음식점, 관련 블로그 게시글과 여행 영상을 확인할 수 있습니다.")
# 파일을 불러온 여행 일정은 신규 검색 여부와 무관하게 확인할 수 있습니다.
render_imported_trip()