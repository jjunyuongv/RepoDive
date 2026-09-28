"""답변 품질 — 인용 정확도와 거절 정확도를 **같은 자로** 잰다.

**과금된다.** 질의마다 Claude 를 실제로 부른다. 두 마커를 **다** 명시했을 때만 돈다:

    pytest -m "billed and evaluation" tests/test_citation_quality.py -s

`-m billed` 만 쓰면 **조용히 3건 skip 된다.** conftest 의 opt-in 게이트는 마커마다
따로 물어서, 이 모듈이 단 `evaluation` 마커가 `-m` 에 없으면 그것만으로 건너뛴다.
0.9초에 `sss` 로 끝나면 측정이 아니라 아무것도 안 돈 것이다 — 로그 파일도 안 생긴다.

## 왜 순위 지표를 안 쓰는가

Recall@8·MRR 은 검색 순위 지표인데 전체 주입에는 검색이 없다. 형식상 Recall@∞ = 1.0 이
되어 자동으로 만점이다 — 그 숫자를 "전체 주입이 이겼다"로 읽으면 안 된다.

대신 **답변이 정답 파일을 실제로 짚었는지**를 센다. 평가셋의 `answers[].path_suffix` 를
답변 텍스트에서 찾는 문자열 매칭이라 **LLM 심판이 필요 없고 결정적이다.**
`CHAT_SYSTEM_PROMPT` 가 "어느 파일 몇 행인지 밝히라"고 이미 강제하고 있어 형식도 맞는다.

## 판정 기준 (측정 전에 고정한다)

전체 주입이 **떨어지면 우회를 접는다.** 동률이면 RAG 유지(비용·지연이 낮다).
**올라야만** 우회를 켠다. 이 기준을 결과를 보고 정하면 측정이 아니라 사후 합리화가 된다.
"큰 덩어리에 섞이면 초점이 흐려진다"는 실패 방식이 이 저장소에 이미 기록돼 있어
(docs/evaluation.md §4, `py_ko_01` 1→10위) 전체 주입이 지는 결과는 실제로 가능하다.

## 답변 품질 판정 기준 — tool use 도입용 (측정 전에 고정한다)

위 기준은 "전체 주입 우회를 켤 것인가"의 자다. 아래는 **tool use 를 붙일 것인가**의 자다.

    A. 인용 정확도가 기준선보다 떨어지면 기각.
    B. A 가 동률이면 거절 정확도를 본다. 떨어지면 기각.
    C. A·거절이 올라도 질문당 비용이 기준선의 COST_RATIO_LIMIT 배를 넘으면
       전면 도입하지 않고 "큰 저장소 전용"으로만 켠다.

### 왜 거절 축이 따로 필요한가

검색을 반복하는 경로는 계속 뒤지다 지어낼 여지가, 한 번 검색하고 끝내는 경로보다 크다.
그런데 인용 정확도는 **정답이 있는 질의**에서만 재므로 그 실패를 못 잡는다 —
정답이 없는 질의에서는 짚을 파일이 없어 **날조와 침묵이 똑같이 0점**으로 보인다.
정답이 없는 질의(`ABSENT_SETS`)를 따로 두고 "규정대로 거절했는가"를 세야 차이가 드러난다.

### 이 자의 한계 — 알고 쓴다

문구 매칭이라 "KeyStoreGetter 는 읽어 오기만 하고 만료 검사는 없습니다"처럼 **내용은
맞는데 규정 문구를 안 쓴 답변**은 실패로 센다. 재는 것은 정확히 "규정대로 거절했는가"이고,
"지어내지 않았는가"의 대리 지표다. 두 경로에 같은 자를 대므로 **비교**에는 쓸 수 있지만,
절대값을 "이만큼 지어낸다"로 읽으면 안 된다.
"""

import json
import re
import time
from pathlib import Path

import pytest

