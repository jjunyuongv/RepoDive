"""도구 정의와 실행기. DB·검색·토큰 계산을 전부 대역으로 바꿔 무과금·무네트워크로 돈다.

DB 쿼리 자체는 tests/test_db_sources.py 가 실제 PostgreSQL 로 확인한다.
"""

import psycopg
import pytest

from app.services import tools

SNAPSHOT = 7

FILES = {
    "app/main.py": "import os\ndef run():\n    return 1\n\n\ndef stop():\n    return 0\n",
}


class FakeSources:
    """db.sources 대역."""

    def __init__(self, files=None, hits=None):
        self.files = FILES if files is None else files
        self.hits = hits or []
        self.count_calls = 0

    def get_file(self, snapshot_id, path):
        return self.files.get(path)

    def grep(self, snapshot_id, pattern, limit):
        return self.hits[:limit]

    def count(self, snapshot_id):
        self.count_calls += 1
        return len(self.files)


class FakeIndexer:
    def __init__(self, found=None):
        self.found = found if found is not None else [{"path": "a.py"}]
        self.queries = []

    def search_code(self, snapshot_id, query):
        self.queries.append(query)
        return self.found

    def format_snippets(self, found):
        return "## 질문과 관련된 코드\n\n### a.py (1-3행)\n```python\nx = 1\n```" if found else ""


@pytest.fixture
def fake(monkeypatch):
    """기본 대역: 소스 있음, 검색 결과 있음, 절단 없음."""
    sources, indexer = FakeSources(), FakeIndexer()
    monkeypatch.setattr(tools, "source_store", sources)
    monkeypatch.setattr(tools, "indexer", indexer)
    # 토큰 계산은 API 왕복이다. 대역으로 막고, 절단 테스트만 따로 켠다.
    monkeypatch.setattr(tools, "count_input_tokens", lambda text: 1)
    return sources, indexer


# --- 스키마 ------------------------------------------------------------------


def test_three_tools_and_no_more():
    """도구를 늘리면 캐시 접두사가 바뀌고 비용 모델이 다시 계산돼야 한다."""
    assert [t["name"] for t in tools.TOOL_SCHEMAS] == ["search_code", "read_file", "grep"]


def test_read_file_requires_the_line_range():
    """**필수가 아니면 모델이 파일을 통째로 부른다.**

    그 결과가 라운드트립마다 이차로 누적돼 비용 상한을 깬다 — 편의 인자가 아니다.
    """
    schema = next(t for t in tools.TOOL_SCHEMAS if t["name"] == "read_file")

    assert set(schema["input_schema"]["required"]) == {"path", "start_line", "end_line"}


def test_every_tool_declares_its_required_inputs():
    for schema in tools.TOOL_SCHEMAS:
        assert schema["input_schema"]["required"], f"{schema['name']} 에 required 가 없다"


# --- search_code --------------------------------------------------------------


def test_search_code_renders_snippets(fake):
    _, indexer = fake
    execute = tools.build_executor(SNAPSHOT)

    out = execute("search_code", {"query": "비밀번호 암호화"})

    assert indexer.queries == ["비밀번호 암호화"]
    assert "질문과 관련된 코드" in out


def test_search_code_says_so_when_nothing_matches(monkeypatch, fake):
    sources, _ = fake
    monkeypatch.setattr(tools, "indexer", FakeIndexer(found=[]))

    out = tools.build_executor(SNAPSHOT)("search_code", {"query": "없는것"})

    assert "검색된 코드가 없습니다" in out


# --- read_file ----------------------------------------------------------------


def test_read_file_numbers_lines_from_the_real_start(fake):
    """도구 결과의 번호는 원본 그대로여야 한다 — 청크와 달리 어긋날 여지가 없다."""
    out = tools.build_executor(SNAPSHOT)(
        "read_file", {"path": "app/main.py", "start_line": 2, "end_line": 3}
    )

    assert "2|def run():" in out
    assert "3|    return 1" in out
    assert "1|import os" not in out


