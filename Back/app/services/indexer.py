"""코드 인덱싱 — 소스 수집 → 청킹 → 임베딩 → 저장.

**백그라운드로 돈다.** /analyze 가 요약을 돌려준 뒤 별도 스레드에서 시작하고,
사용자가 요약을 읽는 동안 진행된다. 동기로 돌리면 첫 질문이 그만큼 멈추는데,
큰 저장소는 임베딩만 수십 분이라(1,683청크 ≈ 29분) 기다릴 수 있는 시간이 아니다.

진행 상태는 DB(snapshot_index_status)에 남긴다 — 화면이 "코드 색인 중 N/M"을
보여줄 근거이자, 실패했을 때 사유를 전달하는 경로다.
"""

import logging
import threading
import time

from app.config import (
    FULL_INJECTION_MAX_SOURCE_BYTES,
    FULL_INJECTION_MAX_TOKENS,
    MAX_INDEX_CHUNKS,
    MAX_STORED_SOURCE_BYTES,
)
from app.core.chunker import chunk_files
from app.core.embeddings import count_tokens, embed_documents, embed_query, input_limit
from app.db import chunks as chunk_store
from app.db import index_status
from app.db import repos as repo_store
from app.db import sources as source_store
from app.db.pool import DB_ERRORS
from app.services.claude_client import count_input_tokens
from app.services.context_builder import build_source_bundle, estimate_tokens
from app.services.github_client import fetch_source_files

logger = logging.getLogger(__name__)

# 검색해서 프롬프트에 넣을 청크 수. 늘릴수록 근거는 많아지지만 입력 토큰이 늘고
# 관련 없는 청크가 섞여 답이 흐려진다.
TOP_K = 8

# 스냅샷별 인덱싱 잠금. 같은 저장소에 요청 두 개가 동시에 들어오면 수집·임베딩을
# 두 번 하게 되므로 직렬화한다. 저장소가 다르면 서로 막지 않는다.
# (DB 의 status 도 중복을 막지만, 그 검사와 표시 사이의 틈을 이 잠금이 닫는다)
_locks: dict[int, threading.Lock] = {}
_locks_guard = threading.Lock()


def _lock_for(snapshot_id: int) -> threading.Lock:
    with _locks_guard:
        return _locks.setdefault(snapshot_id, threading.Lock())


# `/analyze` 가 받아 둔 소스를 큐로 넘길 때의 크기 상한.
#
# 넘기는 이유: 그러지 않으면 새 스냅샷마다 tarball 을 두 번 받는다(정적분석 1회 + 색인 1회).
# 아카이브 다운로드는 core 와 **별개 제한**을 받으므로 공짜가 아니다.
#
# 상한을 두는 이유: 수집 상한이 파일 3,000개 × 200KB 라 **이론상 600MB** 까지 가능한데,
# tarball 한 번을 아끼자고 워커가 처리할 때까지 그만한 메모리를 붙들 이유는 없다.
# 넘으면 넘기지 않고 색인이 스스로 받는다 — 느려질 뿐 결과는 같다.
# (실측: 소스 2.26MB 짜리 저장소가 181파일이다. 이 값에 걸리는 저장소는 드물다)
MAX_HANDOFF_CHARS = 20 * 1024 * 1024


# 상한의 몇 할부터 로그로 알릴지. **데모가 조용히 깨지는 것을 막는 감시선이다.**
#
# 상한을 넘으면 도구가 안 붙고 요약만으로 답하는데, 그 판정은 저장소에 push 가 있어
# 새 스냅샷이 생길 때 일어난다 — 아무도 안 보는 때 바뀌고 아무 신호도 없다.
# 데모 저장소가 그렇게 되면 tool use 경로가 화면에서 사라진다(README 의 스크린샷과
# 인용 정확도 표가 그 경로를 가리킨다).
#
# **0.8 은 "경고를 보고 나서 손 쓸 여유"로 정했다** — 상한의 20% 가 남는다
# (상한 300 이면 60청크 ≈ 28KB 소스). 더 높이면 활주로가 그만큼 짧아진다.
#
# **테스트가 아니라 로그인 이유**: 감시 대상은 살아 있는 저장소인데, 평가셋 캐시는
# 얼어 있는 사본이고(`Back/cache/` 는 커밋되지 않는다) CI 도 없다. 테스트로 만들면
# 한 기계에서만 도는, 자라지 않는 값을 재는 테스트가 된다. 여기는 실제로 색인되는
# 그 스냅샷을 재고, 빌드당 한 줄이라 상시 점등되지도 않는다.
CHUNK_CAP_WARN_RATIO = 0.8