from app import config
from app.core import embeddings
from app.core.chunk_rule import rule_version
from app.core.chunker import chunk_files
from app.db import chunks as chunk_store
from app.db import index_status, pool
from app.db import sources as source_store
from app.services import claude_client, tools
from app.services.indexer import format_snippets, measure_bundle, search_code
from tests.search_eval_dataset import (
    ABSENT_SET_VERSION,
    ABSENT_SETS,
    EVAL_SET_VERSION,
    EVAL_SETS,
)
from tests.test_search_quality import _snapshot_for, _source_files

pytestmark = [pytest.mark.evaluation, pytest.mark.billed]

LOG_PATH = Path(__file__).resolve().parents[1] / "logs" / "citation_evals.jsonl"

# 질문당 비용이 기준선의 몇 배를 넘으면 전면 도입을 접는가 (위 판정 기준 C).
# 3배는 "큰 저장소에서만 켜면 되는" 구간의 하한으로 잡은 값이다 — 임의의 저장소를
# 받는 서비스라 평균 질문 하나의 값이 세 배가 되면 우회 판정과 같은 층위의 결정이 된다.
COST_RATIO_LIMIT = 3.0

# 대화의 두 번째 메시지로 넣을 요약. 실제 서비스에서는 LLM 이 만든 요약이 여기 온다.
# 세트마다 고정 문장을 쓴다 — 매번 요약을 새로 만들면 그 편차가 두 경로의 차이에 섞인다.
# (평가셋이 아니라 **대화 하네스의 입력**이라 search_eval_dataset 에 두지 않는다)
SET_SUMMARIES = {
    "air": "사내 그룹웨어입니다. 전자결재·일정·채팅·보고서를 다루는 Spring Boot 애플리케이션입니다.",
    "marryday": "웨딩드레스 가상 피팅 서비스입니다. FastAPI 백엔드와 React 프론트로 이루어져 있습니다.",
    "apns4j": "APNs 푸시 알림을 보내는 Java 라이브러리입니다.",
}

ARM_LABELS = {"rag": "RAG", "full": "전체주입", "tool": "도구"}

# 대화 이력 없이 질의 하나씩 독립으로 묻는다 — 앞 질문의 답이 다음 답에 섞이면
# 두 경로의 차이가 아니라 대화 순서의 차이를 재게 된다.
NO_HISTORY: list[dict] = []


def _cited(answer: str, case: dict) -> bool:
    """답변이 정답 파일을 짚었는가. answers 중 하나라도 맞으면 정답이다.

    **정답이 없는 질의(kind="absent")를 넣으면 언제나 False 다.** answers 가 없어
    any([]) 가 되기 때문이다. 그래서 거절 질의를 기존 목록에 섞으면 안 된다 —
    섞으면 인용 정확도가 조용히 희석되고, 그 하락이 개선 실패로 오독된다.
    """
    return any(a["path_suffix"] in answer for a in case.get("answers", []))


# ── 거절 채점기 ──────────────────────────────────────────────
#
# **문장 구조에서 유도했다. 정규식을 늘려 15건을 맞춘 것이 아니다.**
# 거절 문장은 예외 없이 "[어디를 봤는가] + [거기에 없다]" 두 조각으로 이루어진다.
# 그래서 두 조각을 따로 정의하고, 한 문장 안에서 이 순서로 나타나는지만 본다.
#
# 옛 채점기는 규정 문구 두 개를 **연속 문자열**로 찾았는데, 모델은 거의 항상
# "검색된 `코드` 범위에는 … 없습니다" 처럼 낱말을 끼워 넣어서 24건 중 15건이
# 새어 나갔다(2026-08-21 기준선). 날조는 0건이었다 — 자의 문제였다.
#
# CHAT_SYSTEM_PROMPT 는 건드리지 않는다. 프롬프트를 고치면 재는 대상이 바뀌어
# 인용 정확도까지 흔들리고 89회를 다시 돌려야 한다. 여기는 채점기만 고친다.

