"""대화가 쓸 도구 — 정의와 실행기.

**왜 도구인가.** 전에는 질문마다 검색을 한 번 돌려 그 결과를 마지막 사용자 메시지에
미리 넣었다. 그 자리는 캐시 브레이크포인트 **뒤**라, 도구를 붙여 라운드트립이 생기면
그 스니펫이 매 호출 정가로 되풀이 청구된다 — 실측 산식으로 라운드트립 3회에 3.78배가
되어 판정 기준 C(3배)를 넘는다. 사전 주입을 빼고 모델이 필요할 때만 부르게 하면 2.11배다.

**claude_client 는 이 모듈을 모른다.** 그쪽은 LLM 어댑터로 두고 DB·검색은 여기 있다.
호출부(api/chat.py)가 스키마와 실행기를 만들어 넘긴다.

**도구 목록은 저장소·요청과 무관하게 항상 같다.** `tools` 는 프롬프트 렌더 위치 0 이라
저장소마다 다른 목록을 주면 캐시 접두사가 갈래로 갈라진다. 보관된 소스가 없는 스냅샷도
도구는 그대로 받고, `read_file`·`grep` 이 "없다"고 답한다.
"""

import logging

from app.db import sources as source_store
from app.db.pool import DB_ERRORS
from app.services import indexer
from app.services.claude_client import count_input_tokens
from app.services.context_builder import estimate_tokens, number_lines

logger = logging.getLogger(__name__)

# 도구 결과 하나의 토큰 상한.
#
# 라운드트립 R 회의 입력은 Σₖ(0.1·P + q + Σᵢ<ₖ(aᵢ + rᵢ)) 라 **결과 크기가 이차로 누적된다.**
# R=3·r=800 이면 기준선의 2.51배로 판정 기준 C(3.0) 안에 남고, r 을 1,500 으로 두면
# 같은 R 에서 3.78배가 되어 넘는다. 남는 0.49배는 effort 가 만드는 사고 토큰
# (출력으로 청구된다)이 추정을 넘길 여지를 덮는 몫이다.
MAX_TOOL_RESULT_TOKENS = 800

# grep 이 돌려줄 줄 수 상한. 한 파일이 결과를 다 차지하지 않게 하는 1차 방어선이고,
# 실제 절단은 위 토큰 상한이 한다.
MAX_GREP_MATCHES = 30

# 보관된 소스가 없는 스냅샷의 답. **예외를 올리지 않는다** — 도구가 죽으면 모델이
# 그 사실을 모른 채 다음 도구로 넘어가거나 지어낸다.
#
# 배포 직후에는 모든 옛 스냅샷이 여기 해당한다(소스 보관은 그 뒤에 생겼고 재색인은 수동이다).
NO_SOURCES = (
    "이 스냅샷에는 보관된 소스가 없습니다 (색인이 소스 보관보다 먼저 만들어졌습니다)."
    " search_code 로 찾으세요."
)

