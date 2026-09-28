"""`runs` 실사용 기록에서 프로덕션 기준선을 뽑는다.

    python scripts/aggregate_runs.py

프로덕션 기준선(질문당 실비·왕복 수·접두사 크기)을 항목별로 출력한다. 표본이 쌓이면 다시 돌린다.

**DB 만 본다. `logs/runs.jsonl` 은 읽지 않는다.** 그 파일 31행은 `migrate_runs_to_db.py`
로 이미 전부 `runs` 에 이관돼 있어(ts 대조 31/31), 합산하면 **이중계상**이다.
`run_log.read()` 를 쓰지 않는 것도 같은 이유다 — 그쪽은 DB 조회가 실패하면 파일로
넘어가므로, 집계에 쓰면 어느 쪽을 봤는지 모른 채 숫자가 달라진다.

**읽기만 한다.** SELECT 뿐이고 `init_schema()` 도 부르지 않는다. LLM 도 부르지 않으므로
무과금이다.
"""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.config import DAILY_LLM_CALL_LIMIT, DAILY_TOKEN_LIMIT  # noqa: E402
from app.db import pool  # noqa: E402

# ── FULL_INJECTION_MAX_TOKENS 역산 ──
#
#   S_max = (3·C_rag/p_in + snip) / w̄  ,  w̄ = Σ(1.25 + 0.1(N−1)) / ΣN
#
# C_rag·p_in·snip 은 **고정**한다. 로그가 "w̄ 가 임계값을 지배한다"고 적어 두었으므로,
# 세션 표본만 바꿔 그 축을 격리해서 본다. 셋을 같이 움직이면 무엇이 답을 바꿨는지 모른다.
C_RAG = 0.0237       # RAG 기준선 질문당 비용 (정가 환산, 4세션 13질문 실측)
P_IN = 3.00e-06      # 입력 정가 $/토큰
SNIP = 2128          # 질문당 스니펫 몫 (TOP_K 조립 실측 평균)

# 산식 자체가 틀어지지 않았는지 보는 대조군. 원래 표본 [3,6,2,2] 를 넣으면 이 값이 나와야
# 한다. 로그에 적힌 56,991 과 82(0.14%) 차이는 로그 쪽 반올림이다.
ORIGINAL_SESSIONS = (3, 6, 2, 2)
ORIGINAL_S_MAX = 56909


def w_bar(question_counts) -> float:
    counts = [n for n in question_counts if n > 0]
    return sum(1.25 + 0.1 * (n - 1) for n in counts) / sum(counts)


def s_max(question_counts) -> float:
    return (3 * C_RAG / P_IN + SNIP) / w_bar(question_counts)


def _head(title: str) -> None:
    print(f"\n{'─' * 68}\n{title}\n{'─' * 68}")


def _rows(sql: str, params=()) -> list[dict]:
    with pool.cursor(commit=False) as cur:
        cur.execute(sql, params)
        return cur.fetchall()


# ── 표본 ─────────────────────────────────────────────────────

def sample() -> None:
    _head("0. 표본")
    total = _rows(
        "SELECT count(*) AS n, min(ts) AS first, max(ts) AS last FROM runs"
    )[0]
    print(f"총 {total['n']}행 · {total['first']:%Y-%m-%d %H:%M} ~ {total['last']:%Y-%m-%d %H:%M} (UTC)")
    print("source × cached:")
    for row in _rows(
        "SELECT source, cached, count(*) AS n FROM runs GROUP BY 1, 2 ORDER BY 3 DESC"
    ):
        label = "캐시 히트" if row["cached"] else "LLM 호출"
        print(f"  {row['source']:12s} {label}  {row['n']:3d}")
    print("저장소:")
    for row in _rows("SELECT repo, count(*) AS n FROM runs GROUP BY 1 ORDER BY 2 DESC"):
        print(f"  {row['repo']:24s} {row['n']:3d}")


