"""/auth/* 경로. **GitHub 도 DB 도 부르지 않는다** (둘 다 대역이다).

## 이 파일에서 가장 중요한 것은 '꺼짐' 쪽이다

배포는 지금 로그인을 끈 채로 돈다(`GITHUB_OAUTH_CLIENT_ID` 가 비어 있다). 그러므로
**꺼진 상태의 동작이 로그인 도입 전과 같다는 것이 유일한 안전 보증**이고, 그 보증은
여기서만 검사된다. 켜짐 쪽 테스트가 아무리 많아도 그걸 대신하지 못한다.

실제 GitHub 을 때리지 않는다 — 테스트가 네트워크에 묶이면 조용히 느려지고
남의 서버 상태에 결과가 좌우된다.
"""

import pytest
from fastapi.testclient import TestClient

from app.api import auth as auth_api
from app.main import app
from app.services import github_client, login_session, oauth

CLIENT_ID = "test-client-id"
STATE = "test-state-value"

PROFILE = {"github_user_id": 4242, "login": "octocat", "avatar_url": "https://x/a.png"}


class FakeUsers:
    """users 모듈 대역. DB 쿼리 자체는 tests/test_db_users.py 가 실제 DB 로 본다."""

    def __init__(self):
        self.upserted: list[tuple] = []
        self.logins: dict[str, dict] = {}
        self.deleted: list[str] = []

    def upsert(self, github_user_id, login, avatar_url):
        self.upserted.append((github_user_id, login, avatar_url))
        return 7

    def create_login(self, user_id, days):
        self.logins["login-id-1"] = {
            "id": user_id, "login": "octocat", "avatar_url": None
        }
        return "login-id-1"

    def get_login(self, login_id):
        return self.logins.get(login_id)

    def delete_login(self, login_id):
        self.deleted.append(login_id)
        self.logins.pop(login_id, None)


@pytest.fixture
def users(monkeypatch):
    fake = FakeUsers()
    monkeypatch.setattr(auth_api, "users", fake)
    monkeypatch.setattr(login_session, "users", fake)
    return fake


@pytest.fixture
def off(monkeypatch):
    """로그인이 꺼진 상태. **이것이 지금 배포의 상태다.**"""
    monkeypatch.setattr(oauth, "GITHUB_OAUTH_CLIENT_ID", "")


@pytest.fixture
def on(monkeypatch):
    monkeypatch.setattr(oauth, "GITHUB_OAUTH_CLIENT_ID", CLIENT_ID)
    monkeypatch.setattr(oauth, "GITHUB_OAUTH_CLIENT_SECRET", "test-secret")
    monkeypatch.setattr(oauth, "new_state", lambda: STATE)
    monkeypatch.setattr(oauth, "exchange_code", lambda code: "token-" + code)
    monkeypatch.setattr(oauth, "fetch_user", lambda token: PROFILE)


@pytest.fixture
def client():
    # lifespan 을 돌리지 않는다 — init_schema 가 실제 DB 를 찾으려 들고,
    # 임베딩 예열 스레드가 수백 MB 모델을 올린다 (tests/test_chat_api.py 와 같은 이유).
    return TestClient(app)


def _cookie_attrs(response, name: str) -> str | None:
    """Set-Cookie 헤더 원문. 속성을 봐야 하므로 cookiejar 가 아니라 헤더를 읽는다."""
    for raw in response.headers.get_list("set-cookie"):
        if raw.startswith(f"{name}="):
            return raw
    return None


# ── 꺼짐이 기본이다 ──────────────────────────────────────────


@pytest.mark.parametrize(
    "method,path",
    [
        ("get", "/auth/login"),
        ("get", "/auth/callback"),
        ("post", "/auth/logout"),
        ("get", "/auth/repos"),
    ],
)
def test_every_auth_path_is_404_when_disabled(client, off, method, path):
    """**CLIENT_ID 가 비면 그 경로는 존재하지 않는 것으로 다룬다.**

    403 이 아니라 404 인 이유는 "설정하면 열리는 문이 여기 있다"를 알릴 이유가 없어서다.
    """
    assert getattr(client, method)(path).status_code == 404


