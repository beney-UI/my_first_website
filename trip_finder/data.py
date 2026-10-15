from typing import Literal, TypedDict


class Place(TypedDict, total=False):
    name: str
    x: str
    y: str
    distance_meters: int
    nearby_category: str
    region: str
    category: str
    recommended_companions: list[str]
    travel_styles: list[str]
    description: str
    recommendation_reason: str
    image_url: str | None
    source: str
    verified: bool
    image_path: str
    recommendation_tags: list[str]
    recommended_age_groups: list[str]
    travel_modes: list[str]
    address: str
    phone: str
    detail_url: str | None
    recommendation_basis: list[str]
    data_kind: str


class Review(TypedDict, total=False):
    title: str
    summary: str
    published_at: str | None
    source_name: str | None
    url: str | None
    search_keyword: str
    expanded_search: bool
    content_kind: Literal["actual", "ai_summary", "example"]


class Video(TypedDict, total=False):
    title: str
    published_at: str | None
    thumbnail_url: str | None
    thumbnail_path: str | None
    channel_name: str
    description: str
    url: str | None
    search_keyword: str
    expanded_search: bool
    content_kind: Literal["actual", "placeholder"]
    travel_duration_status: str
    companion_match: bool
    match_priority: int


class CityData(TypedDict):
    attractions: list[Place]
    restaurants: list[Place]
    reviews: list[Review]
    videos: list[Video]


def _place(
    name: str,
    region: str,
    category: str,
    description: str,
    image_path: str,
    companions: list[str],
    styles: list[str],
    reason: str,
    tags: list[str],
    age_groups: list[str],
    travel_modes: list[str],
) -> Place:
    return {
        "name": name,
        "region": region,
        "category": category,
        "recommended_companions": companions,
        "travel_styles": styles,
        "description": description,
        "recommendation_reason": reason,
        "image_url": None,
        "source": "프로젝트 예시 데이터",
        "verified": False,
        "image_path": image_path,
        "recommendation_tags": tags,
        "recommended_age_groups": age_groups,
        "travel_modes": travel_modes,
    }


COMPANIONS = (
    "혼자여행",
    "아빠와 여행",
    "엄마와 여행",
    "가족여행",
    "아이와 여행",
    "연인과 여행",
)

TRAVEL_DURATIONS = ("당일치기", "1박 2일", "2박 3일", "3박 4일")
TRANSPORT_MODES = ("자차", "대중교통")
TRAVEL_STYLES = ("관광 중심", "맛집 중심", "휴식 중심", "체험 중심")
CHILD_AGE_GROUPS = (
    "영유아 (0~2세)",
    "미취학 (3~6세)",
    "초등 저학년 (7~9세)",
    "초등 고학년 (10~12세)",
    "청소년 (13~18세)",
)

CITY_ALIASES = {
    "경주": ("경주", "경주시"),
    "부산": ("부산", "부산광역시"),
    "강릉": ("강릉", "강릉시"),
}

REVIEW_CARD_EXAMPLE: Review = {
    "title": "후기 카드 구성 예시",
    "summary": "실제 방문 후기가 아닙니다. API 연동 후 실제 후기와 출처 정보를 표시합니다.",
    "published_at": None,
    "source_name": None,
    "url": None,
    "content_kind": "example",
}

VIDEO_CARD_EXAMPLE: Video = {
    "title": "영상 카드 구성 예시",
    "published_at": None,
    "thumbnail_path": None,
    "url": None,
    "content_kind": "placeholder",
}

