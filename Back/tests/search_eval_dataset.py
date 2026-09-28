"""코드 검색 품질 평가셋.

목표는 특정 문장 하나를 맞히는 게 아니라 **한국어로 물었을 때 잘 찾는 것**이다.
그래서 질의를 한 기능에 몰지 않고 저장소의 여러 갈래에 걸쳐 흩어 놓았다.
특정 표현에 맞춰 튜닝하면 그 문장만 좋아지고 나머지가 나빠진다.

**정답은 청크 id 가 아니라 (파일 + 포함 문자열)로 적는다.** 청킹 규칙을 바꾸면 청크 경계가
달라져 id 기반 정답은 전부 무효가 되기 때문이다.

질의는 지어내지 않았다. 저장소 소스를 먼저 읽고 **정답이 실제로 있는 것만** 골랐다
(예전에 저장소에 없는 "예약 기능"을 물었다가 판단 불가에 빠진 적이 있다).

## v2 에서 바뀐 것 (2026-08-18)

v1 은 **청크 검색이 아니라 사실상 파일 검색을 재고 있었다.** 정답 조건이 헐거워
정답 파일의 청크 대부분이 정답으로 인정된 질의가 있었다(`id_05`·`py_id_06` 은 100%).
그러면 "그 파일 어딘가만 걸리면 통과"라 청크 단위 개선이 지표에 안 잡힌다.

- `answers` 로 **정답 복수 표현** — 한 곳으로 못 박을 수 없는 질의가 실재한다
  (같은 함수의 v3/v4 처럼 여러 곳이 다 정당한 답인 경우)
- `must_not_contain` 으로 배제 — import 줄이나 모듈 docstring 이 정답으로 인정되던 것을 막는다
- `difficulty: "hard"` + `why_hard` — 검색이 실제로 어려운 질의를 결함과 구분한다
- 질의-정답 정합성 오류 2건 수정 (`py_id_03`·`py_id_06`, 아래 각 주석 참고)

**v2 수치는 v1 보다 낮게 나오는 것이 정상이다** — 품질이 나빠진 게 아니라 측정이
정확해진 것이다. 두 버전을 비교하지 말 것(`EVAL_SET_VERSION` 이 기록에 함께 남는다).

## 거절 질의는 별개다 (`ABSENT_SETS`, 파일 아래쪽)

"정답이 실제로 있는 것만"은 **검색 품질 평가셋의 규칙**이다. 답변 품질에는 반대 방향의
질의도 필요해서 아래에 따로 두었다 — 저장소에 **없는** 기능을 묻고, 답변이 규정대로
거절하는지를 본다. 순위 지표를 잴 수 없으므로 `EVAL_SETS` 에 합치지 않는다.
"""

# 평가셋 버전. 정답 조건을 고치면 올린다 — 그러지 않으면 조건이 다른 두 측정이
# search_eval_log 의 같은 칸에 겹쳐 하나가 사라진다(repo 키를 추가했을 때와 같은 문제).
EVAL_SET_VERSION = 2

EVAL_REPO = ("jjunyuongv", "air")

