import logging
import time
import uuid

import anthropic
import httpx
from fastapi import APIRouter, HTTPException, Request

from app.db import chats
from app.db.pool import DB_ERRORS
from app.schemas.schemas import AnalyzeRequest, AnalyzeResponse
from app.services import (
    allowlist,
    indexer,
    login_session,
    rate_limit,
    run_log,
    static_analysis,
    summary_cache,
)
from app.services.claude_client import (
    DEFAULT_EFFORT,
    DEFAULT_MODEL,
    MODELS,
    SYSTEM_PROMPT,
    MissingAPIKeyError,
    billable_tokens,
    run_summary,
)
from app.services.context_builder import build_context
from app.services.github_client import (
    RepoAccessError,
    check_repo_access,
    fetch_repo_context,
    fetch_source_files,
    parse_github_url,
    repo_meta,
)

router = APIRouter()

logger = logging.getLogger(__name__)


def _fetch_and_analyze(owner: str, repo: str) -> tuple[dict[str, str] | None, list[dict]]:
    """정적분석용으로 소스를 받아 린터를 돌린다. (받은 소스, 집계) 를 돌려준다.

    **소스를 함께 돌려주는 이유**: 색인도 같은 tarball 이 필요하다. 버리면 새 스냅샷당
    두 번 받게 되는데, 아카이브 다운로드는 core 와 **별개 제한**을 받아 공짜가 아니다
    `_start_indexing` 이 이것을 큐로 넘긴다.

    여기서 받는 이유는 **요약이 코드베이스의 상태를 말하려면 요약 생성 전에 결과가
    있어야 하기 때문**이다. 캐시 히트에는 아예 부르지 않으므로 0회다.

    실패는 삼킨다. 정적분석 때문에 저장소 분석이 실패하면 안 된다.
    """
    try:
        files = fetch_source_files(owner, repo)
    except Exception as e:
        logger.warning("정적분석용 소스를 받지 못했습니다 (%s/%s): %s", owner, repo, e)
        return None, []
    return files, static_analysis.analyze(files)


def _start_indexing(
    snapshot: dict | None, owner: str, repo: str, files: dict[str, str] | None = None
) -> None:
    """코드 색인을 백그라운드로 시작한다. 이미 했거나 진행 중이면 아무 일도 일어나지 않는다.

    요약을 돌려주기 직전에 부른다 — 사용자가 요약을 읽는 동안 진행되고, 첫 질문이
    수십 분을 기다리지 않는다. 실패는 삼킨다(세션 생성과 같은 방침).

    `files` 를 주면 색인이 tarball 을 다시 받지 않는다. 너무 크면 `indexer.start` 가
    조용히 무시하고 스스로 받는다.
    """
    if not snapshot:
        return
    try:
        indexer.start(snapshot["id"], owner, repo, files=files)
    except Exception as e:
        logger.warning("코드 색인을 시작하지 못했습니다 (스냅샷 %s): %s", snapshot["id"], e)


def _is_uuid(value: str | None) -> bool:
    """UUID 가 아닌 값을 DB 에 넘기면 형식 오류가 나 세션 생성 자체가 실패한다.

    /chat 은 같은 상황에서 400 을 내지만 여기서는 **그냥 무시하고 새로 만든다** —
    이 값은 거들 뿐이고, 분석이 그것 때문에 실패하면 안 된다.
    """
    if not value:
        return False
    try:
        uuid.UUID(value)
        return True
    except ValueError:
        logger.info("보낸 session_id 형식이 올바르지 않아 새 세션을 만듭니다.")
        return False


def _start_session(
    snapshot: dict | None, existing: str | None = None, user_id: int | None = None
) -> str | None:
    """후속 질문용 대화 세션. 이어 쓸 수 있으면 재사용하고, 아니면 만든다. 실패하면 None.

    **재사용 조건은 둘이다 — 같은 스냅샷인가, 그리고 같은 주인인가.** 스냅샷이 다르면
    그 세션은 옛 코드를 보고 있으므로 이어 쓰면 안 된다 (세션이 보는 코드 버전은
    스냅샷으로 확정된다).

    ## 주인 조건이 왜 필요한가

    `existing` 은 **클라이언트가 보낸 값**이다(브라우저의 localStorage 에서 온다).
    스냅샷만 보고 재사용하면, 남의 세션 id 를 적어 보내는 것만으로 그 대화를 이어
    쓸 수 있다 — 이력이 프롬프트에 실리고 답변이 그 위에 쌓인다.

    **익명 세션을 로그인한 사람이 이어 쓰지도 못한다**(None ≠ user_id). 그건 결함이
    아니라 의도다 — "로그인하면 내 것으로 가져오기"는 세션 id 를 아는 누구나 남의
    대화를 자기 계정에 붙일 수 있는 가로채기 경로다. 새 대화가 하나 생길 뿐이고,
    옛 익명 대화는 그대로 남는다.

    이 인자가 생기기 전에는 분석할 때마다 새 세션을 만들었고, 프론트가 localStorage 의
    옛 세션을 복원하면 방금 만든 세션은 **메시지 없이 버려졌다.** 서버는 복원 가능
    여부를 알 수 없으니(그 정보는 브라우저에만 있다) 클라이언트가 알려주게 했다.

    세션을 못 만들어도 요약은 그대로 응답한다 — 대화는 부가 기능이고,
    DB 장애로 분석 자체가 실패하면 안 된다. 프론트는 None이면 질문 입력을 숨긴다.
    """
    if not snapshot:
        return None
    try:
        if _is_uuid(existing):
            session = chats.get_session(existing)
            if (
                session
                and session["snapshot_id"] == snapshot["id"]
                and session["user_id"] == user_id
            ):
                return str(session["id"])
        return chats.create_session(snapshot["id"], user_id)
    except DB_ERRORS as e:
        logger.warning("대화 세션을 만들지 못했습니다 (스냅샷 %s): %s", snapshot["id"], e)
        return None