def test_read_file_reports_the_range_in_the_header(fake):
    out = tools.build_executor(SNAPSHOT)(
        "read_file", {"path": "app/main.py", "start_line": 2, "end_line": 3}
    )

    assert out.startswith("### app/main.py (2-3행)")


def test_read_file_clamps_the_end_to_the_file(fake):
    out = tools.build_executor(SNAPSHOT)(
        "read_file", {"path": "app/main.py", "start_line": 6, "end_line": 999}
    )

    assert "(6-7행)" in out


def test_read_file_rejects_a_backwards_range(fake):
    out = tools.build_executor(SNAPSHOT)(
        "read_file", {"path": "app/main.py", "start_line": 9, "end_line": 2}
    )

    assert "보다 작습니다" in out


def test_read_file_rejects_non_integer_lines(fake):
    """모델이 문자열로 줄 번호를 보내는 일이 있다. 예외가 아니라 문장으로 돌려준다."""
    out = tools.build_executor(SNAPSHOT)(
        "read_file", {"path": "app/main.py", "start_line": "2", "end_line": "3"}
    )

    assert "정수로" in out


def test_missing_path_is_distinguished_from_missing_sources(fake):
    """**둘을 같은 문장으로 답하면 안 된다.** 하나는 재색인이 답이고 하나는 경로 오타다."""
    out = tools.build_executor(SNAPSHOT)(
        "read_file", {"path": "app/nope.py", "start_line": 1, "end_line": 5}
    )

    assert "보관된 소스에 없습니다" in out
    assert tools.NO_SOURCES not in out


# --- 도구 목록 고르기 ----------------------------------------------------------


def test_build_gives_every_tool_when_sources_are_stored(fake):
    schemas, execute = tools.build(SNAPSHOT)

    assert schemas == tools.TOOL_SCHEMAS
    assert callable(execute)


def test_build_drops_read_file_and_grep_without_sources(monkeypatch, fake):
    """**빈손 도구를 주면 모델이 "없다"를 "저장소에 없다"로 읽는다.**

    docs/architecture.md §2 의 계약("저장소에 없다"와 "수집 범위에 없다"는 다른 말)이 정확히
    거기서 깨진다. 그리고 이 경로는 측정된 적이 없다 — 판정은 도구 셋이 다 도는
    스냅샷에서 나왔다.
    """
    monkeypatch.setattr(tools, "source_store", FakeSources(files={}))

    schemas, _ = tools.build(SNAPSHOT)

    assert [t["name"] for t in schemas] == ["search_code"]


def test_build_falls_back_to_search_only_when_the_db_is_down(monkeypatch, fake):
    """읽지 못하면 적은 쪽으로 떨어진다 — 없는 소스를 읽으라고 주는 것보다 안전하다."""
    class Broken(FakeSources):
        def count(self, snapshot_id):
            raise psycopg.OperationalError("죽었다")

    monkeypatch.setattr(tools, "source_store", Broken())

    schemas, execute = tools.build(SNAPSHOT)

    assert [t["name"] for t in schemas] == ["search_code"]
    assert callable(execute)


def test_build_counts_the_storage_once(fake):
    """`build` 가 센 값을 실행기가 다시 세지 않는다 — 대화 하나에 왕복 두 번은 낭비다."""
    sources, _ = fake
    _, execute = tools.build(SNAPSHOT)
    execute("read_file", {"path": "app/main.py", "start_line": 1, "end_line": 2})

    assert sources.count_calls == 1


def test_the_two_tool_lists_are_stable_objects(fake):
    """접두사가 갈리는 것은 **스냅샷마다 한 번**이어야 한다. 목록 자체가 흔들리면 안 된다."""
    first, _ = tools.build(SNAPSHOT)
    second, _ = tools.build(SNAPSHOT)

    assert first == second
    assert tools.SEARCH_ONLY_SCHEMAS == tuple(
        t for t in tools.TOOL_SCHEMAS if t["name"] == "search_code"
    )


# --- 보관된 소스가 없는 스냅샷 -------------------------------------------------


@pytest.fixture
def no_sources(monkeypatch, fake):
    sources, _ = fake
    empty = FakeSources(files={}, hits=[])
    monkeypatch.setattr(tools, "source_store", empty)
    return empty