# kind:
#   "korean"     — 한국어 개념어. 코드에 그 단어가 없을 수 있다. 이번 개선의 주 대상
#   "identifier" — 코드에 실제로 있는 영문 식별자. 이미 잘 되는 편이라 회귀 감시용
#
# 각 질의는 answers 목록을 갖는다. 항목 하나라도 맞으면 정답이다.
#   path_suffix      — 이 문자열로 끝나는 경로
#   must_contain     — 청크 본문에 이 문자열이 있어야 한다
#   must_not_contain — (선택) 이 문자열이 있으면 정답이 아니다
EVAL_QUERIES = [
    # ── 보안 · 인증 ────────────────────────────────────────────────
    {
        "id": "ko_01", "kind": "korean",
        "query": "비밀번호는 어떻게 암호화해?",
        "answers": [
            {"path_suffix": "SecurityConfig.java", "must_contain": "BCryptPasswordEncoder"},
        ],
    },
    {
        "id": "ko_02", "kind": "korean",
        "query": "로그인에 실패하면 어떻게 처리해?",
        "answers": [
            {"path_suffix": "SecurityConfig.java", "must_contain": "AuthenticationFailureHandler"},
        ],
    },
    {
        "id": "ko_03", "kind": "korean",
        "query": "관리자만 들어갈 수 있게 어떻게 막았어?",
        "answers": [{"path_suffix": "SecurityConfig.java", "must_contain": "hasRole"}],
    },
    # ── 전자결재 ───────────────────────────────────────────────────
    {
        "id": "ko_04", "kind": "korean",
        "query": "결재 승인은 어디서 처리해?",
        "answers": [{"path_suffix": "ApprovalController.java", "must_contain": "approve"}],
    },
    {
        "id": "ko_05", "kind": "korean",
        "query": "결재를 반려하는 기능이 있어?",
        "answers": [{"path_suffix": "ApprovalController.java", "must_contain": "reject"}],
    },
    # ── 일정 · 캘린더 ──────────────────────────────────────────────
    {
        "id": "ko_06", "kind": "korean",
        "query": "일정을 저장하는 코드는 어디 있어?",
        "answers": [{"path_suffix": "ScheduleService.java", "must_contain": "saveSchedule"}],
    },
    {
        "id": "ko_07", "kind": "korean",
        "query": "일정 삭제는 어떻게 동작해?",
        "answers": [{"path_suffix": "ScheduleService.java", "must_contain": "deleteSchedule"}],
    },
    # ── 채팅(웹소켓) ───────────────────────────────────────────────
    {
        "id": "ko_08", "kind": "korean",
        "query": "채팅 연결이 끊기면 무슨 일이 일어나?",
        "answers": [{"path_suffix": "ChatHandler.java", "must_contain": "afterConnectionClosed"}],
    },
    # ── 회원 관리 ──────────────────────────────────────────────────
    {
        "id": "ko_09", "kind": "korean",
        "query": "직원 계정은 어디서 등록해?",
        # registerEmployee 는 GET 폼·POST 처리·리다이렉트가 잇달아 나와 청크 3개에 걸친다.
        # 셋 다 등록 흐름의 일부라 그대로 둔다(파일 12청크 중 3개라 변별력은 남는다).
        "answers": [{"path_suffix": "UserController.java", "must_contain": "registerEmployee"}],
    },
    {
        "id": "ko_10", "kind": "korean",
        "query": "회원을 지우는 기능은 어디 있어?",
        "answers": [{"path_suffix": "UserController.java", "must_contain": "deleteUser"}],
    },
    # ── 신고 ───────────────────────────────────────────────────────
    {
        "id": "ko_11", "kind": "korean",
        "query": "신고 내역을 종류별로 보려면 어떻게 해?",
        "answers": [{"path_suffix": "ReportService.java", "must_contain": "ReportsByType"}],
    },
    # ── 유틸 ───────────────────────────────────────────────────────
    {
        "id": "ko_12", "kind": "korean",
        "query": "목록 아래 페이지 번호는 어떻게 만들어?",
        # pagingImg 는 8파일에 나오지만 정의는 PagingUtil 한 곳뿐이고 나머지는 호출부다.
        # "어떻게 만들어?" 의 답은 정의부가 맞다.
        "answers": [{"path_suffix": "PagingUtil.java", "must_contain": "pagingImg"}],
    },

    # ── 영문 식별자 (회귀 감시) ────────────────────────────────────
    {
        "id": "id_01", "kind": "identifier",
        "query": "PasswordEncoder 어디서 만들어?",
        "answers": [
            {"path_suffix": "SecurityConfig.java", "must_contain": "BCryptPasswordEncoder"},
        ],
    },
    {
        "id": "id_02", "kind": "identifier",
        "query": "SecurityFilterChain 설정",
        "answers": [{"path_suffix": "SecurityConfig.java", "must_contain": "SecurityFilterChain"}],
    },
    {
        "id": "id_03", "kind": "identifier",
        "query": "UserDetailsService 구현",
        "answers": [{"path_suffix": "SecurityConfig.java", "must_contain": "UserDetailsService"}],
    },
    {
        "id": "id_04", "kind": "identifier",
        "query": "TextWebSocketHandler 상속",
        "answers": [{"path_suffix": "ChatHandler.java", "must_contain": "TextWebSocketHandler"}],
    },
    {
        # v1 은 "ScheduleRepository 는 어디서 쓰여?" 였고 정답이 `scheduleRepository` 였다.
        # 그 필드는 클래스 헤더부터 모든 메서드에 나와 **파일의 4청크 전부가 정답(100%)** 이었다.
        # 사용처를 묻는 질의는 애초에 "여러 곳"이 답이라 청크 변별이 안 된다 → 호출 하나로 좁혔다.
        "id": "id_05", "kind": "identifier",
        "query": "ScheduleRepository.findAll 은 어디서 호출해?",
        "answers": [
            {"path_suffix": "ScheduleService.java", "must_contain": "scheduleRepository.findAll()"},
        ],
    },
]


