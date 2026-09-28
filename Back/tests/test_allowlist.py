"""허용 목록의 파싱과 판정.

**대역 데이터는 두 동작이 다른 결과를 내도록 짰다.** 이 파일에서 그것이
뜻하는 바는 넷이다 —

- **항목을 둘 이상 둔다.** 하나면 "목록 전체를 본다"와 "첫 항목만 본다"가 같은 결과라
  후자로 바뀌어도 통과한다. 그래서 **두 번째 항목의 허용도 따로 확인한다**
- **허용과 차단을 둘 다 둔다.** 한쪽만이면 판정을 통째로 뒤집어도 통과한다
- **대문자는 설정 쪽에 넣는다.** 입력은 `parse_github_url` 이 이미 소문자로 주므로
  (github_client.py), 입력만 대문자로 쓰면 정규화가 죽어도 통과한다.
  실제로 틀리는 방향은 **사람이 `.env` 에 대문자로 적는 것**이다
- **꺼진 상태와 켜진 상태를 함께 확인한다.** 빈 문자열이 "전부 허용"인지가 이 기능의
  기본값이라, 여기가 뒤집히면 로컬 개발이 통째로 막히거나 배포가 통째로 열린다

**로그인 상태를 명시적으로 고정한다.** `conftest.py` 는 환경변수를 고립시키지 않아서,
`Back/.env` 에 `GITHUB_OAUTH_CLIENT_ID` 가 있는 기계에서는 `oauth.enabled()` 가 참이고
없는 기계에서는 거짓이다. 문구가 그 값으로 갈리므로, 안 고정하면 **기계마다 다른 갈래를
재게 된다.**
"""

import pytest

from app.services import allowlist, oauth

# 설정 쪽 대문자(`Alpha/One`)와 소문자(`beta/two`)를 섞는다. 둘 다 소문자 키로 맞아야 한다.
RAW = "Alpha/One, beta/two"


@pytest.fixture
def allowed(monkeypatch):
    """목록을 켠 상태. ALLOWED 는 모듈을 읽을 때 한 번 만들어지므로 그것을 갈아 끼운다."""
    monkeypatch.setattr(allowlist, "ALLOWED", allowlist._parse(RAW))


@pytest.fixture
def login_off(monkeypatch):
    """로그인이 꺼진 상태. `oauth.enabled()` 가 모듈 상수를 매 호출 읽어서 이것으로 듣는다."""
    monkeypatch.setattr(oauth, "GITHUB_OAUTH_CLIENT_ID", "")


@pytest.fixture
def login_on(monkeypatch):
    monkeypatch.setattr(oauth, "GITHUB_OAUTH_CLIENT_ID", "test-client-id")


# --- 파싱 -------------------------------------------------------------------

def test_설정의_대문자를_소문자로_접는다():
    # GitHub 은 저장소 이름의 대소문자를 구분하지 않아, 대문자로 적은 사람은
    # 틀렸다는 것도 모른 채 목록이 영영 안 맞는다.
    assert allowlist._parse(RAW) == ("alpha/one", "beta/two")


def test_공백과_빈_항목과_끝_슬래시를_버린다():
    assert allowlist._parse(" alpha/one/ ,, beta/two , ") == ("alpha/one", "beta/two")


def test_중복은_한_번만_남고_적힌_순서를_지킨다():
    # 순서는 차단 메시지에 그대로 실린다 — 정렬하면 설정과 화면이 다르게 보인다.
    assert allowlist._parse("beta/two, alpha/one, BETA/TWO") == ("beta/two", "alpha/one")


def test_빈_문자열은_빈_목록이다():
    assert allowlist._parse("") == ()
    assert allowlist._parse("  , ") == ()


# --- 켜고 끄기 ---------------------------------------------------------------

def test_목록이_비면_꺼진다(monkeypatch):
    monkeypatch.setattr(allowlist, "ALLOWED", ())
    assert allowlist.enabled() is False
    # 꺼진 상태에서는 무엇을 넣어도 통과한다 — 로컬 개발이 여기에 기댄다.
    allowlist.check("gamma", "three")


def test_목록이_있으면_켜진다(allowed):
    assert allowlist.enabled() is True


# --- 판정 -------------------------------------------------------------------

def test_목록의_첫_항목을_허용한다(allowed):
    allowlist.check("alpha", "one")


def test_목록의_두_번째_항목도_허용한다(allowed):
    # 이 테스트가 없으면 "첫 항목만 본다"로 바뀌어도 위 테스트가 통과한다.
    allowlist.check("beta", "two")


def test_목록에_없으면_거절한다(allowed):
    with pytest.raises(allowlist.RepoNotAllowed) as exc:
        allowlist.check("gamma", "three")
    assert exc.value.status_code == 403


def test_이름만_같고_소유자가_다르면_거절한다(allowed):
    # `owner/name` 전체로 비교하는지 본다. 이름만 보면 통과해 버린다.
    with pytest.raises(allowlist.RepoNotAllowed):
        allowlist.check("gamma", "one")


def test_거절_메시지에_목록_전체가_실린다(allowed, login_off):
    with pytest.raises(allowlist.RepoNotAllowed) as exc:
        allowlist.check("gamma", "three")
    message = str(exc.value)
    # 화면은 이 문자열을 그대로 띄운다(Front/src/App.tsx). 둘 다 있어야 한다 —
    # 하나만 확인하면 목록을 잘라 보여줘도 통과한다.
    assert "alpha/one" in message
    assert "beta/two" in message


# --- 로그인 조건 -------------------------------------------------------------
#
# 상태가 셋이다 — 로그인 꺼짐 / 켜짐+익명 / 켜짐+로그인. `user_id is None` 하나로는
# 앞의 둘이 뭉개지는데 그 둘은 서로 **다른 문구**를 내야 한다.


def test_로그인하면_목록_밖_저장소도_통과한다(allowed, login_on):
    allowlist.check("gamma", "three", user_id=7)


def test_로그인해도_목록_안_저장소는_그대로_통과한다(allowed, login_on):
    # "로그인이면 판정을 뒤집는다"로 잘못 짜도 위 테스트는 통과한다. 이쪽이 그것을 가른다.
    allowlist.check("alpha", "one", user_id=7)


def test_로그인이_켜져_있고_익명이면_로그인_안내를_받는다(allowed, login_on):
    with pytest.raises(allowlist.RepoNotAllowed) as exc:
        allowlist.check("gamma", "three")
    message = str(exc.value)
    assert "로그인" in message
    # 이 갈래도 목록을 실어야 한다 — 무엇을 넣을 수 있는지가 응답의 본론이다.
    assert "alpha/one" in message
    assert "beta/two" in message
    # **비공개까지 열린다고 읽히면 안 된다.** 저장소는 서버 토큰으로 읽는다.
    assert "비공개" in message


def test_로그인이_꺼져_있으면_문구가_도입_전_그대로다(allowed, login_off):
    """**이것이 이 기능의 계약이다.** 부분 일치로 재면 문구가 슬며시 바뀌어도 통과한다."""
    with pytest.raises(allowlist.RepoNotAllowed) as exc:
        allowlist.check("gamma", "three")
    assert str(exc.value) == (
        "이 서비스는 공개 데모라 지정된 저장소만 분석합니다. "
        "사용할 수 있는 저장소: alpha/one, beta/two"
    )
    # 누를 버튼이 화면에 없는데 로그인하라고 하면 안 된다.
    assert "로그인" not in str(exc.value)