# ── 기준선 ───────────────────────────────────────────────────

def daily_usage() -> None:
    _head("1. [기준선] 일일 실사용 — rate_limit_daily")
    rows = _rows("SELECT day, calls, tokens FROM rate_limit_daily ORDER BY day")
    for row in rows:
        print(f"  {row['day']}  {row['calls']:4d}건  {row['tokens']:>10,}토큰")
    if not rows:
        print("  (기록 없음)")
        return
    peak_calls = max(r["calls"] for r in rows)
    peak_tokens = max(r["tokens"] for r in rows)
    print(f"\n  최대 {peak_calls}건 / {peak_tokens:,}토큰 ({len(rows)}일)")
    print(
        f"  상한 대비: 호출 {peak_calls / DAILY_LLM_CALL_LIMIT * 100:.1f}%"
        f" (한도 {DAILY_LLM_CALL_LIMIT:,})"
        f" · 토큰 {peak_tokens / DAILY_TOKEN_LIMIT * 100:.1f}%"
        f" (한도 {DAILY_TOKEN_LIMIT:,})"
    )


def cost_per_question() -> None:
    _head("2. [기준선] 질문당 실비 — 캐시 히트 제외")
    for row in _rows(
        """SELECT source, count(*) AS n, avg(cost_usd) AS avg, min(cost_usd) AS lo,
                  max(cost_usd) AS hi, sum(cost_usd) AS total
             FROM runs WHERE NOT cached GROUP BY 1 ORDER BY 1"""
    ):
        print(
            f"  {row['source']:12s} {row['n']:3d}건  평균 ${row['avg']:.5f}"
            f"  ({row['lo']:.5f} ~ {row['hi']:.5f})  합계 ${row['total']:.4f}"
        )
    latest = _rows(
        """SELECT count(*) AS n, avg(cost_usd) AS avg FROM runs
            WHERE source = 'chat' AND round_trips IS NOT NULL"""
    )[0]
    if latest["n"]:
        print(f"\n  최신 구성 chat(round_trips 유효): {latest['n']}건 평균 ${latest['avg']:.5f}")


# ── 관측치 ───────────────────────────────────────────────────

def prefix_sizes() -> None:
    _head("3. [관측치] 저장소별 캐시 접두사 크기")
    print("  평균을 쓰지 않는다 — 저장소마다 자릿수가 다르고, 같은 저장소도 재색인으로 변한다.")
    # **접두사는 cache_write_tokens 로 읽는다. cache_read_tokens 는 접두사가 아니다.**
    # 한 행은 질문 하나이고 그 안에 API 호출이 (round_trips + 1) 번 있는데, run_log 가
    # 토큰을 **합산해** 한 줄로 남긴다. 그래서 읽기는 접두사 × 읽은 호출 수다.
    # 쓰기는 캐시를 만들 때 한 번뿐이라 합산돼도 접두사 그대로다.
    for row in _rows(
        """SELECT repo, cache_write_tokens AS prefix, min(ts)::date AS first, count(*) AS n
             FROM runs WHERE source = 'chat' AND cache_write_tokens > 0
            GROUP BY 1, 2 ORDER BY 1, 3"""
    ):
        print(f"  {row['repo']:24s} {row['prefix']:>7,}  (첫 관측 {row['first']}, {row['n']}회 생성)")

    # 도구 도입 이전(round_trips 열이 생기기 전) 행에는 cache_write 가 없다. 그 시기는
    # 도구가 없어 질문당 호출이 1번뿐이라 읽기가 곧 접두사다. 같은 저장소의 접두사가
    # 재색인·도구 도입으로 어떻게 움직였는지는 이 값이 있어야 보인다.
    print("\n  도구 도입 이전 (호출 1번 = 읽기가 곧 접두사):")
    for row in _rows(
        """SELECT repo, cache_read_tokens AS prefix, min(ts)::date AS first, count(*) AS n
             FROM runs WHERE source = 'chat' AND round_trips IS NULL AND cache_read_tokens > 0
            GROUP BY 1, 2 ORDER BY 1, 3"""
    ):
        print(f"  {row['repo']:24s} {row['prefix']:>7,}  (첫 관측 {row['first']}, {row['n']}회)")

    # 검산 — 읽기가 접두사의 정수배인가. 어긋나면 위 해석이 틀린 것이다.
    print("\n  검산: 읽기 = 접두사 × 읽은 호출 수 (round_trips 가 있는 행만)")
    bad = 0
    for row in _rows(
        """SELECT r.repo, r.round_trips AS rt, r.cache_write_tokens AS w,
                  r.cache_read_tokens AS rd,
                  (SELECT max(cache_write_tokens) FROM runs p
                    WHERE p.repo = r.repo AND p.cache_write_tokens > 0
                      AND p.ts <= r.ts) AS prefix
             FROM runs r
            WHERE r.source = 'chat' AND r.round_trips IS NOT NULL AND r.cache_read_tokens > 0
            ORDER BY r.ts"""
    ):
        prefix = row["prefix"]
        expected = (row["rt"] + 1) - (1 if row["w"] else 0)
        actual = row["rd"] / prefix if prefix else 0
        ok = abs(actual - expected) < 1e-9
        bad += not ok
        print(
            f"  {row['repo']:24s} 왕복 {row['rt']}  읽기 {row['rd']:>7,}"
            f" ÷ {prefix:>6,} = {actual:.2f}  (기대 {expected})  {'ok' if ok else '←어긋남'}"
        )
    print(f"  → 어긋난 행 {bad}건")


