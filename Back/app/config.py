import os

from dotenv import load_dotenv

load_dotenv()

ANTHROPIC_API_KEY = os.environ.get("ANTHROPIC_API_KEY", "")
GITHUB_TOKEN = os.environ.get("GITHUB_TOKEN", "")  # optional, raises rate limit
FRONTEND_ORIGIN = os.environ.get("FRONTEND_ORIGIN", "http://localhost:5173")

# PostgreSQL 접속 문자열. 비어 있으면 DB를 쓰지 않는다 —
# 요약 캐시가 꺼지고(매번 LLM 호출) 대화 기능은 503이 된다.
DATABASE_URL = os.environ.get("DATABASE_URL", "")

# 저장소 크기 상한(KB, GitHub의 size 필드 기준). 0이면 제한 없음.
# size는 git 히스토리까지 포함한 값이라 LLM에 보내는 양과는 거의 무관하다
# (트리·파일은 MAX_TREE_ENTRIES/MAX_FILE_CHARS로 이미 잘린다).
# 그래서 기본은 끄고, 거대 저장소의 트리 조회가 문제될 때만 .env에서 켠다.
MAX_REPO_SIZE_KB = int(os.environ.get("MAX_REPO_SIZE_KB", "0"))

# 코드 검색용 임베딩 모델. 로컬에서 도는 ONNX 모델이라 호출 비용이 없다.
# 차원(768)은 schema.sql 의 vector(768) 과 맞아야 한다 — 모델을 바꾸면 둘 다 고치고
# code_chunks 를 비운 뒤 다시 인덱싱할 것.
EMBEDDING_MODEL = os.environ.get("EMBEDDING_MODEL", "jinaai/jina-embeddings-v2-base-code")
EMBEDDING_DIM = int(os.environ.get("EMBEDDING_DIM", "768"))

# fastembed 목록에 없는 모델을 등록할 때 쓰는 둘. 목록에 있는 모델이면 무시된다.
#
# EMBEDDING_MODEL 은 **우리가 붙이는 이름**이고, 가중치를 어디서 어떤 파일로 받을지는
# 이 둘이 정한다. 이름을 분리한 이유는 같은 저장소의 다른 파일(양자화 등)을 쓸 때
# 이름까지 같으면 rule_version() 이 그대로라 **옛 인덱스와 조용히 섞이기** 때문이다.
#   예) EMBEDDING_MODEL=intfloat/multilingual-e5-large-int8
#       EMBEDDING_SOURCE_REPO=intfloat/multilingual-e5-large
#       EMBEDDING_MODEL_FILE=onnx/model_qint8_avx512_vnni.onnx
EMBEDDING_SOURCE_REPO = os.environ.get("EMBEDDING_SOURCE_REPO", "") or EMBEDDING_MODEL
EMBEDDING_MODEL_FILE = os.environ.get("EMBEDDING_MODEL_FILE", "onnx/model.onnx")

# 모델 가중치 캐시 위치. 기본값을 두지 않으면 fastembed 가 OS 임시 폴더에 받는데,
# 윈도우가 그 폴더를 비우면 655MB 를 매번 다시 받는다.
EMBEDDING_CACHE_DIR = os.environ.get(
    "EMBEDDING_CACHE_DIR",
    os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "cache", "models"),
)

# 임베딩 접두어. 모델에 따라 **필수**다 — E5 계열은 질의에 "query: ",
# 문서에 "passage: " 를 붙이지 않으면 검색 품질 측정이 무효가 된다.
# fastembed 의 query_embed() 는 접두어를 붙여주지 않으므로(embed() 를 그대로 호출한다)
# 우리가 직접 붙인다. 접두어가 필요 없는 모델(jina-code)은 빈 문자열이 정답이다.
EMBEDDING_QUERY_PREFIX = os.environ.get("EMBEDDING_QUERY_PREFIX", "")
EMBEDDING_PASSAGE_PREFIX = os.environ.get("EMBEDDING_PASSAGE_PREFIX", "")