def test_read_file_without_stored_sources(no_sources):
    out = tools.build_executor(SNAPSHOT)(
        "read_file", {"path": "app/main.py", "start_line": 1, "end_line": 5}
    )

    assert out == tools.NO_SOURCES


def test_grep_without_stored_sources(no_sources):
    out = tools.build_executor(SNAPSHOT)("grep", {"pattern": "run"})

    assert out == tools.NO_SOURCES


def test_search_code_still_works_without_stored_sources(no_sources):
    """검색은 청크에서 오므로 보관과 무관하다 — 이게 폴백 경로다."""
    out = tools.build_executor(SNAPSHOT)("search_code", {"query": "무엇이든"})

    assert "질문과 관련된 코드" in out


def test_storage_is_checked_once_per_conversation(no_sources):
    """도구를 부를 때마다 세면 대화 하나에 DB 왕복이 라운드트립 수만큼 는다."""
    execute = tools.build_executor(SNAPSHOT)
    execute("grep", {"pattern": "a"})
    execute("grep", {"pattern": "b"})
    execute("read_file", {"path": "x.py", "start_line": 1, "end_line": 2})

    assert no_sources.count_calls == 1


# --- grep ---------------------------------------------------------------------


def _hits(n: int) -> list[dict]:
    return [{"path": "a.py", "line": i, "text": f"hit {i}"} for i in range(1, n + 1)]


def test_grep_renders_path_line_text(monkeypatch, fake):
    monkeypatch.setattr(tools, "source_store", FakeSources(hits=_hits(2)))

    out = tools.build_executor(SNAPSHOT)("grep", {"pattern": "hit"})

    assert "a.py:1: hit 1" in out
    assert "a.py:2: hit 2" in out


def test_grep_clips_at_the_match_cap_and_says_so(monkeypatch, fake):
    """**절단을 알리지 않으면 모델이 "이게 전부"로 읽는다.**

    대역은 상한보다 많이 주되 **잘린 것과 안 잘린 것의 결과가 달라야** 한다 —
    한 건만 넘겨주면 두 동작이 같은 출력을 내 이 테스트가 아무것도 못 잡는다.
    """
    monkeypatch.setattr(
        tools, "source_store", FakeSources(hits=_hits(tools.MAX_GREP_MATCHES + 10))
    )

    out = tools.build_executor(SNAPSHOT)("grep", {"pattern": "hit"})

    assert out.count("a.py:") == tools.MAX_GREP_MATCHES
    assert f"a.py:{tools.MAX_GREP_MATCHES}: " in out
    assert f"a.py:{tools.MAX_GREP_MATCHES + 1}: " not in out
    assert "넘어 여기까지만" in out


def test_grep_under_the_cap_has_no_truncation_notice(monkeypatch, fake):
    monkeypatch.setattr(tools, "source_store", FakeSources(hits=_hits(3)))

    out = tools.build_executor(SNAPSHOT)("grep", {"pattern": "hit"})

    assert "여기까지만" not in out


# --- 토큰 상한 ----------------------------------------------------------------


def test_long_result_is_cut_and_the_cut_is_stated(monkeypatch, fake):
    """상한을 넘는 결과는 잘리고, **잘렸다는 문구가 결과 안에 있어야** 한다.

    안 적으면 모델이 잘린 것을 "없다"로 읽는다 — 거절 축이 재려는 실패가 그것이다.
    """
    long_file = {"big.py": "".join(f"line {i}\n" for i in range(1, 401))}
    monkeypatch.setattr(tools, "source_store", FakeSources(files=long_file))
    # 실제 토큰 수 대역: 400줄이 상한의 4배라고 답한다 → 약 1/4 만 남아야 한다
    monkeypatch.setattr(
        tools, "count_input_tokens", lambda text: tools.MAX_TOOL_RESULT_TOKENS * 4
    )

    out = tools.build_executor(SNAPSHOT)(
        "read_file", {"path": "big.py", "start_line": 1, "end_line": 400}
    )

    assert "줄만 보냈습니다" in out
    assert "1|line 1" in out          # 앞은 남는다 (전부 버리는 것이 아니다)
    assert "400|line 400" not in out  # 뒤는 잘린다 (그대로 두는 것도 아니다)
    assert 50 < out.count("|line ") < 200