MARRYDAY_REPO = ("jjunyuongv", "marryday")

# 두 번째 평가 저장소: Python(FastAPI) + React. 청킹 개선(STEP 2)이 Java 한 저장소에만
# 맞춰진 것은 아닌지 보려고 언어가 다른 곳을 골랐다.
# 여기서도 저장소 소스를 먼저 읽고 **정답이 실재하는 질의만** 채택했다.
MARRYDAY_QUERIES = [
    # ── 체형 분석 ──────────────────────────────────────────────────
    {
        "id": "py_ko_01", "kind": "korean",
        "query": "체형 분석은 어디서 처리해?",
        "answers": [{"path_suffix": "body_analysis.py", "must_contain": "def analyze_body"}],
    },
    {
        "id": "py_ko_02", "kind": "korean",
        "query": "체형 라인은 어떻게 분류해?",
        "answers": [
            {"path_suffix": "body_service.py",
             "must_contain": "classify_body_line_with_gemini"},
        ],
    },
    # ── 합성 · 피팅 ────────────────────────────────────────────────
    {
        "id": "py_ko_03", "kind": "korean",
        "query": "드레스 합성은 어디서 처리해?",
        "answers": [{"path_suffix": "composition.py", "must_contain": "def compose_dress"}],
    },
    {
        "id": "py_ko_04", "kind": "korean",
        "query": "얼굴 부분은 어떻게 합성해?",
        "answers": [{"path_suffix": "fitting_service.py", "must_contain": "def blend_face_patch"}],
    },
    {
        # **어려운 질의다. 결함이 아니다.** v1 에서 278위였고 그대로 남겨 둔다.
        # 정답은 인덱스에 분명히 존재한다(아래 3개 청크). 검색이 못 찾을 뿐이다.
        "id": "py_ko_05", "kind": "korean",
        "query": "의상 영역은 어떻게 분리해?",
        "difficulty": "hard",
        "why_hard": (
            "같은 파일에 거의 동일한 함수 3개(원본·v3·v4)가 있고 docstring 도 사실상 같아 "
            "서로 유사도가 높다 — 표를 나눠 갖는다. 게다가 SegFormer 가 14파일 158회 나와 "
            "같은 주제의 청크 더미에 묻힌다. 파일 자체도 38,705자로 이 저장소에서 가장 크다. "
            "버전별 함수가 여러 개인 저장소는 흔하므로 이 약점은 지표에 남겨 둘 가치가 있다."
        ),
        # v1 은 'def parse_garment_image' 하나였는데 접두 매칭으로 _v3·_v4 까지 걸렸다.
        # 셋 다 의상 영역을 분리하는 정당한 답이므로 **의도를 드러내어 명시**한다.
        "answers": [
            {"path_suffix": "segformer_garment_parser.py",
             "must_contain": "def parse_garment_image("},
            {"path_suffix": "segformer_garment_parser.py",
             "must_contain": "def parse_garment_image_v3("},
            {"path_suffix": "segformer_garment_parser.py",
             "must_contain": "def parse_garment_image_v4("},
        ],
    },
    # ── 이미지 처리 ────────────────────────────────────────────────
    {
        "id": "py_ko_06", "kind": "korean",
        "query": "이미지 필터는 어떻게 적용해?",
        "answers": [
            {"path_suffix": "image_processing.py", "must_contain": "def apply_image_filters"},
        ],
    },
    {
        "id": "py_ko_07", "kind": "korean",
        "query": "사진에 스티커를 붙이는 기능이 있어?",
        "answers": [{"path_suffix": "image_processing.py", "must_contain": "def add_sticker"}],
    },
    # ── 드레스 관리 ────────────────────────────────────────────────
    {
        "id": "py_ko_08", "kind": "korean",
        "query": "드레스를 삭제하는 기능은 어디 있어?",
        "answers": [{"path_suffix": "dress_management.py", "must_contain": "def delete_dress"}],
    },
    {
        "id": "py_ko_09", "kind": "korean",
        "query": "드레스 이미지는 어디서 업로드해?",
        "answers": [{"path_suffix": "dress_management.py", "must_contain": "def upload_dresses"}],
    },
    # ── 관리자 ─────────────────────────────────────────────────────
    {
        "id": "py_ko_10", "kind": "korean",
        "query": "관리자 통계는 어떻게 집계해?",
        "answers": [{"path_suffix": "admin.py", "must_contain": "def get_admin_stats"}],
    },

    # ── 영문 식별자 (회귀 감시) ────────────────────────────────────
    {
        # v1 은 must_contain 이 'SegFormer' 한 단어여서 **모듈 docstring + import 주석 청크
        # (1-10행)까지 정답**이었다. "파싱하는 코드"를 물었는데 import 줄이 답이 되면 안 된다.
        "id": "py_id_01", "kind": "identifier",
        "query": "SegFormer 로 옷을 파싱하는 코드",
        "answers": [
            {"path_suffix": "segformer_garment_parser.py",
             "must_contain": "def parse_garment_image("},
            {"path_suffix": "segformer_garment_parser.py",
             "must_contain": "def parse_garment_image_v3("},
            {"path_suffix": "segformer_garment_parser.py",
             "must_contain": "def parse_garment_image_v4("},
        ],
    },
    {
        # v1 은 'boto3' 한 단어라 파일 18청크 중 10개가 정답이었다(56%).
        # `boto3.client(` 로 좁혀도 그대로였다 — 그 파일은 **함수마다 클라이언트를 새로 만들어**
        # 같은 호출이 7번 나온다. 라이브러리 이름만으로는 청크 변별이 원리상 안 되므로
        # 질의에 동작을 결합했다(식별자 회귀 감시라는 목적은 'boto3' 로 유지된다).
        "id": "py_id_02", "kind": "identifier",
        "query": "boto3 로 S3 에 파일 업로드",
        "answers": [{"path_suffix": "s3_client.py", "must_contain": "def upload_to_s3"}],
    },
    {
        # **v1 은 질의-정답이 어긋나 있었다.** `genai.Client(api_key=...)` 한 줄이 8파일에
        # 12번 나오는데 정답을 body_service.py 로 못 박았다. 그 파일이 특별할 이유가 없고
        # (tryon_service.py 가 4회로 더 많다) 질의에도 파일을 특정할 단서가 없었다 → 134위.
        # 여러 곳이 정당한 답인 질의를 한 곳으로 못 박은 것이라 **질의 의도를 바꿨다.**
        # 키 로테이션을 관리하는 전용 클래스는 core/gemini_client.py 하나뿐이다.
        "id": "py_id_03", "kind": "identifier",
        "query": "Gemini 클라이언트를 어디서 관리해?",
        "answers": [
            {"path_suffix": "gemini_client.py", "must_contain": "genai.Client(api_key=key)"},
        ],
    },
    {
        "id": "py_id_04", "kind": "identifier",
        "query": "pose landmark 시각화",
        "answers": [
            {"path_suffix": "body_analysis.py", "must_contain": "def pose_landmark_visualizer"},
        ],
    },
    {
        "id": "py_id_05", "kind": "identifier",
        "query": "tps_warp 는 어디서 쓰여?",
        "answers": [{"path_suffix": "image_processing.py", "must_contain": "def tps_warp"}],
    },
    {
        # 2a(클래스 헤더 손실)가 Java 밖에서도 해소됐는지 보는 질의 —
        # Pydantic 스키마는 `class X(BaseModel):` 선언이 곧 답이다.
        #
        # **v1 은 정답이 질의와 어긋나 있었다.** tryon_schema.py 를 정답으로 뒀는데 그 파일의
        # 세 클래스는 전부 **Response** 다(UnifiedTryonResponse·V4V5CompareResponse·
        # V4V5CustomCompareResponse). "요청 스키마"를 물었는데 응답 스키마가 정답이었고,
        # 파일 청크 3개가 모두 정답이라 100% 였다.
        # 이름에 Request 가 든 스키마는 저장소 전체에 셋뿐이고, schemas/ 아래 있는 것은
        # ComposeV25Request 하나다.
        "id": "py_id_06", "kind": "identifier",
        "query": "BaseModel 을 상속한 요청 스키마",
        "answers": [
            {"path_suffix": "fitting_schema.py",
             "must_contain": "class ComposeV25Request(BaseModel)"},
        ],
    },
]