# (가) 어디를 봤는가. 답변이 근거의 범위를 가리키는 표현.
# 범위를 가리키는 명사. 아래 두 패턴이 이것을 **양쪽 어순으로** 감싼다.
_SCOPE_NOUN = r"(?:소스|코드|저장소|파일|색인|범위)"

REFUSAL_SCOPES = (
    # 검색된 범위 / 검색 결과 / 검색 범위 — 어간 하나에 어미만 다르다.
    r"검색(?:된|\s*결과|\s*범위)",
    # 도구 이름으로 범위를 말하는 경우("grep 결과", "grep 으로 찾아봤지만").
    r"grep",
    r"(?:주어진|제공된)\s*(?:정보|코드|저장소|소스)",
    # **어순 두 방향을 다 받는다.** 한쪽만 두면 같은 뜻이 어순 때문에 샌다 —
    # 실측에서 "소스 전체"·"저장소 전체" 3건이 정확히 그렇게 샜다.
    rf"전체\s*{_SCOPE_NOUN}",
    rf"{_SCOPE_NOUN}\s*전체",
    # 도구를 직접 부르는 경로가 **자기가 한 행동**으로 범위를 말하는 경우.
    # (이번 실측 67건에서는 한 건도 안 걸렸다 — 모델은 도구를 써도 "검색된 범위"라
    #  말했다. 남겨 두는 것은 답변 양식이 바뀔 여지 때문이다)
    r"(?:읽어|열어|확인해|찾아|살펴)\s*본",
)

# (나) 거기에 없다. 부정 술어.
# **세 구조뿐이다.** 전에는 표현을 하나씩 나열해서, 같은 구조의 다른 낱말이 나오면
# 그때마다 샜다(실측에서 "발견되지 않"·"검색되지 않"·"없고"·"없으며" 10건이 그렇게 샜다).
# 구조로 묶으면 낱말이 바뀌어도 걸린다.
REFUSAL_NEGATIONS = (
    # (가) `없-` + 어미. **종결형만 받으면 안 된다** — 문장이 이어지면 연결형으로 온다
    #      ("저장소에 전혀 없고, …", "저장소에 없으며, …").
    r"없(?:습니다|다|음|어요|고|으며|며)",
    # (나) [탐색 동사] + `지 않`. 동사만 갈아끼운 같은 구조다.
    r"(?:보이|존재하|확인되|나타나|발견되|검색되|포함되어\s*있)지\s*않",
    # (다) 가능성 부정.
    r"(?:찾을|알)\s*수\s*없",
)

# (다) 양보 — "X 는 있지만 Y 는 없다". 이건 거절 선언이 아니라 **일부는 답한** 문장이다.
# 근거를 대고 나서 못 찾은 부분을 밝히는 것은 프롬프트가 시킨 대로 답한 것이다.
# 이걸 거절로 세면 "지어낸 답 + 면피 한 줄" 이 그대로 통과한다.
#
# **모호한 문장은 거절로 세지 않는다.** 거절을 적게 세는 쪽으로 틀리면 tool use 가
# 실제보다 정직해 보이는 일은 없다 — 안전한 방향이다.
REFUSAL_CONCESSIONS = (
    r"(?:보이|있|존재하|확인되|나오|나타나)(?:지만|으나|지마는)",
)

_REFUSAL_RE = re.compile(
    f"(?:{'|'.join(REFUSAL_SCOPES)}).*?(?:{'|'.join(REFUSAL_NEGATIONS)})"
)
_CONCESSION_RE = re.compile("|".join(REFUSAL_CONCESSIONS))

_SENTENCE_SPLIT = re.compile(r"(?<=[.!?])\s+|\n+")


def _first_sentence(answer: str) -> str:
    return _SENTENCE_SPLIT.split(answer.strip(), maxsplit=1)[0] if answer.strip() else ""