# 청크를 넣을 테이블. pgvector 의 vector(N) 은 차원이 고정이라 차원이 다른 모델을
# 한 테이블에 담을 수 없다. 모델을 바꿔 A/B 하려면 차원별 테이블을 쓴다.
#   code_chunks      = 768  (jinaai/jina-embeddings-v2-base-code)
#   code_chunks_1024 = 1024 (intfloat/multilingual-e5-large)
#   code_chunks_384  = 384  (intfloat/multilingual-e5-small)
CHUNK_TABLE = os.environ.get("CHUNK_TABLE", "code_chunks")

# RAG 인덱싱용 소스 수집 상한. 0이면 해당 제한을 끈다.
# 소스는 tarball 한 번으로 통째로 받으므로 GitHub 요청 수가 아니라 크기가 비용이다.
#
# 80MB 로 시작했다가 500MB 로 올렸다 — 3D 에셋이 든 저장소가 여기 걸려
# 코드 검색이 통째로 막혀 있었다.
# tarball 은 이미지·모델 파일까지 포함하는데
# 우리가 뽑아 쓰는 건 소스뿐이라, 아카이브 크기는 실제 인덱싱 부담과 비례하지 않는다.
# (받는 쪽은 SpooledTemporaryFile 이라 큰 파일은 디스크로 넘어간다)
MAX_ARCHIVE_BYTES = int(os.environ.get("MAX_ARCHIVE_BYTES", str(500 * 1024 * 1024)))
MAX_SOURCE_FILE_BYTES = int(os.environ.get("MAX_SOURCE_FILE_BYTES", str(200 * 1024)))
MAX_SOURCE_FILES = int(os.environ.get("MAX_SOURCE_FILES", "3000"))

# 스냅샷에 보관할 소스 원문의 크기 상한. 0이면 제한 없음.
#
# 위 두 상한이 파일 3,000개 × 200KB 라 **이론상 600MB** 다. 실측은 저장소당
# 76KB / 131KB / 2.5MB 라 여기 걸리는 저장소는 드물지만, 상한이 없으면 언젠가 터진다.
# (크기와 근거는 indexer.MAX_HANDOFF_CHARS 와 같다 — 둘 다 "받아 둔 소스 전체를
#  한 번에 들고 있어도 되는가"를 재는 값이다)
#
# **넘으면 한 행도 넣지 않는다. 자르지 않는다.** 일부만 보관하면 도구가 "없습니다"라고
# 답할 때 그것이 "저장소에 없다"인지 "잘려서 없다"인지 구분할 수 없어진다.
# 조용히 반쪽짜리 근거를 주느니 그 스냅샷에서는 보관을 포기하는 쪽이 맞다.
MAX_STORED_SOURCE_BYTES = int(
    os.environ.get("MAX_STORED_SOURCE_BYTES", str(20 * 1024 * 1024))
)

# 작은 저장소 RAG 우회. 소스 본문이 이 토큰 수 이하면 검색 없이 통째로 프롬프트에 넣는다.
# 0이면 우회를 끄고 항상 RAG 를 쓴다.
#
# 비용 규칙에서 역산한 값이다 — "질문 1회당 입력 비용이 RAG 경로의 4배를 넘지 않을 것".
#
#     S_max = (3·C_rag/p_in + snip) / w̄
#
#   C_rag  질문당 RAG 비용 $0.0237 (대화 로그 13질문, 수정된 estimate_cost, 정가)
#   p_in   입력 단가 $3/1M
#   snip   질문당 스니펫 토큰 2,128 (전체 주입에서 사라지는 몫)
#   w̄      질문 하나가 지는 접두사 배율 0.4538 = Σ(1.25+0.1(N−1)) / ΣN
#
# **w̄ 가 이 값을 지배한다.** 실측 세션은 질문 2~6회(평균 3.25)로 짧아서 쓰기 1.25배가
# 잘 나눠지지 않는다. 세션이 길어지면 w̄ 가 떨어져 임계값이 올라간다
# (질문이 전부 6회면 w̄ 0.29 → 약 88,000). 표본이 4세션뿐이므로 실사용 기록이 쌓이면
# 다시 뽑을 것. 산식과 측정은 docs/architecture.md §3 과 docs/evaluation.md §5.
FULL_INJECTION_MAX_TOKENS = int(os.environ.get("FULL_INJECTION_MAX_TOKENS", "57000"))