def test_short_result_never_calls_the_token_endpoint(monkeypatch, fake):
    """문자 수 추정이 상한 안이면 토큰 왕복을 하지 않는다 — 추정은 실제보다 많게 잡는다."""
    def must_not_count(text):
        pytest.fail("짧은 결과에 토큰 계산 API 를 불렀다")

    monkeypatch.setattr(tools, "count_input_tokens", must_not_count)

    out = tools.build_executor(SNAPSHOT)(
        "read_file", {"path": "app/main.py", "start_line": 1, "end_line": 3}
    )

    assert "줄만 보냈습니다" not in out


def test_truncation_falls_back_when_tokens_cannot_be_counted(monkeypatch, fake):
    """API 키가 없으면 count_input_tokens 가 None 이다. 그때도 자를 수 있어야 한다."""
    long_file = {"big.py": "".join(f"line {i}\n" for i in range(1, 2001))}
    monkeypatch.setattr(tools, "source_store", FakeSources(files=long_file))
    monkeypatch.setattr(tools, "count_input_tokens", lambda text: None)

    out = tools.build_executor(SNAPSHOT)(
        "read_file", {"path": "big.py", "start_line": 1, "end_line": 2000}
    )

    assert "줄만 보냈습니다" in out


# --- 측정용 호출 내역 ----------------------------------------------------------
#
# **답변 원문만으로는 비용 산식을 되살릴 수 없다.** 지금 상한 셋은 a=150·r=1,500
# 추정 위에 서 있고, 그 가정을 실측으로 대체하려면 "어떤 도구를 어떤 인자로 불러
# 결과가 몇 토큰이었나"가 필요하다. 기록이 없으면 측정을 하고도 다음 설계에 쓸 숫자가
# 안 남는다 — 0단계가 답변 원문을 남겨 둔 덕에 $0 로 재채점한 것과 같은 이유다.


def test_every_call_is_traced_with_its_arguments(fake):
    execute = tools.build_executor(SNAPSHOT)
    execute("search_code", {"query": "인증"})
    execute("read_file", {"path": "app/main.py", "start_line": 1, "end_line": 3})

    assert [e["tool"] for e in execute.trace] == ["search_code", "read_file"]
    assert execute.trace[0]["input"] == {"query": "인증"}
    assert execute.trace[1]["input"]["start_line"] == 1


def test_trace_records_the_result_size(fake):
    """산식의 r 이다. 크기가 없으면 라운드트립 상한을 다시 계산할 수 없다."""
    execute = tools.build_executor(SNAPSHOT)
    execute("read_file", {"path": "app/main.py", "start_line": 1, "end_line": 3})

    entry = execute.trace[0]
    assert entry["sent_chars"] > 0
    assert entry["result_tokens"] > 0


def test_result_tokens_is_measured_before_truncation(monkeypatch, fake):
    """**자른 뒤 값을 남기면 가정을 검증할 수 없다.**

    07 조사가 잡은 가정은 r=1,500 이고 상한은 800 이다. 자른 뒤를 기록하면 언제나
    상한에 눌린 수치가 남아서, "원래 얼마짜리였나"를 영영 못 본다 — 상한이 너무
    빡빡한지도, 1,500 이 현실적이었는지도 판단할 근거가 사라진다.
    """
    long_file = {"big.py": "".join(f"line {i}\n" for i in range(1, 401))}
    monkeypatch.setattr(tools, "source_store", FakeSources(files=long_file))
    monkeypatch.setattr(
        tools, "count_input_tokens", lambda text: tools.MAX_TOOL_RESULT_TOKENS * 4
    )
    execute = tools.build_executor(SNAPSHOT)

    execute("read_file", {"path": "big.py", "start_line": 1, "end_line": 400})

    entry = execute.trace[0]
    # 절단 전 크기가 그대로 남는다 — 상한에 눌린 800 이 아니다
    assert entry["result_tokens"] == tools.MAX_TOOL_RESULT_TOKENS * 4
    # 실제로 간 것은 잘린 쪽이다. 두 값은 다른 시점이다
    assert entry["sent_chars"] < len(long_file["big.py"])