def _refused(answer: str) -> bool:
    """답변이 **거절로 시작하는가.**

    **첫 문장만 본다.** 이것이 이 채점기에서 가장 중요한 조임쇠다.
    `CHAT_SYSTEM_PROMPT` 가 "근거가 없으면 … 한 문장으로 밝히고" 를 요구하므로,
    진짜 거절은 답변을 열면서 나온다. 실측에서도 거절 질의 24건 전부가 첫 문장에서
    거절했다.

    본문 어디든 보면 **근거를 대고 나서 덧붙인 단서**까지 거절로 잡힌다 —
    "ApnsPayload 가 Payload 를 상속합니다. 검색된 범위 안에서 상속하는 다른 클래스는
    확인되지 않습니다" 같은 답변은 거절이 아니라 정확한 답이다. 그걸 거절로 세면
    "지어낸 답 + 면피 한 줄" 이 거절로 통과해 축이 무의미해진다.

    두 팔 모두 세 계열을 다 인정한다. 전체 주입에서 "검색된 범위" 표현은 프롬프트
    위반이지만 그건 **형식 위반이지 날조가 아니다** — 이 축이 재려는 것은 지어냈는가다.
    """
    opening = _first_sentence(answer)
    if _CONCESSION_RE.search(opening):
        return False
    return bool(_REFUSAL_RE.search(opening))


def _score(answer: str, case: dict) -> bool:
    """이 답변이 이 질의에서 옳은가. 질의 종류에 따라 다른 자를 댄다."""
    if case["kind"] == "absent":
        return _refused(answer)
    return _cited(answer, case)


def _ensure_sources(snapshot_id: int, files: dict) -> None:
    """`read_file`·`grep` 이 읽을 원문. 없으면 로컬 캐시에서 채운다. **무과금이다.**

    **이걸 빼면 도구 팔이 `search_code` 하나짜리로 측정된다.** 평가용 스냅샷은 소스
    보관 기능보다 먼저 색인됐고 재색인은 수동이라, 그냥 두면 `read_file`·`grep` 이
    전부 "보관된 소스가 없습니다"를 돌려준다 — 프로덕션의 옛 스냅샷과 같은 상태다.
    그 상태를 재는 것은 이 측정의 목적이 아니다(그건 재색인으로 해소되는 과도기다).

    색인과 같은 파일 집합을 쓴다(`_source_files` 의 로컬 캐시) — 여기서 다시 받으면
    측정마다 입력이 달라질 수 있다.
    """
    if source_store.count(snapshot_id):
        return
    saved = source_store.put_files(snapshot_id, files)
    print(f"  (측정 준비) 소스 원문 {saved}개 파일을 스냅샷 {snapshot_id} 에 보관했다 — 무과금")


def _ensure_index(snapshot_id: int, repo: tuple[str, str], files: dict) -> None:
    """RAG 경로가 쓸 검색 색인. 이미 완료돼 있으면 그대로 쓴다."""
    if index_status.active_build_id(snapshot_id, table=config.CHUNK_TABLE):
        return
    pieces = chunk_files(
        files, count_tokens=embeddings.count_tokens, token_limit=embeddings.input_limit()
    )
    vectors = embeddings.embed_documents([p["content"] for p in pieces])
    build_id = index_status.begin(
        snapshot_id, table=config.CHUNK_TABLE, chunk_rule=rule_version()
    )
    if build_id is None:
        index_status.reset_running()
        build_id = index_status.begin(
            snapshot_id, table=config.CHUNK_TABLE, chunk_rule=rule_version()
        )
    saved = chunk_store.insert_chunks(
        build_id, snapshot_id, pieces, vectors, table=config.CHUNK_TABLE
    )
    index_status.complete(build_id, saved)
    index_status.prune_builds(snapshot_id, table=config.CHUNK_TABLE)