# 사전 게이트. tarball 을 풀기 전에 소스 바이트 합만 보고 명백히 큰 저장소를 걸러낸다.
# 토큰 수보다 느슨하게 잡는다 — 여기서 떨어뜨린 저장소는 토큰을 세어 보지도 않으므로,
# 조이면 우회 대상이 조용히 사라진다.
FULL_INJECTION_MAX_SOURCE_BYTES = int(
    os.environ.get("FULL_INJECTION_MAX_SOURCE_BYTES", str(500 * 1024))
)

# 색인을 만들 청크 수의 상한. 0이면 제한 없음.
#
# **시간 예산이다.** EC2(t3.medium, 2 vCPU) 실측이 청크당 1.898초라 300청크가 약 9.5분이고,
# 그것이 "첫 방문자가 기다릴 수 있는 시간"으로 정한 값이다. 넘는 저장소는 색인을 만들지
# 않고 요약만으로 답한다(services/indexer.py 의 run_build).
#
# **기계마다 값이 달라야 해서 환경변수다.** 같은 300청크가 로컬에서는 약 57초다.
# 지키려는 것은 청크 수가 아니라 시간이므로, 배포가 자기 기계에 맞춰 정한다.
#
# **청크 수로 재는 이유**: 시간을 정하는 것이 청크 수다. 소스 바이트로 추정할 수도
# 있지만 언어에 따라 문자/청크가 431(java)~708(css)로 1.64배 벌어져, 경계에서
# 12% 초과하거나 상한 아래인 저장소를 잘못 거절한다. 청킹은 실측 0.76초(236청크)라
# 정확히 세는 값이 예산의 2% 도 안 된다.
MAX_INDEX_CHUNKS = int(os.environ.get("MAX_INDEX_CHUNKS", "300"))

# 남용 방지 상한. 어느 값이든 0이면 그 제한을 끈다.
# **서비스 전체(=모든 사용자 합산) 기준이고 로그인이 생겨도 그대로다** — 이것은
# 비용 천장이라 사용자 수와 무관해야 한다. 사용자별 층은 USER_DAILY_LIMIT 이다.
# 실사용 데이터가 없어 넉넉하게 시작하고 .env에서 조인다.
DAILY_LLM_CALL_LIMIT = int(os.environ.get("DAILY_LLM_CALL_LIMIT", "500"))
DAILY_TOKEN_LIMIT = int(os.environ.get("DAILY_TOKEN_LIMIT", "5000000"))
IP_RATE_LIMIT = int(os.environ.get("IP_RATE_LIMIT", "20"))
IP_RATE_WINDOW_SECONDS = int(os.environ.get("IP_RATE_WINDOW_SECONDS", "3600"))

# X-Forwarded-For는 클라이언트가 위조할 수 있다. 프록시(Nginx·Cloudflare 등) 뒤에
# 있을 때만 켤 것. 켜지 않으면 TCP 연결 IP를 쓴다.
TRUST_PROXY_HEADERS = os.environ.get("TRUST_PROXY_HEADERS", "0") == "1"