def _small_enough_to_hand_off(files: dict[str, str] | None) -> bool:
    """큐에 들고 있어도 될 크기인가. 문자 수로 잰다 — 코드는 대체로 1자 1바이트다."""
    if not files:
        return False
    return sum(len(c) for c in files.values()) <= MAX_HANDOFF_CHARS


def start(
    snapshot_id: int,
    owner: str,
    repo: str,
    ref: str = "",
    table: str | None = None,
    files: dict[str, str] | None = None,
) -> bool:
    """아직 색인이 없으면 큐에 넣는다. 넣었으면 True.

    **이미 색인이 있으면 아무것도 하지 않는다.** 규칙이 낡았더라도 여기서 자동으로
    다시 만들지 않는다 — 배포 직후 모든 저장소가 한꺼번에 임베딩을 시작하면 그게 곧
    장애다. 재색인은 관리자가 트리거한다(/admin/api/rebuild-index).

    **청크 상한을 넘어 건너뛴 스냅샷도 다시 넣지 않는다.** 같은 소스라 결과가 같다.

    `files` 를 주면 **그것을 쓰고 tarball 을 다시 받지 않는다.** 호출부가 방금 받은
    소스를 넘기는 용도다(`/analyze` 의 정적분석). 너무 크면 조용히 무시하고 색인이
    스스로 받는다 — `MAX_HANDOFF_CHARS` 참고.

    호출부는 결과를 신경 쓰지 않아도 된다 — 실패해도 예외를 올리지 않는다
    (요약·대화가 인덱싱 때문에 막히면 안 된다).
    """
    try:
        if index_status.is_completed(snapshot_id, table=table):
            return False
        # 상한을 넘어 건너뛴 스냅샷은 다시 넣지 않는다. 소스가 고정돼 있어 결과가
        # 같은데, begin() 은 pending/running 만 막으므로 이 검사가 없으면 **질문마다**
        # tarball 을 다시 받아 같은 답을 낸다 (chat.py 가 질문마다 start() 를 부른다).
        if index_status.is_skipped(snapshot_id, table=table):
            return False
    except DB_ERRORS as e:
        logger.warning("인덱싱 상태를 읽지 못해 건너뜁니다 (스냅샷 %s): %s", snapshot_id, e)
        return False

    if files is not None and not _small_enough_to_hand_off(files):
        logger.info(
            "받아 둔 소스가 커서 넘기지 않습니다 (스냅샷 %s) — 색인이 다시 받습니다.",
            snapshot_id,
        )
        files = None

    # 큐를 거치는 이유: 첫 색인도 재색인도 임베딩으로 CPU 를 다 쓴다. 경로가 둘이면
    # 서로 모른 채 동시에 돌아 둘 다 느려진다. (지연 import — 큐가 이 모듈을 부른다)
    from app.services import index_queue

    return index_queue.submit(
        snapshot_id, owner, repo, ref=ref, table=table, files=files
    ) is not None