def test_status_says_disabled_instead_of_404(client, off):
    """`/auth/me` 만 404 가 아니다 — 화면이 이 값으로 버튼을 그릴지 정한다.

    404 로 답하면 꺼둔 상태(지금 배포)에서 콘솔에 오류가 남아 고장처럼 보인다.
    """
    body = client.get("/auth/me").json()

    assert body == {"enabled": False, "user": None}


def test_a_leftover_cookie_does_nothing_when_disabled(client, off, users):
    """껐을 때 **옛 로그인 쿠키가 남아 있어도 아무 영향이 없다.**

    이게 없으면 "껐다"가 "새 로그인만 막았다"가 된다 — 이미 로그인한 브라우저는
    계속 소유자로 행동하고, 그건 끈 상태가 도입 전과 같다는 보증을 깨뜨린다.
    """
    users.logins["login-id-1"] = {"id": 7, "login": "octocat", "avatar_url": None}
    client.cookies.set(login_session.COOKIE_NAME, "login-id-1")

    assert client.get("/auth/me").json() == {"enabled": False, "user": None}


# ── 켜짐 ─────────────────────────────────────────────────────


def test_login_redirects_to_github_and_plants_the_state_cookie(client, on):
    res = client.get("/auth/login", follow_redirects=False)

    assert res.status_code == 302
    assert res.headers["location"].startswith("https://github.com/login/oauth/authorize")
    assert f"state={STATE}" in res.headers["location"]
    assert _cookie_attrs(res, login_session.STATE_COOKIE_NAME)


def test_the_cookies_are_httponly_and_samesite_lax(client, on, users):
    """**두 속성이 다 필요하다.**

    HttpOnly 가 없으면 스크립트가 읽는다 — 화면이 LLM 답변을 마크다운으로 렌더한다.
    SameSite 가 없으면 남의 사이트에서 온 요청에도 실린다.
    `Strict` 로 조이면 안 된다 — GitHub 에서 돌아오는 이동에 안 실려 로그인이 통째로
    깨지므로, 여기서 `lax` 를 **값으로** 고정한다.
    """
    started = client.get("/auth/login", follow_redirects=False)
    state_cookie = _cookie_attrs(started, login_session.STATE_COOKIE_NAME)

    client.cookies.set(login_session.STATE_COOKIE_NAME, STATE)
    done = client.get(
        f"/auth/callback?code=abc&state={STATE}", follow_redirects=False
    )
    login_cookie = _cookie_attrs(done, login_session.COOKIE_NAME)

    for raw in (state_cookie, login_cookie):
        assert "HttpOnly" in raw
        assert "SameSite=lax" in raw


def test_secure_follows_the_configured_value(client, on, users, monkeypatch):
    """`Secure` 는 출처가 https 인지로 갈린다 — 양쪽을 다 고정한다.

    한쪽만 두면 값을 뒤집는 변이가 통과한다. 로컬(http)에서 켜지면 쿠키가 조용히
    버려지고, 배포(https)에서 빠지면 평문으로 나간다 — **둘 다 오류가 안 난다.**
    """
    monkeypatch.setattr(login_session.config, "COOKIE_SECURE", False)
    assert "Secure" not in _cookie_attrs(
        client.get("/auth/login", follow_redirects=False),
        login_session.STATE_COOKIE_NAME,
    )

    monkeypatch.setattr(login_session.config, "COOKIE_SECURE", True)
    assert "Secure" in _cookie_attrs(
        client.get("/auth/login", follow_redirects=False),
        login_session.STATE_COOKIE_NAME,
    )


def test_the_callback_signs_the_user_in(client, on, users):
    client.cookies.set(login_session.STATE_COOKIE_NAME, STATE)

    res = client.get(f"/auth/callback?code=abc&state={STATE}", follow_redirects=False)

    assert res.status_code == 302
    assert users.upserted == [(4242, "octocat", "https://x/a.png")]
    assert _cookie_attrs(res, login_session.COOKIE_NAME)


def test_me_reports_the_signed_in_user(client, on, users):
    users.logins["login-id-1"] = {
        "id": 7, "login": "octocat", "avatar_url": "https://x/a.png"
    }
    client.cookies.set(login_session.COOKIE_NAME, "login-id-1")

    assert client.get("/auth/me").json() == {
        "enabled": True,
        "user": {"login": "octocat", "avatar_url": "https://x/a.png"},
    }