TOOL_SCHEMAS = (
    {
        "name": "search_code",
        "description": (
            "질문과 의미가 가까운 코드 조각을 검색한다. 어디를 봐야 할지 모를 때 먼저 쓴다."
            " 결과 머리글의 (42-58행) 은 그 조각이 원본에서 차지하는 범위이고,"
            " 조각 본문에는 줄 번호가 없다."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "query": {
                    "type": "string",
                    "description": "찾고 싶은 것을 자연어나 식별자로. 질문 문장을 그대로 써도 된다.",
                }
            },
            "required": ["query"],
        },
    },
    {
        "name": "read_file",
        "description": (
            "파일의 지정한 줄 범위를 원문 그대로 읽는다. 각 줄 앞에 `12|코드` 로 실제 줄"
            " 번호가 붙어 나오므로 이 결과의 번호는 그대로 인용해도 된다."
            " 범위는 한 번에 200줄 이하로 좁혀 부를 것 — 넓게 부르면 잘린다."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "path": {
                    "type": "string",
                    "description": "저장소 기준 경로 (예: src/main/java/App.java).",
                },
                "start_line": {"type": "integer", "description": "시작 줄 (1부터)."},
                "end_line": {"type": "integer", "description": "끝 줄 (포함)."},
            },
            # start_line·end_line 은 **편의가 아니라 비용 상한 장치다.** 선택으로 두면
            # 모델이 파일을 통째로 부르고, 그 결과가 라운드트립마다 이차로 누적된다.
            "required": ["path", "start_line", "end_line"],
        },
    },
    {
        "name": "grep",
        "description": (
            "보관된 소스 전체에서 문자열이 든 줄을 찾는다. 정규식이 아니라 부분 문자열이고"
            " 대소문자를 무시한다. 식별자가 어디에 정의·사용되는지 찾을 때 쓴다."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "pattern": {
                    "type": "string",
                    "description": "찾을 문자열 (예: PasswordEncoder).",
                }
            },
            "required": ["pattern"],
        },
    },
)


# 보관 소스가 없는 스냅샷에 줄 목록. **`search_code` 만 남는다.**
#
# 왜 빼는가: `read_file`·`grep` 이 빈손이면 "없습니다"를 돌려주는데, 모델이 그것을
# "저장소에 없다"로 읽는다 — docs/architecture.md §2 의 계약("저장소에 없다"와 "수집 범위에
# 없다"는 다른 말)이 정확히 여기서 깨진다. 부를 수 없는 도구는 아예 안 주는 편이 맞다.
#
# 그리고 **이 경로는 측정된 적이 없다.** 판정($2.13)은 도구 셋이 다 도는 스냅샷에서
# 나왔고 실측 호출 분포가 grep 86 · search_code 83 · read_file 52 였다 —
# 둘이 빠지면 측정한 것과 다른 시스템이다.
SEARCH_ONLY_SCHEMAS = tuple(t for t in TOOL_SCHEMAS if t["name"] == "search_code")


def _truncate(text: str) -> tuple[str, int, bool]:
    """토큰 상한을 넘으면 줄 단위로 자른다. `(결과, 토큰 수, 잘랐는가)`.

    **잘랐다는 사실을 결과 안에 적는다.** 적지 않으면 모델이 잘린 것을 "없다"로 읽는다 —
    그건 거절 축이 재려는 바로 그 실패다.

    **문자 수 추정으로 먼저 거른다.** `estimate_tokens` 는 2.0자/토큰이라 코드에서는
    실제보다 토큰을 많게 잡으므로, 추정이 상한 안이면 실제도 안이다 — 그 경우 토큰 계산
    왕복을 아낀다. 추정이 넘을 때만 실제로 세어 자를 지점을 정한다
    (`_try_full_injection` 이 바이트로 먼저 거르고 토큰으로 판정하는 것과 같은 구조다).

    토큰 수를 함께 돌려주는 이유는 측정이다 — 비용 산식의 `r` 이 실제로 얼마인지는
    이 값으로만 알 수 있고, 지금 산식은 추정치(1,500) 위에 서 있다.
    """
    estimated = estimate_tokens(text)
    if estimated <= MAX_TOOL_RESULT_TOKENS:
        return text, estimated, False

    tokens = count_input_tokens(text) or estimated
    if tokens <= MAX_TOOL_RESULT_TOKENS:
        return text, tokens, False

    lines = text.splitlines()
    keep = max(1, int(len(lines) * MAX_TOOL_RESULT_TOKENS / tokens))
    cut = "\n".join(lines[:keep]) + (
        f"\n\n(결과가 길어 앞 {keep}/{len(lines)}줄만 보냈습니다."
        " 범위나 검색어를 좁혀 다시 부르세요.)"
    )
    return cut, tokens, True