def run_build(
    build_id: int, snapshot_id: int, owner: str, repo: str, ref: str, table: str | None,
    files: dict[str, str] | None = None,
) -> None:
    """실제 인덱싱. 큐 워커 스레드에서 불린다.

    빌드 행은 호출부(index_queue.submit)가 이미 만들어 뒀다 — 중복 요청을 큐에 넣기
    전에 막아야 해서, 그 판정과 삽입이 한 문장으로 DB 에서 일어난다.

    `files` 가 있으면 **그 소스를 그대로 쓴다.** `/analyze` 가 정적분석을 하려고 방금
    받아 둔 것이라, 다시 받으면 같은 tarball 을 두 번 내려받는 셈이다.
    받은 시점이 요약·정적분석과 같아진다는 부수 효과도 있다 — 스냅샷은 `pushed_at` 으로
    고정돼 있으므로 **같은 시점을 보는 쪽이 오히려 맞다.**
    """
    with _lock_for(snapshot_id):
        started = time.perf_counter()
        try:
            reused = files is not None
            if not reused:
                files = fetch_source_files(owner, repo, ref)
            fetch_ms = int((time.perf_counter() - started) * 1000)

            # 전체 주입 판정보다 **앞**이다. 뒤에 두면 작은 저장소가 여기서 빠져나가
            # "소스가 이미 프롬프트에 다 들어간 저장소에만 원문이 남는" 상태가 그대로 남는다.
            _store_sources(snapshot_id, files)

            if _try_full_injection(build_id, snapshot_id, owner, repo, files):
                return

            # 토큰 한도는 문자 수로 지킬 수 없다(비율이 1.7~17배로 벌어진다).
            # 토크나이저를 넘겨 한도를 넘는 청크만 다시 자르게 한다.
            pieces = chunk_files(
                files, count_tokens=count_tokens, token_limit=input_limit()
            )
            # 청킹은 이미 임베딩 앞이라 여기서 청크 수를 안다. 상한 판정에 필요한 것이
            # 그것뿐이라 큐도 워커도 건드리지 않는다 (실측 236청크 0.76초).
            if _skip_over_cap(build_id, owner, repo, len(pieces)):
                return
            _warn_if_near_cap(owner, repo, len(pieces))
            index_status.set_total(build_id, len(pieces))

            embed_started = time.perf_counter()
            vectors = embed_documents(
                [p["content"] for p in pieces],
                on_progress=lambda done: _report(build_id, done),
            )
            embed_ms = int((time.perf_counter() - embed_started) * 1000)

            saved = chunk_store.insert_chunks(
                build_id, snapshot_id, pieces, vectors, table=table
            )
            # 완료 표시와 포인터 교체가 한 트랜잭션이다. 이 순간부터 검색이 새 청크를 본다.
            index_status.complete(build_id, saved)
            _warn_if_truncated([p["content"] for p in pieces])
            _prune(snapshot_id, table)
        except Exception as e:
            # 소스가 없는 저장소(청크 0개)도 이 경로가 아니라 위에서 completed 로 끝난다.
            # 여기 오는 건 수집·임베딩·저장이 실제로 실패한 경우다.
            # 활성 포인터는 그대로라, 재색인이 실패해도 쓰던 색인은 살아 있다.
            logger.warning("%s/%s 인덱싱 실패: %s", owner, repo, e)
            _mark_failed(build_id, str(e))
            return

        logger.info(
            "%s/%s 색인 빌드 #%d 완료: 파일 %d개 → 청크 %d개 "
            "(수집 %dms%s, 임베딩 %dms, 전체 %dms)",
            owner, repo, build_id, len(files), saved,
            fetch_ms, " — 넘겨받아 재사용" if reused else "", embed_ms,
            int((time.perf_counter() - started) * 1000),
        )


def measure_bundle(files: dict[str, str]) -> tuple[str, int]:
    """소스 번들과 그 토큰 수. count_tokens 가 실패하면 문자 수 추정으로 내려간다.

    추정값은 실제보다 토큰을 **많게** 잡는 쪽이라, 재는 데 실패하면 우회를 덜 켜게 된다.
    """
    bundle = build_source_bundle(files)
    if not bundle:
        return "", 0
    tokens = count_input_tokens(bundle)
    if tokens is None:
        tokens = estimate_tokens(bundle)
        logger.info("토큰을 세지 못해 문자 수로 추정했습니다: %d 토큰", tokens)
    return bundle, tokens


def _store_sources(snapshot_id: int, files: dict[str, str]) -> None:
    """수집한 소스 원문을 스냅샷에 보관한다. 실패해도 색인은 계속한다.

    **전체 주입 여부와 무관하게 부른다.** 원문이 남는 곳이 전체 주입 번들뿐이면
    소스가 이미 프롬프트에 다 들어간 저장소에만 원문이 있고 정작 큰 저장소에는 없다.
    청크로는 복원할 수 없어서(db/sources.py 참고) 큰 저장소는 원문을 영영 못 준다.

    **크기 상한을 넘으면 한 행도 넣지 않는다. 잘라서 넣지 않는다.** 일부만 보관하면
    읽는 쪽이 "저장소에 없다"와 "잘려서 없다"를 구분할 수 없어져, 없다는 답이 거짓이 된다.

    실패를 삼키는 이유는 _prune·_report 와 같다 — 부가 기능 때문에 색인이 실패하면 안 된다.
    """
    if not files:
        return

    total = sum(len(c.encode("utf-8")) for c in files.values())
    if MAX_STORED_SOURCE_BYTES and total > MAX_STORED_SOURCE_BYTES:
        logger.warning(
            "소스가 보관 상한을 넘어 이 스냅샷(%s)에는 원문을 남기지 않습니다"
            " (%d > %d 바이트). 일부만 보관하지 않습니다 — 잘린 원문은 '없다'는 답을"
            " 거짓으로 만듭니다.",
            snapshot_id, total, MAX_STORED_SOURCE_BYTES,
        )
        return

    try:
        saved = source_store.put_files(snapshot_id, files)
    except DB_ERRORS as e:
        logger.warning("소스 원문을 보관하지 못했습니다 (스냅샷 %s): %s", snapshot_id, e)
        return
    logger.info("소스 원문 %d개 파일 보관 (스냅샷 %s, %d바이트).", saved, snapshot_id, total)