def round_trips() -> None:
    _head("4. [관측치] round_trips — 열이 나중에 추가돼 옛 행은 NULL")
    # **캐시 히트를 뺀다.** append_cache_hit() 이 round_trips=0 을 남기는데 그 행은 LLM 을
    # 부른 적이 없다. 넣으면 평균이 0 쪽으로 끌려가고(0.91 → 0.59) 질문당 비용도 0 이
    # 섞여 반토막 난다 — 도구를 몇 번 불렀는가는 호출한 요청에서만 뜻이 있다.
    null_n = _rows(
        "SELECT count(*) AS n FROM runs WHERE NOT cached AND round_trips IS NULL"
    )[0]["n"]
    rows = _rows(
        """SELECT source, round_trips AS rt, count(*) AS n, avg(cost_usd) AS cost,
                  avg(input_tokens + output_tokens
                      + coalesce(cache_write_tokens, 0) + coalesce(cache_read_tokens, 0)) AS tok
             FROM runs WHERE NOT cached AND round_trips IS NOT NULL
            GROUP BY 1, 2 ORDER BY 1, 2"""
    )
    valid = sum(r["n"] for r in rows)
    print(f"  유효 {valid}건 · NULL {null_n}건 (열 추가 이전)")
    # source 를 나눈다 — analyze 는 도구를 붙이지 않는 경로라 구조적으로 0 이다.
    # chat 과 같이 평균 내면 "도구를 덜 부른다"가 아니라 "도구가 없었다"가 섞인다.
    for row in rows:
        print(
            f"  {row['source']:10s} {row['rt']}회  {row['n']:3d}건"
            f"  질문당 ${row['cost']:.5f}  총 {row['tok']:>8,.0f}토큰"
        )
    chat = [r for r in rows if r["source"] == "chat"]
    if chat:
        n = sum(r["n"] for r in chat)
        mean = sum(r["rt"] * r["n"] for r in chat) / n
        print(
            f"\n  chat {n}건 평균 {mean:.2f}회 · 최대 {max(r['rt'] for r in chat)}회"
            f" (상한 MAX_TOOL_ROUND_TRIPS=3)"
        )