CITY_DATA: dict[str, CityData] = {
    "경주": {
        "attractions": [
            _place("불국사", "경주", "문화유산", "경주의 역사 유적을 둘러보는 예시 장소입니다.", "assets/destination-illustration.svg", ["아빠와 여행", "엄마와 여행", "가족여행", "혼자여행"], ["관광 중심", "휴식 중심"], "역사 문화 관람을 중심으로 구성한 예시입니다.", ["parent_comfort", "culture"], ["초등 저학년 (7~9세)", "초등 고학년 (10~12세)", "청소년 (13~18세)"], ["자차"]),
            _place("첨성대", "경주", "역사 유적", "도심에서 만나는 신라 시대 유적 예시입니다.", "assets/destination-illustration.svg", ["가족여행", "아이와 여행", "연인과 여행", "혼자여행"], ["관광 중심", "체험 중심"], "야외에서 역사 이야기를 살펴보는 예시입니다.", ["easy_access", "child_activity", "transit_friendly"], list(CHILD_AGE_GROUPS), ["자차", "대중교통"]),
            _place("대릉원", "경주", "역사 공원", "고분과 산책 공간을 살펴보는 예시 장소입니다.", "assets/destination-illustration.svg", ["아빠와 여행", "엄마와 여행", "가족여행", "아이와 여행", "연인과 여행"], ["관광 중심", "휴식 중심", "체험 중심"], "산책과 야외 관람을 섞은 예시입니다.", ["parent_comfort", "easy_access", "child_activity"], list(CHILD_AGE_GROUPS), ["자차", "대중교통"]),
        ],
        "restaurants": [
            _place("황리단길 식당가", "경주", "지역 식당가", "다양한 식당이 모인 거리의 예시입니다.", "assets/food-illustration.svg", ["가족여행", "아이와 여행", "연인과 여행", "혼자여행"], ["맛집 중심", "체험 중심"], "여러 식당 선택지를 비교하는 예시입니다.", ["food_variety", "child_friendly"], list(CHILD_AGE_GROUPS), ["자차", "대중교통"]),
            _place("경주 전통 음식", "경주", "지역 음식", "지역 음식을 찾아볼 때의 예시 항목입니다.", "assets/food-illustration.svg", ["아빠와 여행", "엄마와 여행", "가족여행", "혼자여행"], ["맛집 중심"], "지역 음식을 살펴보는 예시 항목입니다.", ["parent_comfort"], list(CHILD_AGE_GROUPS), ["자차"]),
        ],
        "reviews": [REVIEW_CARD_EXAMPLE],
        "videos": [VIDEO_CARD_EXAMPLE],
    },
    "부산": {
        "attractions": [
            _place("해운대 해수욕장", "부산", "해변", "바다 풍경을 즐기는 여행지 예시입니다.", "assets/destination-illustration.svg", ["아빠와 여행", "엄마와 여행", "가족여행", "아이와 여행", "연인과 여행"], ["휴식 중심", "관광 중심", "체험 중심"], "바닷가에서 쉬거나 산책하는 예시입니다.", ["parent_comfort", "easy_access", "child_activity", "transit_friendly"], list(CHILD_AGE_GROUPS), ["자차", "대중교통"]),
            _place("감천문화마을", "부산", "마을·문화", "골목 풍경과 예술 공간을 둘러보는 예시 장소입니다.", "assets/destination-illustration.svg", ["가족여행", "아이와 여행", "연인과 여행", "혼자여행"], ["관광 중심", "체험 중심"], "마을 풍경과 문화 공간을 둘러보는 예시입니다.", ["culture", "child_activity"], ["초등 저학년 (7~9세)", "초등 고학년 (10~12세)", "청소년 (13~18세)"], ["자차", "대중교통"]),
            _place("광안리 해변", "부산", "해변", "해변 산책 코스의 예시 장소입니다.", "assets/destination-illustration.svg", ["아빠와 여행", "엄마와 여행", "가족여행", "아이와 여행", "연인과 여행", "혼자여행"], ["휴식 중심", "관광 중심"], "해변 주변에서 여유를 보내는 예시입니다.", ["parent_comfort", "easy_access", "transit_friendly"], list(CHILD_AGE_GROUPS), ["자차", "대중교통"]),
        ],
        "restaurants": [
            _place("자갈치시장 주변 식당", "부산", "해산물", "해산물 식당을 찾아볼 때의 예시입니다.", "assets/food-illustration.svg", ["아빠와 여행", "엄마와 여행", "가족여행", "혼자여행"], ["맛집 중심", "체험 중심"], "시장 주변의 지역 음식을 살펴보는 예시입니다.", ["food_variety"], ["초등 저학년 (7~9세)", "초등 고학년 (10~12세)", "청소년 (13~18세)"], ["자차", "대중교통"]),
            _place("광안리 식당가", "부산", "지역 식당가", "해변 주변 식당가의 예시 항목입니다.", "assets/food-illustration.svg", ["가족여행", "아이와 여행", "연인과 여행", "혼자여행"], ["맛집 중심", "휴식 중심"], "해변 주변에서 식사 장소를 찾는 예시입니다.", ["easy_access", "child_friendly", "transit_friendly"], list(CHILD_AGE_GROUPS), ["자차", "대중교통"]),
        ],
        "reviews": [REVIEW_CARD_EXAMPLE],
        "videos": [VIDEO_CARD_EXAMPLE],
    },
    "강릉": {
        "attractions": [
            _place("경포해변", "강릉", "해변", "동해 바다를 둘러보는 여행지 예시입니다.", "assets/destination-illustration.svg", ["아빠와 여행", "엄마와 여행", "가족여행", "아이와 여행", "연인과 여행"], ["휴식 중심", "관광 중심", "체험 중심"], "바다를 보며 산책과 휴식을 하는 예시입니다.", ["parent_comfort", "easy_access", "child_activity"], list(CHILD_AGE_GROUPS), ["자차", "대중교통"]),
            _place("오죽헌", "강릉", "문화유산", "강릉의 역사 문화를 살펴보는 예시 장소입니다.", "assets/destination-illustration.svg", ["아빠와 여행", "엄마와 여행", "가족여행", "혼자여행"], ["관광 중심", "체험 중심"], "역사 문화 관람을 중심으로 구성한 예시입니다.", ["culture", "parent_comfort"], ["초등 저학년 (7~9세)", "초등 고학년 (10~12세)", "청소년 (13~18세)"], ["자차"]),
            _place("안목해변", "강릉", "해변", "바다와 카페 거리를 즐기는 예시 장소입니다.", "assets/destination-illustration.svg", ["엄마와 여행", "아빠와 여행", "연인과 여행", "혼자여행"], ["휴식 중심", "맛집 중심"], "바다 풍경과 주변 식음 공간을 함께 살피는 예시입니다.", ["parent_comfort", "easy_access", "transit_friendly"], list(CHILD_AGE_GROUPS), ["자차", "대중교통"]),
        ],
        "restaurants": [
            _place("초당 순두부 마을", "강릉", "지역 음식", "순두부 음식을 찾아볼 때의 예시입니다.", "assets/food-illustration.svg", ["아빠와 여행", "엄마와 여행", "가족여행", "아이와 여행"], ["맛집 중심"], "지역 음식을 살펴보는 예시 항목입니다.", ["parent_comfort", "child_friendly"], list(CHILD_AGE_GROUPS), ["자차"]),
            _place("중앙시장 먹거리", "강릉", "시장 먹거리", "시장 먹거리를 둘러보는 예시 항목입니다.", "assets/food-illustration.svg", ["가족여행", "아이와 여행", "연인과 여행", "혼자여행"], ["맛집 중심", "체험 중심"], "시장의 다양한 먹거리를 살펴보는 예시입니다.", ["food_variety", "child_activity", "transit_friendly"], list(CHILD_AGE_GROUPS), ["자차", "대중교통"]),
        ],
        "reviews": [REVIEW_CARD_EXAMPLE],
        "videos": [VIDEO_CARD_EXAMPLE],
    },
}


def find_city(query: str) -> str | None:
    """Return the supported city matching a Korean destination query."""
    normalized_query = query.strip().replace(" ", "")
    if not normalized_query:
        return None

    for city, aliases in CITY_ALIASES.items():
        if any(alias in normalized_query for alias in aliases):
            return city
    return None