# ── state (CSRF) ─────────────────────────────────────────────


@pytest.mark.parametrize(
    "cookie,query",
    [
        (None, STATE),          # 우리가 시작한 인가가 아니다
        (STATE, ""),            # GitHub 이 state 를 안 돌려줬다
        (STATE, "other-state"),  # 값이 다르다
    ],
)
def test_a_mismatched_state_is_rejected_before_any_exchange(
    client, on, monkeypatch, cookie, query
):
    """**대조가 코드 교환보다 먼저다.**

    뒤에 두면 남이 시작한 인가로 우리 서버가 GitHub 을 부른다. 그래서 거절만 보지 않고
    **교환이 아예 안 불렸는지**까지 본다 — 순서를 뒤집는 변이는 상태 코드로는 안 잡힌다.
    """
    called: list[str] = []
    monkeypatch.setattr(oauth, "exchange_code", lambda code: called.append(code) or "t")
    if cookie:
        client.cookies.set(login_session.STATE_COOKIE_NAME, cookie)

    res = client.get(f"/auth/callback?code=abc&state={query}", follow_redirects=False)

    assert res.status_code == 400
    assert called == []


# ── 로그아웃 ─────────────────────────────────────────────────


def test_logout_deletes_the_row_not_just_the_cookie(client, on, users):
    """**행을 지운다.** 쿠키만 비우면 그 값이 다른 데 남아 있을 때 계속 통한다.

    DB 세션을 고른 이유가 이것이라, 여기서 지워지는지를 값으로 확인한다.
    """
    users.logins["login-id-1"] = {"id": 7, "login": "octocat", "avatar_url": None}
    client.cookies.set(login_session.COOKIE_NAME, "login-id-1")

    res = client.post("/auth/logout", follow_redirects=False)

    assert res.status_code == 303
    assert users.deleted == ["login-id-1"]


# ── 내 저장소 목록 ───────────────────────────────────────────

REPO_LIST = {
    "login": "octocat",
    "total": 1,
    "repos": [{
        "owner": "octocat", "name": "hello", "html_url": "https://github.com/octocat/hello",
        "description": None, "language": None, "size_kb": 12,
        "pushed_at": "2026-08-30T00:00:00Z", "stars": 0,
        "fork": False, "archived": False, "too_large": False,
    }],
}


@pytest.fixture
def github_lists(monkeypatch):
    """GitHub 호출 대역. 어느 login 으로 불렸는지 남긴다."""
    asked: list[str] = []

    def fake(login):
        asked.append(login)
        return REPO_LIST

    monkeypatch.setattr(github_client, "list_public_repos", fake)
    return asked


def test_repos_requires_a_signed_in_user(client, on, users, github_lists):
    """켜져 있어도 로그인 쿠키가 없으면 401 — GitHub 은 부르지 않는다."""
    res = client.get("/auth/repos")

    assert res.status_code == 401
    assert github_lists == []


@pytest.mark.parametrize("cookie", ["someone-elses-id", "not-a-uuid"])
def test_repos_rejects_unknown_or_expired_cookies(client, on, users, github_lists, cookie):
    client.cookies.set(login_session.COOKIE_NAME, cookie)

    assert client.get("/auth/repos").status_code == 401
    assert github_lists == []


def test_repos_lists_the_signed_in_users_public_repos(client, on, users, github_lists):
    """**저장된 `users.login` 으로 부른다** — 사용자 토큰은 어디에도 없다."""
    users.logins["login-id-1"] = {"id": 7, "login": "octocat", "avatar_url": None}
    client.cookies.set(login_session.COOKIE_NAME, "login-id-1")

    res = client.get("/auth/repos")

    assert res.status_code == 200
    assert res.json() == REPO_LIST
    assert github_lists == ["octocat"]


def test_repos_passes_github_errors_through(client, on, users, monkeypatch):
    users.logins["login-id-1"] = {"id": 7, "login": "octocat", "avatar_url": None}
    client.cookies.set(login_session.COOKIE_NAME, "login-id-1")

    def fail(login):
        raise github_client.RepoAccessError("한도 초과", 429)

    monkeypatch.setattr(github_client, "list_public_repos", fail)

    res = client.get("/auth/repos")

    assert res.status_code == 429
    assert res.json()["detail"] == "한도 초과"