def _try_full_injection(
    build_id: int, snapshot_id: int, owner: str, repo: str, files: dict[str, str]
) -> bool:
    """작은 저장소면 검색을 만들지 않고 소스를 통째로 저장한다. 우회했으면 True.

    **판정은 크기 속성으로만 한다** (docs/architecture.md §3). 사전 게이트로 바이트를 먼저 보는 이유는
    실측에서 바이트 추정이 저장소에 따라 24~32% 어긋났기 때문이다 — 바이트는 명백히 큰
    저장소를 싸게 걸러낼 뿐이고, **통과 판정은 실제 토큰 수로 한다.**

    빌드는 청크 0개로 완료 처리한다. 그러면 is_ready() 가 True 가 되어 대화가 "색인 중"
    배너 없이 바로 답하고, 검색은 빈 목록을 돌려준다 — 근거는 스냅샷의 번들에서 온다.
    """
    if not FULL_INJECTION_MAX_TOKENS or not files:
        return False

    source_bytes = sum(len(c.encode("utf-8")) for c in files.values())
    if FULL_INJECTION_MAX_SOURCE_BYTES and source_bytes > FULL_INJECTION_MAX_SOURCE_BYTES:
        return False

    bundle, tokens = measure_bundle(files)
    if not tokens or tokens > FULL_INJECTION_MAX_TOKENS:
        logger.info(
            "%s/%s 는 전체 주입 임계값을 넘어 검색 색인을 만듭니다 (%d > %d 토큰).",
            owner, repo, tokens, FULL_INJECTION_MAX_TOKENS,
        )
        return False

    repo_store.set_source_bundle(snapshot_id, bundle, tokens)
    index_status.complete(build_id, 0)
    logger.info(
        "%s/%s 는 전체 주입으로 처리합니다 — 파일 %d개 %d 토큰 (임계값 %d). 임베딩 없음.",
        owner, repo, len(files), tokens, FULL_INJECTION_MAX_TOKENS,
    )
    return True


def _skip_over_cap(build_id: int, owner: str, repo: str, chunks: int) -> bool:
    """청크가 상한을 넘으면 건너뛰기로 표시하고 True. 상한이 0이면 항상 False.

    **거절이 아니다.** 요약과 파일 목록으로는 그대로 답한다 — 없어지는 것은 코드 검색과
    도구뿐이다. 그 상태는 이미 있던 것이라(색인이 안 끝난 스냅샷) 새로 만들 동작이 없다.

    **`failed` 로 쓰지 않는다.** 실패가 아니라 안 하기로 정한 것이고, 무엇보다
    재시도 여부가 다르다 — `start()` 가 이 상태를 보고 다시 큐에 넣지 않는다.
    """
    if not MAX_INDEX_CHUNKS or chunks <= MAX_INDEX_CHUNKS:
        return False

    reason = f"코드 조각 {chunks:,}개가 상한 {MAX_INDEX_CHUNKS:,}개를 넘습니다"
    logger.warning(
        "%s/%s 는 색인을 만들지 않습니다 — %s. 요약만으로 답합니다.", owner, repo, reason
    )
    try:
        index_status.skip(build_id, chunks, reason)
    except DB_ERRORS as e:
        # 표시를 못 남기면 다음 질문에 다시 시도하게 된다. 낭비지만 틀리지는 않는다.
        logger.warning("색인을 건너뛴 것을 기록하지 못했습니다 (빌드 %s): %s", build_id, e)
    return True


def _warn_if_near_cap(owner: str, repo: str, chunks: int) -> None:
    """상한에 가까워지면 로그로 알린다. 색인은 그대로 진행한다.

    왜 이 감시선이 필요한지는 `CHUNK_CAP_WARN_RATIO` 주석에 있다.
    """
    if not MAX_INDEX_CHUNKS:
        return
    if chunks < MAX_INDEX_CHUNKS * CHUNK_CAP_WARN_RATIO:
        return
    logger.warning(
        "%s/%s 가 청크 상한에 가깝습니다 — %d / %d (%.0f%%)."
        " 넘으면 코드 검색과 도구가 붙지 않습니다.",
        owner, repo, chunks, MAX_INDEX_CHUNKS, chunks / MAX_INDEX_CHUNKS * 100,
    )