def build(snapshot_id: int) -> tuple[tuple, object]:
    """이 스냅샷에 줄 `(도구 목록, 실행기)`.

    **보관 소스가 없으면 `search_code` 하나만 준다** (`SEARCH_ONLY_SCHEMAS` 참고).

    **이것은 도구 목록 분기다 — 캐시 접두사가 갈린다.** 그래도 되는 이유는
    전체 주입 스냅샷에 도구를 안 붙이는 것과 **같은 성질**이기 때문이다:

    - **스냅샷 속성**이다. 요청 내용이나 저장소 이름이 아니라 그 스냅샷에 보관된
      소스가 있는가로만 갈린다
    - **한 대화 안에서 안 바뀐다.** 세션은 스냅샷 하나에 묶여 있다
    - **단조롭다.** `put_files` 는 0 → N 으로만 가고 되돌아오지 않으므로, 스냅샷
      하나의 생애에서 접두사가 바뀌는 것은 재색인 시점 **한 번**뿐이다

    즉 접두사가 두 갈래로 갈리되 각 스냅샷은 한 갈래에 머문다. 요청마다 흔들리는
    분기가 아니라서 캐시가 깨지지 않는다.

    보관 여부를 읽지 못하면(DB 장애) **적은 쪽으로 떨어진다** — 없는 소스를 읽으라고
    도구를 주는 것보다 검색만 주는 편이 안전하다.
    """
    try:
        stored = source_store.count(snapshot_id) > 0
    except DB_ERRORS as e:
        logger.warning(
            "보관 소스 여부를 읽지 못해 검색 도구만 줍니다 (스냅샷 %s): %s", snapshot_id, e
        )
        stored = False
    schemas = TOOL_SCHEMAS if stored else SEARCH_ONLY_SCHEMAS
    return schemas, build_executor(snapshot_id, has_sources=stored)