APNS4J_REPO = ("teaey", "apns4j")

# 세 번째 평가 저장소: Java APNs 푸시 라이브러리. **작은 저장소 RAG 우회의 검수 대상이다.**
# 앞의 둘은 우회 임계값을 넘어(62,380 · 1,057,764 토큰) 그 경로를 타지 않는다.
#
# 선정 규칙을 요약하면
# 번들 34,467토큰(임계값 대비 여유 65%) · 168청크 · TOP_K 몫 4.8% 다 —
# **우회할 만큼 작으면서 검색이 실제로 골라야 할 만큼은 큰** 구간에 든다.
#
# 앞의 둘과 다른 점 둘:
#   - 웹앱이 아니라 라이브러리다. 층(network/keystore/protocol)이 갈려 질의를 흩기 좋다
#   - **한국어 주석이 없다.** 앞의 둘은 한국어 주석이 한국어 질의를 도왔는데(그래서 CSS 가
#     노이즈가 됐다), 여기는 한국어 질의가 영문 식별자와 맞아야 한다. 임의의 저장소를 받는
#     이 서비스에서는 이쪽이 오히려 일반적인 조건이다
APNS4J_QUERIES = [
    # ── 인증서 · 키스토어 ──────────────────────────────────────────
    {
        "id": "ap_ko_01", "kind": "korean",
        "query": "인증서 파일은 어디서 읽어?",
        "answers": [{"path_suffix": "KeyStoreGetter.java", "must_contain": "keyStore.load("}],
    },
    {
        "id": "ap_ko_02", "kind": "korean",
        "query": "키스토어 비밀번호가 틀리면 어떻게 처리해?",
        # 예외 클래스 파일에도 같은 이름이 있다. `new` 를 붙여 던지는 쪽만 정답으로 둔다.
        "answers": [
            {"path_suffix": "KeyStoreGetter.java",
             "must_contain": "new InvalidKeyStorePasswordException("},
        ],
    },
    # ── SSL · 소켓 ─────────────────────────────────────────────────
    {
        "id": "ap_ko_03", "kind": "korean",
        "query": "SSL 소켓은 어떻게 만들어?",
        "answers": [
            {"path_suffix": "SecuritySocketFactory.java",
             "must_contain": "sslSocketFactory.createSocket("},
            {"path_suffix": "SecuritySocketFactory.java",
             "must_contain": "createSSLSocketFactoryWithTrustManagers(TrustManager[]"},
        ],
    },
    {
        "id": "ap_ko_04", "kind": "korean",
        "query": "서버 인증서를 검증하지 않고 그냥 신뢰하는 부분이 있어?",
        "answers": [
            {"path_suffix": "SecuritySocketFactory.java", "must_contain": "checkServerTrusted"},
        ],
    },
    # ── 전송 · 재시도 ──────────────────────────────────────────────
    {
        "id": "ap_ko_05", "kind": "korean",
        "query": "푸시 전송에 실패하면 몇 번이나 다시 시도해?",
        # 재시도 루프와 기본 횟수 상수가 서로 다른 청크에 있고 **둘 다 정당한 답이다.**
        "answers": [
            {"path_suffix": "ApnsChannel.java", "must_contain": "for (int i = 1; i <= tryTimes; i++)"},
            {"path_suffix": "ApnsChannel.java", "must_contain": "DEFAULT_TRY_TIMES = 3"},
        ],
    },
    {
        "id": "ap_ko_06", "kind": "korean",
        "query": "실패한 알림을 다시 보내는 코드가 있어?",
        "answers": [
            {"path_suffix": "ApnsChannel.java", "must_contain": "cacheWapper.getApnsPayload()"},
        ],
    },
    # ── 페이로드 · JSON ────────────────────────────────────────────
    {
        "id": "ap_ko_07", "kind": "korean",
        "query": "알림 내용을 JSON 문자열로 어떻게 바꿔?",
        # 직렬화 본체(append)와 진입점(toJsonString)이 갈릴 수 있어 둘 다 정답으로 둔다.
        # Payload.toJsonString() 은 JsonParser 로 넘기기만 하므로 정답에서 뺐다.
        "answers": [
            {"path_suffix": "JsonParser.java", "must_contain": "toJsonString(Map<String, Object> root)"},
            {"path_suffix": "JsonParser.java", "must_contain": "static void append(StringBuilder sb"},
        ],
    },
    {
        "id": "ap_ko_08", "kind": "korean",
        "query": "앱 아이콘의 배지 숫자는 어떻게 설정해?",
        "answers": [{"path_suffix": "ApnsPayload.java", "must_contain": "ATTR_BADGE, num"}],
    },
    {
        "id": "ap_ko_09", "kind": "korean",
        "query": "기기 토큰 길이가 잘못되면 어떻게 돼?",
        "answers": [
            {"path_suffix": "ApnsHelper.java",
             "must_contain": "device token string must [64] length not"},
        ],
    },
    {
        "id": "ap_ko_10", "kind": "korean",
        "query": "비동기로 보낼 때 스레드 풀 크기는 어떻게 정해?",
        "answers": [
            {"path_suffix": "ApnsService.java", "must_contain": "Executors.newFixedThreadPool(this.size)"},
        ],
    },
    # ── 식별자 ─────────────────────────────────────────────────────
    {
        "id": "ap_id_01", "kind": "identifier",
        "query": "ApnsGateway 에 정의된 애플 서버 주소",
        "answers": [
            {"path_suffix": "ApnsGateway.java",
             "must_contain": 'PRODUCTION("gateway.push.apple.com", 2195)'},
        ],
    },
    {
        "id": "ap_id_02", "kind": "identifier",
        "query": "PayloadSender 인터페이스를 구현한 클래스",
        # **정답이 둘이다.** 동기 채널과 비동기 서비스가 같은 인터페이스를 구현한다.
        # 하나로 못 박으면 나머지를 찾아도 오답이 된다.
        "answers": [
            {"path_suffix": "ApnsChannel.java",
             "must_contain": "implements Channel, PayloadSender<ApnsPayload>"},
            {"path_suffix": "ApnsService.java",
             "must_contain": "class ApnsService implements PayloadSender<ApnsPayload>"},
        ],
    },
    {
        "id": "ap_id_03", "kind": "identifier",
        "query": "Payload 를 상속한 클래스",
        # air 의 id_04 와 같은 종류다 — 클래스 헤더가 인덱스에 살아 있는지 보는 질의(2a 회귀 감시).
        "answers": [
            {"path_suffix": "ApnsPayload.java", "must_contain": "class ApnsPayload extends Payload"},
        ],
    },
    {
        "id": "ap_id_04", "kind": "identifier",
        "query": "toRequestBytes 가 만드는 바이너리 헤더 구조",
        "answers": [
            {"path_suffix": "ApnsHelper.java", "must_contain": "buffer.putShort((short) 32)"},
        ],
    },
    {
        "id": "ap_id_05", "kind": "identifier",
        "query": "응답 코드로 Status 를 찾는 메서드",
        # 원본의 오타(resolove)를 그대로 쓴다. 철자가 흔들리는 식별자를 찾는지 보는 질의다.
        "answers": [
            {"path_suffix": "ErrorResp.java", "must_contain": "public static Status resolove(int code)"},
        ],
    },
    {
        "id": "ap_id_06", "kind": "identifier",
        "query": "DEF_CHARSET 상수는 어디서 정의해?",
        # 정의는 Protocal 한 곳이고 나머지는 사용처다.
        "answers": [
            {"path_suffix": "Protocal.java", "must_contain": "DEF_CHARSET = Charset.forName"},
        ],
    },
]