def _prune(snapshot_id: int, table: str | None) -> None:
    """보관 개수를 넘은 옛 빌드를 지운다. 실패해도 인덱싱은 성공으로 둔다."""
    try:
        removed = index_status.prune_builds(snapshot_id, table=table)
        if removed:
            logger.info("옛 색인 빌드 %d개를 정리했습니다 (스냅샷 %s).", removed, snapshot_id)
    except DB_ERRORS as e:
        logger.warning("옛 색인 빌드를 정리하지 못했습니다: %s", e)


def _warn_if_truncated(texts: list[str]) -> None:
    """입력 한도에 걸려 뒷부분이 임베딩되지 않은 청크가 있으면 경고를 남긴다.

    잘림은 조용히 일어난다 — 토크나이저가 한도에서 끊고 아무 말도 하지 않아서, 그 청크의
    뒷부분은 검색에서 사라지는데 아무도 모른다. `MAX_CHUNK_CHARS` 는 실측 비율로 정한
    값이라 한국어 비중이 아주 높은 코드에서는 여전히 넘을 수 있으므로 남겨 둔다.

    **초과(>)가 아니라 도달(>=)로 센다** — 토크나이저가 이미 한도에서 잘라 돌려주므로
    초과로 세면 영원히 0이다.
    """
    limit = input_limit()
    counts = count_tokens(texts)
    if not limit or counts is None:
        return
    hit = [n for n in counts if n >= limit]
    if hit:
        logger.warning(
            "청크 %d/%d 개가 입력 한도(%d 토큰)에 닿아 뒷부분이 임베딩되지 않았습니다"
            " — MAX_CHUNK_CHARS 를 더 낮춰야 할 수 있습니다.",
            len(hit), len(counts), limit,
        )


def _report(build_id: int, done: int) -> None:
    """진행 상황 기록. 실패해도 인덱싱은 계속한다 — 진행률은 부가 정보다."""
    try:
        index_status.advance(build_id, done)
    except DB_ERRORS as e:
        logger.debug("진행 상황을 기록하지 못했습니다: %s", e)


def _mark_failed(build_id: int, error: str) -> None:
    try:
        index_status.fail(build_id, error)
    except DB_ERRORS as e:
        # DB 가 죽어 실패조차 기록 못 하면 running 으로 남는다. 다음 기동의
        # reset_running() 이 pending 으로 되돌려 재시도할 수 있게 한다.
        logger.warning("인덱싱 실패를 기록하지 못했습니다: %s", e)


def is_ready(snapshot_id: int, table: str | None = None) -> bool:
    """이 스냅샷의 코드를 검색해도 되는지.

    **완료했을 때만 True.** 진행 중인 인덱스로 검색하면 아직 임베딩하지 않은 코드가
    '저장소에 없는 코드'처럼 보여 틀린 답을 만든다.
    """
    try:
        return index_status.is_completed(snapshot_id, table=table)
    except DB_ERRORS as e:
        logger.warning("인덱싱 여부를 확인하지 못했습니다: %s", e)
        return False


# 답의 근거가 되기 어려운 청크에 거리를 더해 뒤로 민다. **제외가 아니라 감점이다** —
# 스타일시트가 정답인 질문("이 버튼 스타일 어디서 정의해?")도 있고, 클래스 선언은
# "이 클래스가 무엇을 상속하는가" 같은 질의의 유일한 정답이다.
#
# 값은 실측으로 정했다(평가셋 17질의, 코사인 거리 기준). 0.03 부터 한국어 Recall@8 이
# 0.50 → 0.67 로 회복되고, 0.05 이상은 결과가 더 변하지 않는다 — 저정보 청크가 완전히
# 뒤로 밀려 사실상 '제외'가 되는 구간이라 그 앞에서 멈춘다.
# (측정: docs/evaluation.md §4 청킹 수정 2c)
LOW_INFO_PENALTY = 0.03

# 감점 후보를 넉넉히 가져와서 다시 세운다. 상위 limit 개만 가져와 감점하면
# 감점 대상에 밀려 들어오지 못한 진짜 근거가 영영 보이지 않는다.
CANDIDATE_MULTIPLIER = 5

# 스타일시트. 한국어 주석("/* 페이지네이션 링크 스타일 */")이 많아 한국어 질의와
# 곧잘 맞는데, 정작 "어떻게 동작하는가"의 답은 아니다 — 실측에서 ko_03/ko_05/ko_08 의
# 상위 절반을 CSS 가 차지했다.
STYLE_LANGUAGES = frozenset({"css", "scss"})

