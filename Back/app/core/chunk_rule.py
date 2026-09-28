"""청킹 규칙 버전 — 지금 코드가 만드는 색인의 모양을 식별하는 해시.

**무엇까지 세는가.** 이름은 '청킹'이지만 실제로 재는 것은 **청크 경계와 그 청크를
벡터로 만드는 방식** 둘 다이다. 저장된 청크가 지금 검색에 쓸 만한지를 가르려면
둘 다 봐야 한다 — 경계가 같아도 벡터가 다르면 그 색인은 지금 질의와 다른 자로 잰
것이고, 그것이 fp32 를 int8 로 바꿀 때 실제로 재색인 대상을 잡아 준 신호였다.

**왜 필요한가.** 청킹 규칙을 고쳐도 이미 저장된 청크는 옛 규칙 그대로다. 다시
인덱싱되는 것은 새 스냅샷이 생길 때뿐이라(저장소가 갱신돼 pushed_at 이 바뀔 때),
규칙만 바꾼 경우에는 개선이 사용자에게 영영 닿지 않는다. 실제로 그런 상태였다 —
청킹을 여러 번 고치는 동안 서비스 인덱스는 처음 만든 그대로였고, 아무도 몰랐다.

어느 인덱스가 낡았는지 알려면 "무슨 규칙으로 만들었는지"를 청크와 함께 남겨야 한다.

**손으로 올리는 상수가 아니다.** 사람이 올리는 것을 잊으면 그 순간부터 거짓말이
되므로, 규칙을 이루는 상수와 함수 코드에서 값을 자동으로 뽑는다.
"""

import ast
import hashlib
import inspect
import textwrap

from app.config import EMBEDDING_MODEL, EMBEDDING_PASSAGE_PREFIX
from app.core import chunker
from app.core.embeddings import EMBED_BATCH_SIZE

# 이 표식이 붙은 인덱스는 규칙 기록이 생기기 전에 만들어진 것이다.
# 무슨 규칙이었는지 알 방법이 없으므로 "모른다"로 남기고 재색인 대상으로 본다.
LEGACY = "legacy"

# 청크 경계를 정하는 함수 전부. 하나라도 로직이 바뀌면 청크 모양이 달라진다.
_RULE_FUNCTIONS = (
    chunker._line_chunks,
    chunker._node_chunks,
    chunker._merge_small,
    chunker._split_by_tokens,
    chunker.chunk_file,
    chunker.chunk_files,
)


def _constants() -> list:
    """값이 바뀌면 청크 경계나 벡터가 바뀌는 것들.

    노드 목록(DEFINITION_NODES 등)도 넣는다 — 언어를 추가하거나 어떤 노드를
    정의로 볼지 바꾸면 그 언어의 청크가 통째로 달라진다. 집합은 순서가 없으므로
    정렬해서 넣는다(같은 내용이 실행마다 다른 해시를 내면 안 된다).

    임베딩 모델은 **토큰 한도를 통해** 청크 크기를 정한다(_split_by_tokens).
    한도를 직접 읽지 않고 모델 이름을 넣는 이유는 input_limit() 이 모델을 올려야
    답하기 때문이다 — 규칙 하나 확인하려고 2GB 를 로드할 수는 없다.
    문서 접두어도 토큰 수에 들어가므로 함께 넣는다.

    배치 크기는 청크 경계를 안 바꾼다. 그런데도 넣는 이유는 **같은 청크가 배치에 따라
    다른 벡터로 나오기 때문**이다(코사인 평균 0.993 — int8 과 fp32 의 차이만큼 벌어진다).
    이 값을 빼 두면 배치를 바꿔도 해시가 그대로라 낡음 경고가 안 뜨고, 옛 배치로 만든
    색인과 새 배치로 만든 색인이 겉으로 구분되지 않는다 — 배치를 다시 재려고 할 때
    **어느 쪽으로 만든 색인인지 모른 채 측정하게 된다.**
    """
    return [
        chunker.MAX_CHUNK_CHARS,
        chunker.MIN_CHUNK_CHARS,
        chunker.OVERLAP_LINES,
        chunker.TOKEN_LIMIT_MARGIN,
        chunker.MAX_SPLIT_PASSES,
        sorted(chunker.IMPORT_NODES),
        sorted(chunker.CONTAINER_NODES),
        sorted((lang, sorted(nodes)) for lang, nodes in chunker.DEFINITION_NODES.items()),
        EMBEDDING_MODEL,
        EMBEDDING_PASSAGE_PREFIX,
        EMBED_BATCH_SIZE,
    ]


def _normalized_source(func) -> str:
    """함수 코드를 AST 로 정규화한다 — 서식만 바꾼 편집에는 반응하지 않는다.

    주석(`#`)은 AST 에 남지 않으므로 그냥 사라지고, docstring 은 문자열 리터럴이라
    남으므로 지운다. 이 저장소는 "왜 이 값인가"를 주석과 docstring 으로 길게 남기는
    편이라, 그런 편집마다 모든 인덱스가 '재색인 필요'로 뜨면 이 표시를 아무도 믿지 않게 된다.

    **`ast.dump` 가 아니라 `ast.unparse` 를 쓴다. 판 사이에서 흔들렸기 때문이다.**
    `ast.dump` 는 3.13 에서 기본 출력이 바뀌었다(`show_empty` 가 생기고 기본이 False).
    그래서 같은 코드·같은 설정인데 **인터프리터 판만 달라도 해시가 갈렸다** — 로컬
    3.14 가 `822bb217`, 컨테이너 3.12 가 `7d06885c` 를 냈고, 양쪽이 서로를 영원히
    낡음으로 보는 상태였다. 청크 경계도 벡터도 안 바뀌는데 뜨는 경고라 위 문단이
    경계한 바로 그것이다.

    **`ast.unparse` 도 stdlib 렌더링이라 판이 바뀌면 또 흔들릴 수 있다.**
    그래서 `tests/test_chunk_rule.py` 가 표본 하나의 정규화 결과를 리터럴로 고정해
    둔다 — 렌더링이 바뀌면 배포에서 색인이 조용히 낡는 대신 그 테스트가 깨진다.
    """
    tree = ast.parse(textwrap.dedent(inspect.getsource(func)))
    for node in ast.walk(tree):
        body = getattr(node, "body", None)
        if not isinstance(body, list) or not body:
            continue
        first = body[0]
        if (
            isinstance(first, ast.Expr)
            and isinstance(first.value, ast.Constant)
            and isinstance(first.value.value, str)
        ):
            body.pop(0)
    return ast.unparse(tree)


def rule_version() -> str:
    """지금 청킹 규칙의 식별자 (16진수 8자).

    같은 규칙이면 항상 같은 값이고, 상수나 청킹 로직을 고치면 자동으로 달라진다.
    값 자체에 의미는 없다 — 저장된 값과 같은지만 본다.
    """
    material = [repr(_constants())]
    material += [_normalized_source(func) for func in _RULE_FUNCTIONS]
    digest = hashlib.sha256("\n".join(material).encode("utf-8")).hexdigest()
    return digest[:8]