def build_executor(snapshot_id: int, has_sources: bool | None = None):
    """이 스냅샷에 묶인 도구 실행기. `(이름, 입력) -> 결과 문자열`.

    **예외를 올리지 않는다.** 도구가 던지면 루프가 그것을 `is_error` 로 감싸 돌려주는데,
    모델이 볼 수 있는 것은 그 문자열뿐이라 여기서 사람이 읽을 문장으로 만들어 주는 편이
    낫다. 진짜 오류(DB 장애)만 루프로 올린다.

    보관 여부는 한 번만 확인하고 기억한다 — 도구를 부를 때마다 세면 대화 하나에 DB 왕복이
    라운드트립 수만큼 늘어난다. 대화 도중에 재색인이 끝나 값이 바뀌어도 다음 질문에 반영된다.
    """
    # `build()` 가 이미 세었으면 그 값을 받는다 — 대화 하나에 DB 왕복이 두 번 될 이유가 없다.
    cached: list[bool | None] = [has_sources]

    def _stored() -> bool:
        if cached[0] is None:
            cached[0] = source_store.count(snapshot_id) > 0
        return cached[0]

    def _search_code(params: dict) -> tuple[str, list[str]]:
        query = (params.get("query") or "").strip()
        if not query:
            return "query 가 비어 있습니다.", []
        found = indexer.search_code(snapshot_id, query)
        return indexer.format_snippets(found) or f"'{query}' 로 검색된 코드가 없습니다.", []

    def _read_file(params: dict) -> tuple[str, list[str]]:
        path = (params.get("path") or "").strip()
        start, end = params.get("start_line"), params.get("end_line")
        if not path:
            return "path 가 비어 있습니다.", []
        if not isinstance(start, int) or not isinstance(end, int):
            return "start_line 과 end_line 을 정수로 지정하세요.", []
        if end < start:
            return f"end_line({end}) 이 start_line({start}) 보다 작습니다.", []

        content = source_store.get_file(snapshot_id, path)
        if content is None:
            if not _stored():
                return NO_SOURCES, []
            return (
                f"'{path}' 는 보관된 소스에 없습니다."
                " 경로가 정확한지 확인하거나 grep 으로 찾으세요."
                " (수집 범위 밖 파일일 수도 있습니다 — 소스가 아닌 확장자, 200KB 초과,"
                " node_modules 같은 디렉터리는 애초에 수집하지 않습니다.)"
            ), []

        lines = content.splitlines()
        start = max(1, start)
        if start > len(lines):
            return f"'{path}' 는 {len(lines)}줄뿐입니다 (요청한 시작 줄 {start}).", []
        chunk = "\n".join(lines[start - 1 : end])
        return (
            f"### {path} ({start}-{min(end, len(lines))}행)\n{number_lines(chunk, start)}",
            [],
        )

    def _grep(params: dict) -> tuple[str, list[str]]:
        pattern = (params.get("pattern") or "").strip()
        if not pattern:
            return "pattern 이 비어 있습니다.", []
        hits = source_store.grep(snapshot_id, pattern, MAX_GREP_MATCHES + 1)
        if not hits:
            if not _stored():
                return NO_SOURCES, []
            return f"'{pattern}' 이 든 줄이 보관된 소스에 없습니다.", []

        clipped = hits[:MAX_GREP_MATCHES]
        body = "\n".join(f"{h['path']}:{h['line']}: {h['text']}" for h in clipped)
        if len(hits) > MAX_GREP_MATCHES:
            body += (
                f"\n\n(일치가 {MAX_GREP_MATCHES}줄을 넘어 여기까지만 보냈습니다."
                " 검색어를 좁히세요.)"
            )
            return body, ["grep_matches"]
        return body, []

    handlers = {
        "search_code": _search_code,
        "read_file": _read_file,
        "grep": _grep,
    }

    def execute(name: str, params: dict) -> str:
        params = params or {}
        entry = {"tool": name, "input": params, "caps": [], "error": False}
        execute.trace.append(entry)

        handler = handlers.get(name)
        if handler is None:
            output, caps = (
                f"'{name}' 은 없는 도구입니다. {', '.join(handlers)} 중에서 고르세요.",
                [],
            )
            entry["error"] = True
        else:
            try:
                output, caps = handler(params)
            except DB_ERRORS as e:
                # DB 장애는 사람이 읽을 문장으로 바꿔 모델에게 알린다. 여기서 예외를
                # 올리면 대화 전체가 실패하는데, 다른 도구는 아직 쓸 수 있을지도 모른다.
                logger.warning(
                    "도구 %s 가 DB 오류로 실패했습니다 (스냅샷 %s): %s", name, snapshot_id, e
                )
                output, caps = f"'{name}' 을 지금 쓸 수 없습니다 (저장소 조회 실패).", []
                entry["error"] = True

        output, tokens, cut = _truncate(output)
        # **둘은 다른 시점의 값이다.** result_tokens 는 자르기 **전** 크기(캡이 무엇을
        # 막았는지), sent_chars 는 실제로 모델에 **간** 크기다. 이름을 뭉뚱그리면
        # 분석이 조용히 틀린다 — 실제로 한 번 어긋나 있었다.
        # 청구된 토큰은 이 둘이 아니라 `_call_loop` 의 호출별 input_tokens 가 정본이다.
        entry["result_tokens"] = tokens
        entry["sent_chars"] = len(output)
        entry["caps"] = [*caps, *(["result_tokens"] if cut else [])]
        return output

    # **도구 호출 내역을 남긴다.** 비용 산식의 a·r 은 아직 추정치(150·1,500) 위에 서
    # 있는데, 그 가정을 실측으로 대체하려면 "어떤 도구를 어떤 인자로 불러 결과가 몇
    # 토큰이었나"가 필요하다. 답변 원문만으로는 재계산할 수 없다.
    #
    # 프로덕션은 이 목록을 읽지 않는다(대화 하나가 끝나면 실행기와 함께 사라진다).
    # 읽는 것은 평가 하네스뿐이고, 거기서 jsonl 에 실린다.
    execute.trace = []
    return execute