# 프레임워크 진입점. 어느 저장소에나 있고 내용이 비슷해 아무 질문에나 어중간하게 걸린다.
ENTRY_POINT_MARKERS = (
    "@SpringBootApplication",
    "SpringApplication.run",
    "static void main(",
    'if __name__ == "__main__"',
    "if __name__ == '__main__'",
)

# 상속·구현 관계는 선언 한 줄뿐이어도 그 자체가 답이다 (id_04 가 그 경우다).
INHERITANCE_MARKERS = (" extends ", " implements ", "(BaseModel)", "(ABC)")

_COMMENT_OR_ANNOTATION = ("//", "/*", "*", "#", "--", "<!--", "@")


def _is_declaration_only(content: str) -> bool:
    """선언만 있고 실행되는 코드가 없는 블록인가 (필드만 나열된 DTO 헤더 등)."""
    body = [
        line.strip() for line in content.splitlines()
        if line.strip()
        and not line.strip().startswith(_COMMENT_OR_ANNOTATION)
        and line.strip() not in ("{", "}", "};")
    ]
    if not body:
        return True
    return all("(" not in line for line in body)


def is_low_info(chunk: dict) -> bool:
    """답의 근거가 되기 어려운 청크인가. 감점 대상을 고르는 데만 쓴다."""
    content = chunk["content"]
    if chunk.get("language") in STYLE_LANGUAGES:
        return True
    if any(marker in content for marker in ENTRY_POINT_MARKERS):
        return True
    if any(marker in content for marker in INHERITANCE_MARKERS):
        return False
    return _is_declaration_only(content)


def search_code(
    snapshot_id: int, question: str, limit: int = TOP_K, table: str | None = None
) -> list[dict]:
    """질문과 가까운 코드 청크. 실패하면 빈 목록 — 검색이 안 돼도 답변은 진행한다.

    질문은 원문 그대로 임베딩한다 (모델이 요구하는 접두어만 embed_query 가 붙인다).
    가져온 뒤 저정보 청크에 LOW_INFO_PENALTY 를 더해 순서를 다시 세운다.
    """
    try:
        # 활성 빌드 안에서만 찾는다 — 재색인 중이면 새 빌드의 절반짜리 청크가 함께
        # 들어 있는데, 그것이 섞이면 아직 임베딩하지 않은 코드가 '없는 코드'처럼 보인다.
        build_id = index_status.active_build_id(snapshot_id, table=table)
        if build_id is None:
            return []
        rows = chunk_store.search(
            build_id, embed_query(question), limit * CANDIDATE_MULTIPLIER, table=table
        )
    except Exception as e:
        logger.warning("코드 검색 실패: %s", e)
        return []

    for row in rows:
        row["distance"] = float(row["distance"]) + (
            LOW_INFO_PENALTY if is_low_info(row) else 0.0
        )
    rows.sort(key=lambda row: row["distance"])
    return rows[:limit]


def format_snippets(found: list[dict]) -> str:
    """검색 결과를 프롬프트에 넣을 문자열로.

    경로와 **행 범위**를 헤더에 붙인다. 본문에는 줄 번호를 붙이지 않는다.

    붙여 봤다가 되돌렸다(2026-08-19). 전체 주입 번들처럼 `12|코드` 로 매기려면
    청크 content 가 원본의 `start_line~end_line` 과 1:1 이어야 하는데 **그렇지 않다** —
    `_merge_small` 이 떨어진 조각 둘을 이어붙이면서 사이 줄이 빠지기 때문에,
    실측에서 청크의 **48.8%가 줄 수부터 어긋났다**(10줄 범위인데 content 는 11줄).
    그 상태로 번호를 매기면 절반이 틀린 번호가 되고, 모델이 세는 것보다 나빠진다
    (틀린 번호를 그대로 믿는다).

    청크를 "원본의 연속된 줄"로 재정의하면 붙일 수 있다. 그때는 청크 경계가 바뀌므로
    재색인과 재평가가 따라온다 — docs/evaluation.md §7 참고.
    """
    if not found:
        return ""
    blocks = ["## 질문과 관련된 코드"]
    for item in found:
        blocks.append(
            f"\n### {item['path']} ({item['start_line']}-{item['end_line']}행)\n"
            f"```{item['language']}\n{item['content']}\n```"
        )
    return "\n".join(blocks)