# ── 거절 질의 — 저장소에 **없는** 기능을 묻는다 ────────────────────────
#
# 왜 따로 두는가: 위 세 목록에 섞으면 Recall@8 의 분모가 바뀌어 search_eval_log 의
# 기존 기록과 비교가 끊긴다. 그리고 이 질의들은 정답 청크가 없으므로 순위 지표를
# 잴 수 없다 — 재는 것은 **답변이 규정대로 거절했는가**뿐이다.
# 그래서 EVAL_SET_VERSION 은 2 그대로 두고 버전을 따로 매긴다.
#
# **왜 이 축이 필요한가.** 인용 정확도는 정답이 있는 질의에서만 재므로
# "없는 것을 뒤지다 지어내는" 실패를 못 잡는다 — 정답이 없는 질의에서는 짚을 파일이
# 없어 날조와 침묵이 똑같이 0점으로 보인다. 검색을 반복하는 경로(tool use)는 한 번
# 검색하고 끝내는 경로보다 그 여지가 크므로, 그걸 재려면 이 목록이 있어야 한다.
ABSENT_SET_VERSION = 1

# kind: "absent" — 정답이 없다. answers 를 갖지 않는다(is_answer 를 부르면 안 된다).
#
# **질의를 지어내지 않았다.** 저장소 소스 전체를 검색해 부재를 확인하고 why_absent 에
# 근거를 남겼다 — 나중에 같은 방법으로 다시 확인할 수 있다. 전부 그 도메인에
# **있을 법한** 기능이다. 있을 법하지 않은 것을 물으면 거절이 너무 쉬워져 축이 무의미해진다.
#
# ## 부재 판정 규칙 — 낱말 검색만으로 하지 않는다
#
# **기능은 그 기능의 이름으로 존재하지 않는다.** 다른 이름·상수·주소로 구현돼 있을 수
# 있으므로, 낱말이 0회라는 것은 부재의 증거가 아니라 출발점일 뿐이다. 질의를 넣기 전에
# 세 가지를 함께 본다:
#   1. 기능 이름 (예: "proxy", "coupon")
#   2. 그 기능이 쓰는 **식별자·상수·API 이름** (예: `checkValidity`, `getNotAfter`)
#   3. 그 기능이 쓰는 **주소·포트·외부 서비스명** (예: `api.push.apple.com`, 2196)
#
# **반례가 실재한다 — `ap_ab_02`.** `http2`·`HTTP/2` 가 0회라 "HTTP/2 전송이 없다"고
# 적었는데, `ApnsGateway` 에 `RESTFUL_PRODUCTION("api.push.apple.com", 443)` 이 상수로
# 있었다. 프로토콜 **이름**이 아니라 **엔드포인트 주소**로 들어 있었던 것이다.
# 판정("전송 구현은 레거시 바이너리뿐이다")은 맞았지만 근거가 틀렸다.
# 근거가 틀린 채로 질의를 더 넣으면 **다음엔 판정도 틀린다.**
AIR_ABSENT_QUERIES = [
    {
        "id": "air_ab_01", "kind": "absent",
        "query": "출퇴근 기록은 어디서 처리해?",
        "why_absent": "attend·근태·출퇴근 0회. 근태 관리 도메인 자체가 없다",
    },
    {
        "id": "air_ab_02", "kind": "absent",
        "query": "휴가 신청 기능이 있어?",
        "why_absent": "vacation·휴가·leave 0회. 결재는 있지만 휴가 결재 종류가 없다",
    },
    {
        "id": "air_ab_03", "kind": "absent",
        "query": "구글이나 카카오 소셜 로그인은 어떻게 붙였어?",
        "why_absent": "OAuth·소셜·카카오·google 0회. SecurityConfig 는 폼 로그인만 설정한다",
    },
    {
        "id": "air_ab_04", "kind": "absent",
        "query": "새 결재가 오면 알림을 보내는 코드가 있어?",
        "why_absent": "notification·notify·알림 0회. 결재 상태 변경 후 통지 경로가 없다",
    },
    {
        "id": "air_ab_05", "kind": "absent",
        "query": "보고서에 댓글을 다는 기능이 있어?",
        "why_absent": "comment·댓글·reply 0회. Report 엔티티에 댓글 연관이 없다",
    },
    {
        "id": "air_ab_06", "kind": "absent",
        "query": "화면을 영어로 바꾸는 다국어 처리가 있어?",
        "why_absent": "i18n·locale·MessageSource·다국어 0회. 문구가 전부 한국어로 박혀 있다",
    },
]

