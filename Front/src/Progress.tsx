import { useEffect, useState } from "react";

/** 단계와 그 단계로 넘어가는 경과 시간(ms).
 *
 * 백엔드 `/analyze` 는 단일 POST 라 진행 상황을 알려주지 않는다. 그래서 이 표시는
 * 서버의 실제 진행이 아니라 **경과 시간으로 추정한 값**이다. 전환 시점의 근거는
 * 실측 — check_repo_access 는 GitHub 1회, fetch_repo_context 는 3~6회이고,
 * 그 뒤 남는 시간은 전부 LLM 호출이다.
 */
const STAGES = [
  { at: 0, label: "저장소 확인" },
  { at: 1500, label: "파일 수집 (README · 파일 트리 · 매니페스트)" },
  { at: 5000, label: "AI 요약 작성" },
];

function format(ms: number) {
  const total = Math.floor(ms / 1000);
  return `${Math.floor(total / 60)}:${String(total % 60).padStart(2, "0")}`;
}

export default function AnalyzeProgress() {
  const [elapsed, setElapsed] = useState(0);

  useEffect(() => {
    const started = Date.now();
    const id = setInterval(() => setElapsed(Date.now() - started), 250);
    return () => clearInterval(id);
  }, []);

  // 마지막 단계는 응답이 올 때까지 계속 진행 중으로 둔다 (끝나는 시점을 알 수 없다).
  let active = 0;
  for (let i = 0; i < STAGES.length; i++) {
    if (elapsed >= STAGES[i].at) active = i;
  }

  return (
    <section className="progress" role="status" aria-live="polite">
      <h3>분석 중</h3>

      <ol>
        {STAGES.map((stage, i) => (
          <li key={stage.label} className={i < active ? "done" : i === active ? "active" : ""}>
            <span className="mark">
              {i < active ? "✓" : i === active ? <span className="spinner" /> : "·"}
            </span>
            {stage.label}
            {/* 단계별이 아니라 전체 경과 시간. 1초마다 바뀌므로 낭독에서는 뺀다. */}
            {i === active && (
              <span className="elapsed" aria-hidden>
                {format(elapsed)}
              </span>
            )}
          </li>
        ))}
      </ol>

      <p className="progress-note">
        레포 크기에 따라 10~40초 정도 걸립니다. 한 번 분석한 레포는 다음부터 바로 표시됩니다.
      </p>
    </section>
  );
}
