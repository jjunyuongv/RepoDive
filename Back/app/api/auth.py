"""GitHub OAuth 로그인.

**꺼짐이 기본이다.** `GITHUB_OAUTH_CLIENT_ID` 가 비면 `/auth/login`·`/auth/callback`·
`/auth/logout` 이 404 이고, `/auth/me` 는 `{enabled: false, user: null}` 로 답한다.
그 상태의 서비스 동작은 로그인 도입 전과 **완전히 같다** — `login_session.current_user`
가 쿠키를 아예 보지 않기 때문이다.

## 흐름

    GET /auth/login     state 를 만들어 쿠키에 넣고 GitHub 으로 302
    GET /auth/callback  쿠키의 state 와 조회 문자열의 state 를 대조 →
                        코드 교환 → GET /user → users.upsert → logins 행 →
                        로그인 쿠키를 심고 화면으로 302
    POST /auth/logout   logins 행을 지우고 쿠키를 비운다
    GET  /auth/me       지금 요청이 누구인가
    GET  /auth/repos    로그인한 사람의 공개 저장소 목록 (서버 토큰으로 공개 API 를 부른다)

## state 를 왜 쿠키에 두는가

CSRF 대비값은 "우리가 시작한 인가인가"를 증명해야 하므로 **서버가 낸 것임을 확인할
수 있는 자리**에 있어야 한다. 서명 토큰으로 만들면 비밀키가 하나 늘고, DB 에 넣으면
표가 하나 는다. 쿠키는 **우리 출처에만 심을 수 있으므로**(남의 사이트가 우리 도메인
쿠키를 못 만든다) 그 자체로 출처 증명이 되고, 대조는 문자열 비교 한 번이다.

`SameSite=Lax` 라서 GitHub 에서 돌아오는 **최상위 GET 이동**에는 실린다.
`Strict` 였다면 그 이동에 안 실려서 모든 로그인이 실패한다.
"""

import logging
import secrets

from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import RedirectResponse

from app.config import FRONTEND_ORIGIN, LOGIN_SESSION_DAYS
from app.db import users
from app.db.pool import DB_ERRORS
from app.schemas.schemas import AuthStatus, AuthUser, RepoList
from app.services import github_client, login_session, oauth

router = APIRouter(prefix="/auth")

logger = logging.getLogger(__name__)

DISABLED = "로그인 기능이 꺼져 있습니다."
DB_UNAVAILABLE = "로그인을 처리할 수 없습니다 (데이터베이스 연결 실패)."


def _require_enabled() -> None:
    """꺼져 있으면 404. **403 이 아니다** — 그 경로는 존재하지 않는 것으로 다룬다."""
    if not oauth.enabled():
        raise HTTPException(status_code=404, detail=DISABLED)


@router.get("/login")
def login():
    """GitHub 인가 화면으로 보낸다.

    **302 를 직접 돌려준다.** 화면이 `window.location` 으로 이동하는 대신 이 경로를
    링크로 걸면, 인가 흐름 전체가 최상위 이동이라 `SameSite=Lax` 쿠키가 성립한다.
    """
    _require_enabled()
    state = oauth.new_state()
    response = RedirectResponse(oauth.authorize_url(state), status_code=302)
    login_session.set_state_cookie(response, state)
    return response


@router.get("/callback")
def callback(request: Request, code: str = "", state: str = ""):
    """GitHub 이 돌려보낸 사용자를 받는다.

    **state 를 먼저 본다.** 코드 교환보다 앞이라야 남이 시작한 인가로 우리 서버가
    GitHub 을 부르는 일이 없다.
    """
    _require_enabled()

    expected = request.cookies.get(login_session.STATE_COOKIE_NAME) or ""
    # **상수 시간 비교를 쓴다.** 값이 짧고 한 번뿐이라 실익은 작지만, 대조 코드가
    # `==` 이면 나중에 이 자리를 흉내 낸 다른 비교가 생긴다.
    if not expected or not state or not secrets.compare_digest(expected, state):
        raise HTTPException(
            status_code=400, detail="로그인 요청이 유효하지 않습니다. 다시 시도해 주세요."
        )
    if not code:
        raise HTTPException(status_code=400, detail="GitHub 이 인가 코드를 주지 않았습니다.")

    try:
        token = oauth.exchange_code(code)
        profile = oauth.fetch_user(token)
    except oauth.OAuthError as e:
        raise HTTPException(status_code=e.status_code, detail=str(e))

    try:
        user_id = users.upsert(
            profile["github_user_id"], profile["login"], profile["avatar_url"]
        )
        login_id = users.create_login(user_id, LOGIN_SESSION_DAYS)
    except DB_ERRORS as e:
        logger.warning("로그인을 저장하지 못했습니다: %s", e)
        raise HTTPException(status_code=503, detail=DB_UNAVAILABLE)

    # 화면으로 돌려보낸다. 프론트가 /auth/me 로 상태를 다시 읽는다.
    response = RedirectResponse(FRONTEND_ORIGIN, status_code=302)
    login_session.set_login_cookie(response, login_id)
    login_session.clear_state_cookie(response)
    return response


@router.post("/logout")
def logout(request: Request):
    """로그아웃. **행을 지우므로 즉시 유효하다.**

    쿠키만 지우면 그 값이 다른 데 남아 있을 때 계속 통한다.
    """
    _require_enabled()
    login_id = request.cookies.get(login_session.COOKIE_NAME)
    if login_id:
        try:
            users.delete_login(login_id)
        except (DB_ERRORS, ValueError) as e:
            # 지우지 못해도 쿠키는 비운다 — 여기서 막으면 로그아웃이 안 되는 상태가 된다.
            logger.warning("로그인 행을 지우지 못했습니다: %s", e)

    response = RedirectResponse(FRONTEND_ORIGIN, status_code=303)
    login_session.clear_login_cookie(response)
    return response


@router.get("/me", response_model=AuthStatus)
def me(request: Request):
    """지금 요청이 누구인가. **꺼져 있어도 404 가 아니다** — 모듈 머리말 참고."""
    if not oauth.enabled():
        return AuthStatus(enabled=False)
    user = login_session.current_user(request)
    return AuthStatus(
        enabled=True,
        user=AuthUser(login=user["login"], avatar_url=user["avatar_url"]) if user else None,
    )


@router.get("/repos", response_model=RepoList)
def my_repos(request: Request):
    """로그인한 사람의 공개 저장소 목록.

    **`/auth` 아래에 둔다.** 새 최상위 경로를 만들면 `Front/vite.config.ts` 의
    `BACKEND_PATHS` 와 `Front/nginx.conf` 를 둘 다 고쳐야 하고, 갈리면 배포에서 404 가
    SPA 폴백에 먹혀 200 으로 보인다.

    401 을 쓴다 — 여기는 "남의 것"(`/chat` 의 404)이 아니라 "로그인 안 함"이다.
    """
    _require_enabled()
    user = login_session.current_user(request)
    if not user:
        raise HTTPException(status_code=401, detail="로그인이 필요합니다.")
    try:
        return github_client.list_public_repos(user["login"])
    except github_client.RepoAccessError as e:
        raise HTTPException(status_code=e.status_code, detail=str(e))