MARRYDAY_ABSENT_QUERIES = [
    {
        "id": "md_ab_01", "kind": "absent",
        "query": "드레스 대여 결제는 어디서 처리해?",
        "why_absent": "payment·결제·stripe·toss·iamport 0회. 결제 연동이 없다",
    },
    {
        "id": "md_ab_02", "kind": "absent",
        "query": "예약 날짜를 고르는 기능이 있어?",
        "why_absent": (
            "예약·booking 0회. reserv 26회는 전부 LLM 프롬프트의 'Preserve' 다 — "
            "낱말이 걸린다고 기능이 있는 것이 아니다"
        ),
    },
    {
        "id": "md_ab_03", "kind": "absent",
        "query": "카카오 로그인은 어떻게 붙였어?",
        "why_absent": "kakao·카카오·oauth·소셜 0회. routers/auth.py 는 자체 세션 로그인만 한다",
    },
    {
        "id": "md_ab_04", "kind": "absent",
        "query": "장바구니에 드레스를 담는 기능이 있어?",
        "why_absent": "cart·장바구니·wishlist·찜 0회. 드레스는 고르기만 하고 담지 않는다",
    },
    {
        "id": "md_ab_05", "kind": "absent",
        "query": "쿠폰이나 할인 코드를 적용하는 부분이 있어?",
        "why_absent": "coupon·쿠폰·discount·할인·promo 0회. 금액을 다루는 코드가 없다",
    },
    {
        "id": "md_ab_06", "kind": "absent",
        "query": "합성 결과를 PDF 로 내려받는 기능이 있어?",
        "why_absent": "pdf 0회. 결과는 이미지로만 다룬다",
    },
]