# 분석을 허용할 저장소 목록. `owner/name` 을 쉼표로 잇는다. **비어 있으면 제한을 끈다**
# (위의 상한들이 0으로 꺼지는 것과 같은 관용구다).
#
# 공개 배포에서 켜지 않으면 URL 을 아는 누구나 LLM 비용과 색인 CPU 를 쓴다.
# **로그인이 꺼진 배포에서는 이 값이 유일한 문이다.** 로그인을 켜면 이 목록은
# 로그인하지 않은 사람에게만 걸리고, 로그인한 사람은 공개 저장소를 무엇이든 넣는다
# (app/services/allowlist.py 머리말에 왜 그렇게 바뀌었는지가 있다).
# 켜는 곳은 배포의 --env-file 하나뿐이고, 로컬 개발은 이 값을 비워 두어 임의 저장소를
# 그대로 넣는다.
#
# **문자열 그대로 둔다. 여기서 파싱하지 않는다.** 두 가지 이유다 —
# tests/test_config.py 가 이 파일의 os.environ.get 호출을 그대로 읽어 .env.example 과
# 대조하는데, frozenset(...) 으로 가공하면 대조할 리터럴이 사라진다.
# 그리고 목록 해석(공백·대소문자·빈 항목)은 비즈니스 규칙이라 services/ 의 몫이다.
# 파싱과 판정은 app/services/allowlist.py 에 있다.
ALLOWED_REPOS = os.environ.get("ALLOWED_REPOS", "")

# ── 로그인 (GitHub OAuth) ────────────────────────────────────────────────────
#
# **비면 로그인 기능이 통째로 꺼진다.** `ALLOWED_REPOS` 가 비면 허용 목록이 꺼지고
# `DAILY_*` 가 0 이면 상한이 꺼지는 것과 같은 관용구다. 꺼진 상태의 동작은 로그인
# 도입 전과 **완전히 같아야 하고**, 그것을 `tests/test_auth_api.py` 가 고정한다.
#
# 배포는 지금 이 값을 비워 둔다 — OAuth App 의 콜백 URL 은 호스트가 정확히 일치해야
# 하는데 인스턴스를 껐다 켤 때마다 퍼블릭 IP 가 바뀐다.
# 도메인이 생긴 뒤에 켠다.
GITHUB_OAUTH_CLIENT_ID = os.environ.get("GITHUB_OAUTH_CLIENT_ID", "")
GITHUB_OAUTH_CLIENT_SECRET = os.environ.get("GITHUB_OAUTH_CLIENT_SECRET", "")

# 로그인 쿠키의 수명(일). 만료는 쿠키가 아니라 `logins.expires_at` 이 판정한다 —
# 쿠키의 Max-Age 는 클라이언트가 들고 있는 값이라 지우고 보낼 수 있다.
LOGIN_SESSION_DAYS = int(os.environ.get("LOGIN_SESSION_DAYS", "14"))

# 로그인한 사용자 한 명의 하루 질문 수 상한. 0이면 이 층을 끈다.
#
# **`DAILY_LLM_CALL_LIMIT`(서비스 전체)을 대체하지 않는다.** 그쪽은 비용 천장이고
# 이쪽은 공평성 장치다 — 한 사람이 천장을 혼자 다 쓰는 것을 막는다. 그래서 값도
# 그쪽에서 역산한다: 서비스 상한 500 을 **최소 5명이 나눠 쓸 수 있어야 한다**로 잡아 100.
# 실사용은 하루 최대 8건이라 여기 닿는 사람은 아직 없다.
USER_DAILY_LIMIT = int(os.environ.get("USER_DAILY_LIMIT", "100"))

# 로그인 쿠키에 `Secure` 를 붙일지. **비우면 `FRONTEND_ORIGIN` 이 https 인지로 정한다.**
#
# 고정 기본값을 두지 않는 이유: `0` 이 기본이면 배포에서 켜는 것을 잊어 평문으로 나가고,
# `1` 이 기본이면 로컬(http)에서 쿠키가 아예 안 실려 로그인이 안 되는데 **아무 오류도
# 안 난다**(브라우저가 조용히 버린다). 출처에서 유도하면 둘 다 자동으로 맞고,
# 명시적으로 덮고 싶으면 `0`/`1` 을 적는다. (`EMBEDDING_SOURCE_REPO` 가 비면
# `EMBEDDING_MODEL` 로 폴백하는 것과 같은 관용구다)
COOKIE_SECURE = (
    os.environ.get("COOKIE_SECURE", "")
    or ("1" if FRONTEND_ORIGIN.startswith("https://") else "0")
) == "1"