def stop_reasons() -> None:
    _head("5. stop_reason")
    null_n = _rows("SELECT count(*) AS n FROM runs WHERE stop_reason IS NULL")[0]["n"]
    rows = _rows(
        """SELECT stop_reason, count(*) AS n FROM runs
            WHERE stop_reason IS NOT NULL GROUP BY 1 ORDER BY 2 DESC"""
    )
    valid = sum(r["n"] for r in rows)
    print(f"  유효 {valid}건 · NULL {null_n}건 (열 추가 이전 + 캐시 히트)")
    for row in rows:
        print(f"  {row['stop_reason']:20s} {row['n']:3d}")
    if valid < 10:
        print("\n  ** 표본이 집계할 수준이 아니다. **")


# ── 세션과 임계값 역산 ───────────────────────────────────────

def sessions() -> list[dict]:
    _head("6. [관측치] 세션당 질문 수")
    rows = _rows(
        """SELECT s.id,
                  count(*) FILTER (WHERE m.role = 'user') AS questions,
                  -- 여러 날에 걸쳐 이어 쓴 세션. 자정을 걸친 단일 세션이 여기 섞일 수
                  -- 있지만, 실사용 세션 길이의 대표값이 아니라는 판정은 그대로다.
                  (s.last_message_at::date > s.created_at::date) AS spans_days,
                  s.created_at::date AS started, s.last_message_at::date AS ended
             FROM chat_sessions s LEFT JOIN messages m ON m.session_id = s.id
            GROUP BY s.id, s.created_at, s.last_message_at
            ORDER BY s.created_at"""
    )
    live = [r for r in rows if r["questions"] > 0]
    print(f"  세션 {len(rows)}개 · 질문이 있는 세션 {len(live)}개")
    for row in live:
        mark = "  ← 여러 날 이어 씀" if row["spans_days"] else ""
        print(f"  {row['started']} ~ {row['ended']}  {row['questions']:2d}질문{mark}")
    return live


def threshold(live: list[dict]) -> None:
    _head("7. FULL_INJECTION_MAX_TOKENS 역산")

    check = s_max(ORIGINAL_SESSIONS)
    drift = abs(check - ORIGINAL_S_MAX) / ORIGINAL_S_MAX
    print(f"  대조군 — 원래 4세션 {list(ORIGINAL_SESSIONS)}: {check:,.0f}")
    if drift > 0.002:
        print(f"  ** 산식이 틀어졌다: {ORIGINAL_S_MAX:,} 이 나와야 한다 (차이 {drift:.1%}) **")
        return
    print(f"     기대값 {ORIGINAL_S_MAX:,} 과 일치 — 산식 정상\n")

    cuts = [
        ("전부", [r["questions"] for r in live]),
        ("하루 안에 끝난 세션만", [r["questions"] for r in live if not r["spans_days"]]),
    ]
    for label, counts in cuts:
        if not counts:
            print(f"  {label:24s} (표본 없음)")
            continue
        print(
            f"  {label:24s} {sorted(counts)}  ΣN={sum(counts):3d}"
            f"  w̄={w_bar(counts):.4f}  S_max={s_max(counts):,.0f}"
        )
    print(
        "\n  두 컷이 갈리면 '빠듯한가'에 단일한 답이 없다는 뜻이다."
        " 표본 선택이 답을 뒤집는 동안에는 값을 바꾸지 않는다."
    )


def main() -> int:
    if not pool.is_enabled():
        print("DATABASE_URL 이 없습니다. Back/.env 를 확인하세요.")
        return 1
    sample()
    daily_usage()
    cost_per_question()
    prefix_sizes()
    round_trips()
    stop_reasons()
    threshold(sessions())
    return 0


if __name__ == "__main__":
    try:
        code = main()
    finally:
        # 풀을 열어 둔 채 인터프리터가 끝나면 psycopg_pool 이 종료 시점에
        # 워커를 join 하려다 PythonFinalizationError 를 뱉는다.
        pool.close()
    raise SystemExit(code)