def _ask(
    context: str, summary: str, case: dict, *,
    snippets: str, bundle: str, tools=None, execute=None,
) -> dict:
    result = claude_client.run_chat(
        context=context,
        summary=summary,
        history=NO_HISTORY,
        question=case["query"],
        snippets=snippets,
        source_bundle=bundle,
        tools=tools,
        execute=execute,
    )
    return {
        "id": case["id"],
        "kind": case["kind"],
        "ok": _score(result["text"], case),
        "answer": result["text"],
        # 지연은 판정 기준 B·C 와 함께 보는 축이다. tool use 를 붙이면 검색을 여러 번
        # 도느라 여기가 가장 크게 움직인다 — 버리면 그 변화를 사후에 못 잰다.
        "llm_ms": result["llm_ms"],
        "input_tokens": result["input_tokens"],
        "output_tokens": result["output_tokens"],
        "cache_write_tokens": result["cache_write_tokens"],
        "cache_read_tokens": result["cache_read_tokens"],
        "cost_usd": result["cost_usd"] or 0.0,
        # 비용 모델이 이 값 위에 서 있다. 도구를 안 쓴 팔은 0 이다.
        "round_trips": result.get("round_trips", 0),
        "hit_round_trip_cap": result.get("hit_round_trip_cap", False),
        # **호출별 사용량과 도구 호출 내역.** 0단계가 답변 원문을 남겨 둔 덕에 채점기를
        # 고치고 $0 으로 재채점할 수 있었다. 이번 측정에서 대체하려는 것은 비용 산식의
        # 가정(a=150 · r=1,500)이라, 원문만으로는 부족하고 이 둘이 있어야 한다.
        # tool_result 원문은 싣지 않는다 — 크기와 절단 여부면 산식에 충분하고,
        # 본문은 보관 소스에 이미 있다.
        "calls": result.get("calls", []),
        "tool_trace": result.get("tool_trace", []),
        # 어느 캡이 걸렸는가. 셋을 한 자리에 모아 스캔할 수 있게 한다.
        "caps_hit": sorted(
            {cap for entry in result.get("tool_trace", []) for cap in entry["caps"]}
            | ({"round_trips"} if result.get("hit_round_trip_cap") else set())
        ),
    }


def _rate(rows: list[dict]) -> float:
    return round(sum(r["ok"] for r in rows) / len(rows), 4) if rows else 0.0


def _mean(values: list[int]) -> float:
    """빈 목록이면 0.0 — 도구를 안 쓴 팔이 여기로 온다."""
    return round(sum(values) / len(values), 1) if values else 0.0


def _summarize(rows: list[dict]) -> dict:
    """팔 하나의 집계.

    **인용 정확도의 모수에서 absent 행을 뺀다.** 안 빼면 지난 기록(set_version=2,
    16질의)과 같은 칸에서 비교가 끊긴다 — 거절 질의를 더한 것이 인용 정확도를 떨어뜨린
    것처럼 보인다. `EVAL_SET_VERSION` 을 2 로 둔 이유가 이것이다.
    """
    answerable = [r for r in rows if r["kind"] != "absent"]
    absent = [r for r in rows if r["kind"] == "absent"]
    return {
        "citation_accuracy": _rate(answerable),
        "korean": _rate([r for r in rows if r["kind"] == "korean"]),
        "identifier": _rate([r for r in rows if r["kind"] == "identifier"]),
        "refusal_accuracy": _rate(absent),
        "cost_usd": round(sum(r["cost_usd"] for r in rows), 4),
        "avg_cost_usd": round(sum(r["cost_usd"] for r in rows) / len(rows), 6),
        "avg_llm_ms": round(sum(r["llm_ms"] for r in rows) / len(rows)),
        "cache_write_tokens": sum(r["cache_write_tokens"] for r in rows),
        "cache_read_tokens": sum(r["cache_read_tokens"] for r in rows),
        # 라운드트립 분포가 비용 모델의 입력이다. 평균만으로는 캡에 닿은 질의가
        # 몇 건인지 안 보여서 함께 센다.
        "avg_round_trips": round(sum(r["round_trips"] for r in rows) / len(rows), 2),
        "capped": sum(1 for r in rows if r["hit_round_trip_cap"]),
        # **산식의 a·r 실측치.** 07 조사가 잡은 가정은 a=150 · r=1,500 이고,
        # 3.78×/2.11× 가 거기서 나왔다 — 순수 tool use 로 간 근거다.
        # r 은 **자르기 전** 크기다. 자른 뒤 값과 대조하면 상한(800)에 눌린 수치를 보게
        # 되어 가정을 검증할 수 없다.
        "avg_tool_result_tokens": _mean(
            [e["result_tokens"] for r in rows for e in r["tool_trace"]]
        ),
        "avg_tool_call_output_tokens": _mean([
            call["output_tokens"]
            for r in rows
            for call in r["calls"][:-1]  # 마지막 호출은 답변이라 a_final 이다
        ]),
        "caps_hit": sorted({cap for r in rows for cap in r["caps_hit"]}),
    }