@router.post("/analyze", response_model=AnalyzeResponse)
def analyze(req: AnalyzeRequest, request: Request):
    # 이 요청의 사용자. 로그인이 꺼져 있으면 언제나 None 이고, 그때 아래 흐름은
    # 로그인 도입 전과 글자 그대로 같다.
    user_id = login_session.current_user_id(request)
    try:
        owner, repo = parse_github_url(req.github_url)
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))

    # 허용 목록은 **check_repo_access 앞에서** 본다. 뒤에 두면 차단할 요청마다
    # GitHub GET /repos 를 한 번씩 쓰는데, 토큰이 없는 환경은 시간당 60회라
    # 거절만으로도 서비스가 멎는다. 목록이 비어 있으면 아무 일도 하지 않는다.
    #
    # **check_repo_access 안에 넣지 않은 이유**: 그 함수는 관리자 실험실도 쓴다
    # (api/admin.py). 안에 넣으면 검수용 임의 저장소 분석까지 함께 막힌다.
    #
    # **목록은 로그인하지 않은 요청에만 걸린다.** 통과시키는 것은 여기까지이고 아래
    # 남용 상한·청크 상한은 로그인 여부와 무관하게 그대로 걸린다 — 로그인이 여는 것은
    # **어떤 저장소를 받나**이지 **얼마나 쓰나**가 아니다.
    try:
        allowlist.check(owner, repo, user_id=user_id)
    except allowlist.RepoNotAllowed as e:
        raise HTTPException(status_code=e.status_code, detail=str(e))

    started = time.perf_counter()
    try:
        # LLM 호출 전에 접근 불가·빈 저장소를 먼저 걸러낸다.
        access = check_repo_access(owner, repo)

        # 저장소가 갱신되지 않았으면 GitHub 조회도 LLM 호출도 하지 않는다.
        cached = summary_cache.get(access)
        if cached:
            # 캐시 히트도 색인은 필요하다 — 요약만 재사용할 뿐, 그 스냅샷이 아직
            # 인덱싱 전일 수 있다(색인은 첫 분석 뒤에도 시간이 걸린다).
            _start_indexing(cached, owner, repo)
            run_log.append_cache_hit(
                source="analyze",
                repo=f"{owner}/{repo}",
                model=cached["model"],
                fetch_ms=int((time.perf_counter() - started) * 1000),
                summary=cached["summary"],
            )
            return AnalyzeResponse(
                repo=repo_meta(access),
                summary=cached["summary"],
                # 히트한 스냅샷을 그대로 쓰므로 추가 LLM·GitHub 호출은 없다.
                session_id=_start_session(cached, req.session_id, user_id),
            )

        # 캐시 미스부터 LLM 비용이 든다. 남용 제한은 여기서만 건다.
        rate_limit.check_and_reserve(rate_limit.client_ip(request), user_id)

        ctx = fetch_repo_context(owner, repo, access=access)
    except rate_limit.RateLimitExceeded as e:
        raise HTTPException(
            status_code=e.status_code,
            detail=str(e),
            headers={"Retry-After": str(e.retry_after)},
        )
    except RepoAccessError as e:
        raise HTTPException(status_code=e.status_code, detail=str(e))
    except httpx.HTTPStatusError as e:
        raise HTTPException(
            status_code=502,
            detail=f"GitHub API 오류 ({e.response.status_code}). 잠시 후 다시 시도하세요.",
        )
    # 소스 수집·린터는 GitHub 요청과 같은 성격(외부 I/O)이라 fetch_ms 안에 함께 센다.
    # 받은 소스는 버리지 않고 색인으로 넘긴다 (아래 _start_indexing).
    sources, analysis = _fetch_and_analyze(owner, repo)
    fetch_ms = int((time.perf_counter() - started) * 1000)

    context = build_context(ctx, analysis)
    try:
        result = run_summary(context)
    except MissingAPIKeyError as e:
        raise HTTPException(status_code=503, detail=str(e))
    except anthropic.APIError as e:
        raise HTTPException(status_code=502, detail=f"Claude API 오류: {e}")

    # 관리자 페이지에서 프론트 호출도 함께 비교할 수 있도록 같은 로그에 남긴다.
    run_log.append_run(
        source="analyze",
        repo=f"{owner}/{repo}",
        model=DEFAULT_MODEL,
        effort=DEFAULT_EFFORT if MODELS[DEFAULT_MODEL]["effort"] else None,
        fetch_ms=fetch_ms,
        context=context,
        system_prompt=SYSTEM_PROMPT,
        result=result,
    )
    # context 도 함께 저장한다 — 후속 질문이 GitHub 을 다시 읽지 않고 이 원문을 재사용한다.
    snapshot = summary_cache.put(
        access, model=DEFAULT_MODEL, summary=result["text"], context=context
    )
    rate_limit.record_tokens(billable_tokens(result))
    _start_indexing(snapshot, owner, repo, files=sources)

    return AnalyzeResponse(
        repo=ctx["meta"],
        summary=result["text"],
        session_id=_start_session(snapshot, req.session_id, user_id),
    )