APNS4J_ABSENT_QUERIES = [
    {
        "id": "ap_ab_01", "kind": "absent",
        "query": "만료된 기기 토큰을 애플에서 회수하는 피드백 서비스가 있어?",
        "why_absent": "feedback 0회. 전송 채널만 있고 피드백 서비스 연결이 없다",
    },
    {
        "id": "ap_ab_02", "kind": "absent",
        "query": "HTTP/2 로 푸시를 보내는 코드는 어디 있어?",
        # **위 규칙의 반례다.** 낱말 검색만 믿었다가 근거를 틀리게 적었던 질의.
        "why_absent": (
            "전송 구현이 SSLSocket + ApnsHelper.toRequestBytes 의 레거시 바이너리뿐이다. "
            "낱말 http2·HTTP/2 는 0회지만 **그것만으로는 부재의 근거가 못 된다** — "
            "ApnsGateway 에 RESTFUL_PRODUCTION('api.push.apple.com', 443) 이 상수로 있다. "
            "주소는 있고 그 프로토콜로 요청을 만드는 코드가 없는 것이다"
        ),
    },
    {
        "id": "ap_ab_03", "kind": "absent",
        "query": "인증서 만료일을 검사하는 부분이 있어?",
        "why_absent": "checkValidity·getNotAfter 0회. KeyStoreGetter 는 읽어 오기만 한다",
    },
    {
        "id": "ap_ab_04", "kind": "absent",
        "query": "전송 성공·실패 건수를 집계하는 통계 기능이 있어?",
        "why_absent": "metric·statist 0회. stat 73회는 전부 static 키워드다",
    },
    {
        "id": "ap_ab_05", "kind": "absent",
        "query": "프록시 서버를 거쳐 접속하는 설정이 있어?",
        "why_absent": "proxy 0회. SecuritySocketFactory 는 직결 소켓만 만든다",
    },
    {
        "id": "ap_ab_06", "kind": "absent",
        "query": "푸시 우선순위를 지정하는 옵션이 있어?",
        "why_absent": "priority 0회. Payload 에 우선순위 필드가 없다",
    },
]