def _arms_for(tokens: int) -> tuple[str, ...]:
    """이 저장소에서 성립하는 팔.

    **저장소 이름으로 가르지 않는다**(docs/architecture.md §3). 프로덕션이 우회를 판정할 때 쓰는
    바로 그 기준(번들 토큰 수 ≤ 임계값)을 그대로 쓴다 — 하네스와 서비스의 기준이
    갈리면 "여기서는 성립하는데 서비스에서는 안 켜지는" 비교를 하게 된다.
    임계값을 넘는 저장소에서 전체 주입은 아예 성립하지 않으므로 그 팔만 빠진다.

    `tool` 은 어디서나 성립한다 — 검색 색인만 있으면 된다. `rag` 는 그대로 두어야
    한다: 프롬프트가 바뀌어 옛 기준선이 무효가 됐으므로 **같은 실행에서 다시 재야**
    tool 팔과 비교가 성립한다.
    """
    if tokens and tokens <= config.FULL_INJECTION_MAX_TOKENS:
        return ("rag", "full", "tool")
    return ("rag", "tool")


@pytest.mark.parametrize("set_name", list(EVAL_SETS))
def test_measure_answer_quality(set_name, capsys):
    """세트 하나를 성립하는 모든 팔로 돌려 인용·거절 정확도를 재고 기록한다.

    단정하지 않는다 — 측정이다. 판정은 위 docstring 의 기준으로 사람이 한다.
    """
    if not pool.is_enabled():
        pytest.skip("DATABASE_URL 이 없어 답변 품질 측정을 건너뜁니다.")
    if not config.ANTHROPIC_API_KEY:
        pytest.skip("ANTHROPIC_API_KEY 가 없어 답변 품질 측정을 건너뜁니다.")

    spec = EVAL_SETS[set_name]
    repo = spec["repo"]
    cases = [*spec["queries"], *ABSENT_SETS[set_name]]
    files = _source_files(repo)
    snapshot_id = _snapshot_for(repo)
    _ensure_index(snapshot_id, repo, files)
    _ensure_sources(snapshot_id, files)

    # 무과금이다 — 토큰 계산 엔드포인트는 추론을 돌리지 않는다.
    bundle, bundle_tokens = measure_bundle(files)
    arms = _arms_for(bundle_tokens)
    context = f"## 레포지토리 정보\n- 이름: {repo[0]}/{repo[1]}"
    summary = SET_SUMMARIES[set_name]

    started = time.perf_counter()
    results: dict[str, list[dict]] = {}
    for arm in arms:
        rows = []
        for case in cases:
            snippets = ""
            if arm == "rag":
                snippets = format_snippets(
                    search_code(snapshot_id, case["query"], table=config.CHUNK_TABLE)
                )
            rows.append(
                _ask(
                    context, summary, case,
                    snippets=snippets,
                    bundle=bundle if arm == "full" else "",
                    tools=tools.TOOL_SCHEMAS if arm == "tool" else None,
                    # 프로덕션과 **같은 실행기**를 쓴다. 하네스가 자기 대역을 쓰면
                    # 서비스가 아니라 하네스를 재게 된다.
                    execute=tools.build_executor(snapshot_id) if arm == "tool" else None,
                )
            )
        results[arm] = rows

    totals = {arm: _summarize(rows) for arm, rows in results.items()}

    with capsys.disabled():
        _print_report(set_name, repo, cases, arms, results, totals,
                      bundle, bundle_tokens, time.perf_counter() - started)

    LOG_PATH.parent.mkdir(parents=True, exist_ok=True)
    record = {
        "ts": time.strftime("%Y-%m-%dT%H:%M:%S"),
        "repo": f"{repo[0]}/{repo[1]}",
        "set": set_name,
        "set_version": EVAL_SET_VERSION,
        "absent_set_version": ABSENT_SET_VERSION,
        "model": claude_client.DEFAULT_MODEL,
        "bundle_chars": len(bundle),
        "bundle_tokens": bundle_tokens,
        # 어느 팔이 돌았는지 기록에 남긴다. 옛 기록에는 이 키가 없으므로 읽는 쪽은
        # ("rag", "full") 로 폴백한다 (tests/test_line_accuracy.py).
        "arms": list(arms),
    }
    for arm in arms:
        record[arm] = {**totals[arm], "rows": results[arm]}
    with LOG_PATH.open("a", encoding="utf-8") as f:
        f.write(json.dumps(record, ensure_ascii=False) + "\n")

    # 단정하지 않는다 — 측정이다. 다만 하네스가 깨진 경우는 잡는다.
    assert all(results[arm] for arm in arms)


