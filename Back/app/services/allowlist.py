"""분석을 허용할 저장소 목록. 공개 데모라 아무 저장소나 받으면 비용이 열려 있다.

`DAILY_TOKEN_LIMIT` 은 비용 천장이라 서비스 전체 합산이고, 한 명이 큰 저장소를
몇 개 넣으면 나머지가 막힌다. 무는 것은 LLM 비용만이 아니다 — 색인은 큐가 하나뿐이라
큰 저장소 하나가 그 뒤의 모든 색인을 세운다.

**이 목록이 걸리는 것은 로그인하지 않은 요청뿐이다.** 로그인한 사람은 공개 저장소를
무엇이든 넣을 수 있다.

**전에는 반대로 적혀 있었다("로그인은 무엇을 분석할지 안 가른다"). 그때는 청크 상한이
없었다** — 목록이 크기를 막는 유일한 방어선이라, 열면 큰 저장소 하나가 색인을 통째로
세울 수 있었다. 지금은 `MAX_INDEX_CHUNKS` 가 한 건당 크기를 막는다(`services/indexer.py`).
그래서 이 목록의 역할이 **크기 방어에서 "로그인하지 않은 사람의 범위 제한"으로 좁아졌다.**

**위 첫 문단은 여전히 유효하다.** 청크 상한은 저장소 **하나**의 크기를 막을 뿐 줄 서는
것 자체를 안 막는다 — 큐는 여전히 하나이고 거기에는 주인이 없다(docs/architecture.md §8).

**로그인이 꺼져 있으면 모두에게 걸린다.** `GITHUB_OAUTH_CLIENT_ID` 가 비면
`login_session.current_user_id()` 가 언제나 None 이라 아래 판정이 로그인 도입 전과
글자 그대로 같다. DB 를 못 읽을 때도 마찬가지다 — 그쪽도 익명으로 떨어지므로
**로그인한 사람까지 목록 밖 저장소가 막힌다.** 막는 쪽으로 틀리는 것이라 그대로 둔다.

**목록은 코드가 아니라 배포 설정이 정한다**(`config.ALLOWED_REPOS`). 저장소 이름을
프로덕션 코드에 쓰지 않는다는 규칙(docs/architecture.md §3) 때문이기도 하고, 무엇을 데모로 걸지는
배포마다 다른 값이라 애초에 코드가 알 일이 아니다. 이 모듈이 아는 것은 형식뿐이다.

**비어 있으면 제한을 끈다.** 상한들이 `0` 으로 꺼지는 것과 같은 관용구다 — 로컬 개발은
`.env` 에 이 값을 안 적으면 지금까지처럼 임의 저장소를 넣을 수 있고, 켜는 곳은 배포의
`--env-file` 하나뿐이다. 끄는 스위치를 따로 만들지 않는 이유는 둘이 되면 어느 쪽이
이기는지를 또 기억해야 하기 때문이다.
"""

from app.config import ALLOWED_REPOS
from app.services import oauth


class RepoNotAllowed(Exception):
    """허용 목록에 없는 저장소. status_code는 API 계층이 그대로 응답에 쓴다."""

    def __init__(self, message: str, status_code: int = 403):
        super().__init__(message)
        self.status_code = status_code


def _parse(raw: str) -> tuple[str, ...]:
    """`owner/name` 을 쉼표로 이은 문자열 → 정규화된 튜플. 순서는 적힌 대로 둔다.

    **소문자로 접는다.** `parse_github_url` 이 owner/repo 를 소문자로 돌려주므로
    설정 쪽에 대문자로 적혀 있으면 그대로는 영영 안 맞는다. GitHub 자체가 저장소
    이름의 대소문자를 구분하지 않아서, 적은 사람은 틀렸다는 것도 모른다.

    순서를 지키는 것은 차단 메시지에 그대로 실리기 때문이다 — 사람이 적은 차례가
    유지되는 편이 읽기 좋고, 정렬하면 설정과 화면이 다르게 보인다.
    """
    seen, items = set(), []
    for entry in raw.split(","):
        name = entry.strip().strip("/").lower()
        if name and name not in seen:
            seen.add(name)
            items.append(name)
    return tuple(items)


ALLOWED = _parse(ALLOWED_REPOS)


def enabled() -> bool:
    """목록이 비어 있으면 이 제한은 꺼진 상태다."""
    return bool(ALLOWED)


def _blocked_message() -> str:
    """차단 문구. **로그인이 꺼져 있으면 도입 전 문구 그대로다.**

    켜져 있을 때만 로그인을 안내한다 — 꺼져 있으면 누를 버튼이 화면에 없다
    (`/auth/me` 가 `enabled: False` 를 주면 프론트가 로그인 줄을 안 그린다).
    안 되는 것을 하라고 시키는 안내가 된다.

    **"아무 저장소나"라고 쓰지 않는다.** 저장소는 언제나 서버의 `GITHUB_TOKEN` 으로
    읽으므로(`services/github_client.py`) 로그인해도 비공개는 안 열린다. 그 한 마디가
    없으면 로그인한 사람이 자기 비공개 저장소를 넣어 보고 404 를 받는다.

    두 갈래 모두 목록을 싣는다 — 무엇을 넣을 수 있는지가 이 응답의 본론이다.
    """
    listed = ", ".join(ALLOWED)
    if not oauth.enabled():
        return (
            "이 서비스는 공개 데모라 지정된 저장소만 분석합니다. "
            f"사용할 수 있는 저장소: {listed}"
        )
    return (
        "로그인하지 않으면 정해진 저장소만 분석할 수 있습니다. "
        f"지금 넣을 수 있는 저장소: {listed}\n"
        "GitHub 으로 로그인하면 공개 저장소는 무엇이든 넣을 수 있습니다"
        "(비공개 저장소는 로그인해도 열리지 않습니다)."
    )


def check(owner: str, name: str, user_id: int | None = None) -> None:
    """허용 목록에 없으면 RepoNotAllowed. 꺼져 있으면 아무 일도 하지 않는다.

    **owner/name 은 `parse_github_url` 이 준 소문자 키를 그대로 받는다.** 여기서
    다시 정규화하지 않는 이유는, 하면 "무엇이 이미 정규화된 값인가"가 흐려져서
    호출부마다 다르게 접게 되기 때문이다.

    `user_id` 는 로그인한 요청에만 있다(`login_session.current_user_id`).
    **값이 있으면 목록을 안 본다** — 그것이 이번 조건의 전부다. 기본값이 None 이라
    인자를 안 넘기면 동작이 로그인 도입 전과 같다.

    **`oauth.enabled()` 로 한 번 더 확인하지 않는다.** 로그인이 꺼졌는데 `user_id` 가
    들어오는 경로는 코드에 없고(`current_user()` 가 쿠키를 아예 안 본다), 판정을 두
    값에 걸면 어느 쪽이 이기는지를 또 기억해야 한다. 문구를 고를 때만 그 값을 본다.
    """
    if not ALLOWED or user_id is not None or f"{owner}/{name}" in ALLOWED:
        return
    raise RepoNotAllowed(_blocked_message())