def test_trace_does_not_carry_the_result_body(fake):
    """본문은 보관 소스에 이미 있다. 기록에 또 실으면 jsonl 이 소스만큼 커진다."""
    execute = tools.build_executor(SNAPSHOT)
    execute("read_file", {"path": "app/main.py", "start_line": 1, "end_line": 3})

    assert "def run():" not in repr(execute.trace[0])


def test_trace_names_which_cap_fired(monkeypatch, fake):
    """"캡에 걸렸다"로는 부족하다 — **어느 캡인지**를 알아야 무엇을 고칠지 정해진다."""
    monkeypatch.setattr(
        tools, "source_store", FakeSources(hits=_hits(tools.MAX_GREP_MATCHES + 10))
    )
    execute = tools.build_executor(SNAPSHOT)

    execute("grep", {"pattern": "hit"})

    assert execute.trace[0]["caps"] == ["grep_matches"]


def test_trace_marks_the_token_cap_separately(monkeypatch, fake):
    long_file = {"big.py": "".join(f"line {i}\n" for i in range(1, 401))}
    monkeypatch.setattr(tools, "source_store", FakeSources(files=long_file))
    monkeypatch.setattr(
        tools, "count_input_tokens", lambda text: tools.MAX_TOOL_RESULT_TOKENS * 4
    )
    execute = tools.build_executor(SNAPSHOT)

    execute("read_file", {"path": "big.py", "start_line": 1, "end_line": 400})

    assert execute.trace[0]["caps"] == ["result_tokens"]
    assert execute.trace[0]["result_tokens"] == tools.MAX_TOOL_RESULT_TOKENS * 4


def test_uncapped_calls_record_no_cap(fake):
    execute = tools.build_executor(SNAPSHOT)
    execute("read_file", {"path": "app/main.py", "start_line": 1, "end_line": 3})

    assert execute.trace[0]["caps"] == []


def test_trace_marks_errors(monkeypatch, fake):
    class Broken(FakeSources):
        def get_file(self, snapshot_id, path):
            raise psycopg.OperationalError("죽었다")

    monkeypatch.setattr(tools, "source_store", Broken())
    execute = tools.build_executor(SNAPSHOT)

    execute("read_file", {"path": "a.py", "start_line": 1, "end_line": 2})
    execute("list_files", {})

    assert [e["error"] for e in execute.trace] == [True, True]


def test_each_conversation_gets_its_own_trace(fake):
    """실행기는 대화 하나에 묶인다. 내역이 섞이면 질의별 집계가 어긋난다."""
    first, second = tools.build_executor(SNAPSHOT), tools.build_executor(SNAPSHOT)
    first("grep", {"pattern": "a"})

    assert len(first.trace) == 1
    assert second.trace == []


# --- 오류 처리 ----------------------------------------------------------------


def test_unknown_tool_is_answered_not_raised(fake):
    out = tools.build_executor(SNAPSHOT)("list_files", {})

    assert "없는 도구입니다" in out


def test_db_failure_becomes_a_sentence(monkeypatch, fake):
    """도구 하나가 DB 로 죽어도 대화 전체가 실패하면 안 된다."""
    class Broken(FakeSources):
        def get_file(self, snapshot_id, path):
            raise psycopg.OperationalError("죽었다")

    monkeypatch.setattr(tools, "source_store", Broken())

    out = tools.build_executor(SNAPSHOT)(
        "read_file", {"path": "app/main.py", "start_line": 1, "end_line": 2}
    )

    assert "지금 쓸 수 없습니다" in out


def test_empty_arguments_do_not_raise(fake):
    execute = tools.build_executor(SNAPSHOT)

    assert "비어 있습니다" in execute("search_code", {})
    assert "비어 있습니다" in execute("grep", {"pattern": "  "})
    assert "비어 있습니다" in execute("read_file", {"start_line": 1, "end_line": 2})