def _print_report(set_name, repo, cases, arms, results, totals,
                  bundle, bundle_tokens, elapsed) -> None:
    """팔 개수에 맞춰 표를 그린다. 팔이 하나면 비교 열도 판정 줄도 뜨지 않는다."""
    labels = [ARM_LABELS[a] for a in arms]
    print(f"\n{repo[0]}/{repo[1]} [{set_name}] — 질의 {len(cases)}개 "
          f"(거절 {sum(1 for c in cases if c['kind'] == 'absent')}개) · "
          f"팔 {'·'.join(labels)} · {int(elapsed)}초")
    print(f"번들 {len(bundle):,}자 / {bundle_tokens:,}토큰 "
          f"(전체 주입 임계값 {config.FULL_INJECTION_MAX_TOKENS:,})")

    print("\n" + f"{'지표':<16}" + "".join(f"{label:>12}" for label in labels))
    for label, key in (
        ("인용 정확도", "citation_accuracy"),
        ("  한국어", "korean"),
        ("  식별자", "identifier"),
        ("거절 정확도", "refusal_accuracy"),
        ("비용(USD)", "cost_usd"),
        ("질문당 비용", "avg_cost_usd"),
        ("질문당 지연(ms)", "avg_llm_ms"),
        ("질문당 도구호출", "avg_round_trips"),
        ("한도에 닿음", "capped"),
        ("도구결과 절단전(r)", "avg_tool_result_tokens"),
        ("도구턴 출력(a)", "avg_tool_call_output_tokens"),
    ):
        print(f"{label:<16}" + "".join(f"{totals[a][key]:>12}" for a in arms))

    # **가정과 상한은 다른 양이다.** 섞어 놓으면 대조가 성립하지 않는다.
    #   r=1,500 · a=150  캡이 생기기 전 조사가 잡은 가정. 3.78×/2.11× 가 여기서 나왔고
    #                    그것이 순수 tool use 로 간 근거다 → 이 값이 실측 대조 대상이다
    #   r=800            MAX_TOOL_RESULT_TOKENS. 천장이지 기대값이 아니다.
    #                    모든 결과가 여기 닿는 최악을 재서 R=3 을 정했다(2.51×)
    # 평균이 800 아래인 것은 당연하므로 아무것도 증명하지 않는다. 봐야 할 것은
    # **절단 전 크기가 1,500 근처인가**와 **절단이 얼마나 자주 걸렸는가**다.
    if "tool" in arms:
        tool = totals["tool"]
        print(
            f"\n  대조 — 07 조사의 가정 r=1,500 · a=150"
            f"  vs  실측 r={tool['avg_tool_result_tokens']} · a={tool['avg_tool_call_output_tokens']}"
        )
        print(
            f"  상한 r={tools.MAX_TOOL_RESULT_TOKENS}(MAX_TOOL_RESULT_TOKENS) 는 천장이다 —"
            f" 평균이 그 아래인 것은 대조가 아니다."
            f" 절단이 걸린 질의: {sum(1 for r in results['tool'] if 'result_tokens' in r['caps_hit'])}건"
        )
    for arm in arms:
        if totals[arm]["caps_hit"]:
            print(f"  {ARM_LABELS[arm]} 에서 걸린 캡: {', '.join(totals[arm]['caps_hit'])}")

    # 판정 기준 C 는 **기준선 대비 배수**로 정해져 있다. 눈으로 나누게 두면 틀린다.
    base = totals["rag"]["avg_cost_usd"]
    if base:
        ratios = "".join(f"{totals[a]['avg_cost_usd'] / base:>11.2f}배" for a in arms)
        print(f"{'RAG 대비 배수':<16}{ratios}   (기준 C: {COST_RATIO_LIMIT}배)")

    print("\n" + f"{'id':<10} {'종류':<11}" + "".join(f"{lb:>7}" for lb in labels) + "  질의")
    by_id = {arm: {r["id"]: r for r in rows} for arm, rows in results.items()}
    for case in cases:
        marks = "".join(
            f"{'O' if by_id[a][case['id']]['ok'] else 'X':>7}" for a in arms
        )
        note = ""
        if len(arms) > 1:
            scores = [by_id[a][case["id"]]["ok"] for a in arms]
            if len(set(scores)) > 1:
                won = [ARM_LABELS[a] for a, ok in zip(arms, scores) if ok]
                note = f"  ← {'·'.join(won)} 우세"
        print(f"{case['id']:<10} {case['kind']:<11}{marks}  {case['query']}{note}")

    # 판정은 사람이 한다. 여기서는 **기준을 적용한 결과만** 보여준다 —
    # 눈으로 표를 읽고 정하면 사후 합리화가 된다.
    rag = totals["rag"]
    print("\n판정 (기준은 측정 전에 고정돼 있다. A→B→C 순서로 본다):")
    for arm in arms:
        if arm == "rag":
            continue
        this = totals[arm]
        if this["citation_accuracy"] < rag["citation_accuracy"]:
            verdict = "기각 — 인용 정확도가 떨어졌다 (기준 A)"
        elif this["citation_accuracy"] == rag["citation_accuracy"]:
            verdict = (
                "기각 — 동률인데 거절 정확도도 떨어졌다 (기준 B)"
                if this["refusal_accuracy"] < rag["refusal_accuracy"]
                else "동률 — RAG 유지 (올라야만 켠다)"
            )
        elif rag["avg_cost_usd"] and (
            this["avg_cost_usd"] / rag["avg_cost_usd"] > COST_RATIO_LIMIT
        ):
            verdict = f"품질은 올랐으나 비용이 {COST_RATIO_LIMIT}배를 넘었다 — 전면 도입하지 않는다 (기준 C)"
        else:
            verdict = "켠다 — 인용이 오르고 비용이 상한 안이다"
        print(f"  {ARM_LABELS[arm]:<6} {verdict}")

    if "full" not in arms:
        print("  (이 저장소는 전체 주입 임계값을 넘어 그 팔이 성립하지 않는다)")