# 평가 대상 묶음. 하네스가 이걸 순회한다.
EVAL_SETS = {
    "air": {"repo": EVAL_REPO, "queries": EVAL_QUERIES},
    "marryday": {"repo": MARRYDAY_REPO, "queries": MARRYDAY_QUERIES},
    "apns4j": {"repo": APNS4J_REPO, "queries": APNS4J_QUERIES},
}

# 거절 질의는 세트 이름으로만 이어 둔다. EVAL_SETS 에 합치지 않는 이유는 위와 같다 —
# 검색 품질 하네스(test_search_quality)가 이것까지 순회하면 안 된다.
ABSENT_SETS = {
    "air": AIR_ABSENT_QUERIES,
    "marryday": MARRYDAY_ABSENT_QUERIES,
    "apns4j": APNS4J_ABSENT_QUERIES,
}


def is_answer(chunk: dict, case: dict) -> bool:
    """이 청크가 해당 질의의 정답인가. answers 항목 하나라도 맞으면 정답이다.

    한 곳으로 못 박을 수 없는 질의가 실재해서 목록으로 받는다 — 같은 함수의 v3/v4 처럼
    여러 곳이 다 정당한 답인 경우다. 억지로 하나만 고르면 나머지를 찾아도 오답이 된다.
    """
    for answer in case["answers"]:
        if not chunk["path"].endswith(answer["path_suffix"]):
            continue
        if answer["must_contain"] not in chunk["content"]:
            continue
        excluded = answer.get("must_not_contain")
        if excluded and excluded in chunk["content"]:
            continue
        return True
    return False


def expected_label(case: dict) -> str:
    """보고서에 쓸 정답 표기. 여러 개면 첫 항목에 개수를 덧붙인다."""
    first = case["answers"][0]
    label = f"{first['path_suffix']} / {first['must_contain']}"
    if len(case["answers"]) > 1:
        label += f" (외 {len(case['answers']) - 1}개)"
    return label
