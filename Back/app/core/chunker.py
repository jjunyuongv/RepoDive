"""소스 파일을 검색 단위(청크)로 자른다.

tree-sitter 구문 트리에서 함수·클래스 경계를 찾아 자르므로, 라인 수로 자를 때처럼
함수 한가운데가 끊기지 않는다. 파서가 없거나 파싱이 깨진 파일은 라인 기준으로 자른다
— 청킹 실패가 그 파일을 검색에서 통째로 지워버리면 안 된다.
"""

import logging

from app.core.languages import language_for

logger = logging.getLogger(__name__)

# 청크 하나의 목표 크기(문자).
#
# **작게 잡는다.** 넉넉히(2400) 잡고 토큰 한도만 _split_by_tokens() 로 지키는 안을
# 실측했는데, 저장소마다 방향이 갈렸다 — 작은 Java 저장소는 미세하게 좋아졌지만
# 큰 Python/JS 저장소에서 한국어 Recall@8 이 0.80 → 0.60 으로 무너졌다.
# 정답이 더 큰 덩어리에 섞여 초점이 흐려지는 것으로, 클래스를 통째로 한 청크로 두지
# 않는 것과 같은 이유다. 넓은 문맥이 유리한 것은 식별자 질의뿐이었다.
# (측정은 docs/evaluation.md §4)
#
# 이 값이 임베딩 한도(512토큰)를 **보장하지는 않는다.** 문자/토큰 비율이 실측에서
# 1.68 ~ 17.40 까지 10배 벌어져서(한국어 주석 덩어리 vs 압축된 코드), 800자로도
# 넘는 청크가 나온다. 한도는 _split_by_tokens() 가 토크나이저로 지킨다.
MAX_CHUNK_CHARS = 800
# 이보다 작은 조각은 앞뒤와 합친다. import 한 줄이 독립 청크가 되면 검색에 잡히기만 하고
# 답에 쓸 내용이 없다.
MIN_CHUNK_CHARS = 200

# 상한을 넘겨 쪼갤 때 다음 조각에 겹쳐 남길 줄 수. 경계에 걸친 코드(조건문 머리와 본문이
# 갈리는 식)가 어느 쪽에서도 온전히 읽히지 않는 것을 완화한다.
OVERLAP_LINES = 3

# import 계열 노드는 청크로 만들지 않는다. 검색에는 걸리지만 답에 쓸 내용이 없어
# 상위 k개 자리만 차지한다. 어떤 라이브러리를 쓰는지는 매니페스트(build.gradle 등)가
# 이미 요약 컨텍스트에 들어 있다.
IMPORT_NODES = frozenset({
    "import_declaration",      # java, go
    "import_statement",        # python, javascript, typescript
    "import_from_statement",   # python
    "use_declaration",         # rust
    "preproc_include",         # c, cpp
    "using_directive",         # csharp
    "package_declaration",     # java
    "package_clause",          # go
})

# 다른 정의를 담는 그릇(클래스·인터페이스·impl 블록 등).
# 크기가 작아도 **항상 내부 메서드로 내려간다.** 클래스 하나를 통째로 한 청크로 두면
# 그 벡터가 "이 클래스가 하는 모든 일"의 평균이 되어, 어떤 질문에도 어중간하게 걸린다
# (실측: 컨트롤러 클래스들이 질문과 무관하게 늘 상위에 올라왔다).
CONTAINER_NODES = frozenset({
    "class_declaration", "interface_declaration", "enum_declaration", "record_declaration",
    "class_definition", "class_specifier", "struct_declaration", "struct_specifier",
    "impl_item", "trait_item", "mod_item", "module", "class", "object_declaration",
    "namespace_definition", "protocol_declaration", "object_definition", "trait_definition",
})

# 언어별 "정의"에 해당하는 노드. 이 경계에서 자른다.
# 여기 없는 언어는 라인 기준 폴백으로 처리된다 (css, yaml, sql 등 정의 개념이 옅은 것 포함).
DEFINITION_NODES = {
    "python": {"function_definition", "class_definition", "decorated_definition"},
    "java": {
        "class_declaration", "interface_declaration", "enum_declaration",
        "record_declaration", "method_declaration", "constructor_declaration",
    },
    "javascript": {
        "function_declaration", "class_declaration", "method_definition",
        "lexical_declaration", "export_statement",
    },
    "typescript": {
        "function_declaration", "class_declaration", "method_definition",
        "interface_declaration", "type_alias_declaration", "lexical_declaration",
        "export_statement",
    },
    "tsx": {
        "function_declaration", "class_declaration", "method_definition",
        "interface_declaration", "type_alias_declaration", "lexical_declaration",
        "export_statement",
    },
    "go": {"function_declaration", "method_declaration", "type_declaration"},
    "rust": {"function_item", "impl_item", "struct_item", "trait_item", "mod_item"},
    "ruby": {"method", "class", "module"},
    "php": {"function_definition", "class_declaration", "method_declaration"},
    "c": {"function_definition", "struct_specifier", "enum_specifier"},
    "cpp": {
        "function_definition", "class_specifier", "struct_specifier", "namespace_definition",
    },
    "csharp": {
        "class_declaration", "interface_declaration", "struct_declaration",
        "method_declaration", "constructor_declaration", "record_declaration",
    },
    "kotlin": {"function_declaration", "class_declaration", "object_declaration"},
    "swift": {"function_declaration", "class_declaration", "protocol_declaration"},
    "scala": {"function_definition", "class_definition", "object_definition", "trait_definition"},
    "bash": {"function_definition"},
    "lua": {"function_declaration"},
    "elixir": {"call"},
    "haskell": {"function", "data_type"},
    "dart": {"class_definition", "function_signature", "method_signature"},
}


def _line_chunks(
    path: str,
    language: str,
    content: str,
    start_offset: int = 0,
    max_chars: int | None = None,
) -> list[dict]:
    """라인 기준 폴백. 목표 크기를 넘지 않게 줄 단위로 묶는다.

    max_chars 는 토큰 한도를 넘긴 청크를 더 잘게 다시 자를 때 쓴다 (_split_by_tokens).
    """
    limit = max_chars or MAX_CHUNK_CHARS
    chunks: list[dict] = []
    lines = content.splitlines()
    buffer: list[str] = []
    buffer_start = 0

    def flush(end_index: int) -> None:
        if not buffer:
            return
        text = "\n".join(buffer)
        if text.strip():
            chunks.append({
                "path": path,
                "language": language,
                "start_line": start_offset + buffer_start + 1,
                "end_line": start_offset + end_index + 1,
                "content": text,
            })

    for i, line in enumerate(lines):
        # 한 줄만으로 이미 목표를 넘으면 그 줄은 혼자 청크가 된다 (더 쪼갤 기준이 없다).
        if buffer and sum(len(x) + 1 for x in buffer) + len(line) > limit:
            flush(i - 1)
            # 끝 몇 줄을 다음 조각에 겹쳐 남긴다. 겹침이 상한의 절반을 넘으면 남기지
            # 않는다 — 그러지 않으면 조각이 겹침으로만 채워져 진도가 나가지 않는다.
            keep = buffer[-OVERLAP_LINES:]
            if sum(len(x) + 1 for x in keep) > limit // 2:
                keep = []
            buffer, buffer_start = list(keep), i - len(keep)
        buffer.append(line)

    flush(len(lines) - 1)
    return chunks


def _node_chunks(node, source: bytes, path: str, language: str, definitions: set) -> list[dict]:
    """구문 노드를 청크로. 목표보다 크면 내부 정의로 한 단계 내려간다.

    작은 클래스는 통째로 한 청크가 되고, 큰 클래스는 메서드별로 쪼개진다 —
    "이 클래스가 무엇인가"와 "이 메서드가 무엇을 하는가" 둘 다 검색되게 하려는 것이다.
    """
    text = source[node.start_byte:node.end_byte].decode("utf-8", errors="replace")

    def whole() -> list[dict]:
        return [{
            "path": path,
            "language": language,
            "start_line": node.start_point[0] + 1,
            "end_line": node.end_point[0] + 1,
            "content": text,
        }]

    # 내부 정의는 한 단계 아래(클래스 본문)에 있다. 본문 노드를 건너뛰고 찾는다.
    inner = [c for c in node.children if c.type in definitions]
    if not inner:
        for child in node.children:
            inner = [c for c in child.children if c.type in definitions]
            if inner:
                break

    is_container = node.type in CONTAINER_NODES
    if not inner:
        if len(text) <= MAX_CHUNK_CHARS:
            return whole()
        return _line_chunks(path, language, text, start_offset=node.start_point[0])
    if not is_container and len(text) <= MAX_CHUNK_CHARS:
        return whole()

    chunks: list[dict] = []
    # 클래스 선언부와 필드는 메서드 어디에도 없으므로 따로 남긴다 —
    # "이 클래스가 무엇인가"와 "어떤 필드를 갖는가"도 검색 대상이다.
    #
    # **짧다고 버리지 않는다.** 전에는 MIN_CHUNK_CHARS 미만이거나 주석+선언만 있는 헤더를
    # 노이즈로 보고 버렸는데, 그러면 `public class X extends SomeBase` 처럼 짧은 선언이
    # 인덱스에서 통째로 사라진다 — "무엇을 상속하는가"를 묻는 질의는 순위가 낮았던 게
    # 아니라 **정답 자체가 없었다.** 클래스가 무엇을 상속하는지는 그 파일에서 그 한 줄에만 있다.
    # (측정: docs/evaluation.md §4 청킹 수정 2a)
    # 작은 헤더는 _merge_small() 이 뒤따르는 첫 메서드에 붙인다.
    header_end = inner[0].start_byte
    header = source[node.start_byte:header_end].decode("utf-8", errors="replace")
    if header.strip():
        chunks.append({
            "path": path,
            "language": language,
            "start_line": node.start_point[0] + 1,
            "end_line": inner[0].start_point[0],
            "content": header,
        })

    for child in inner:
        chunks.extend(_node_chunks(child, source, path, language, definitions))
    return chunks


def _merge_small(chunks: list[dict]) -> list[dict]:
    """붙어 있는 작은 청크를 이웃 **하나**에만 합친다. 경계를 넘어서 합치지는 않는다.

    연쇄 병합을 막는 이유: 작은 조각이 여러 개 이어지면 그것들이 계속 붙어 서로 다른
    메서드가 한 청크에 뭉친다. 그러면 그 벡터가 "이 메서드들이 하는 일"의 평균이 되어
    어떤 질문에도 어중간하게 걸린다 — 클래스를 통째로 두지 않는 것과 같은 이유다.
    (짧은 클래스 헤더를 버리지 않게 바꾼 뒤 실제로 메서드 둘이 뭉쳤다.)
    """
    merged: list[dict] = []
    absorbed: list[bool] = []  # 이미 뭔가를 흡수한 청크인지
    for chunk in chunks:
        if (
            merged
            and not absorbed[-1]
            and len(merged[-1]["content"]) < MIN_CHUNK_CHARS
            and len(merged[-1]["content"]) + len(chunk["content"]) <= MAX_CHUNK_CHARS
        ):
            previous = merged[-1]
            previous["content"] += "\n" + chunk["content"]
            previous["end_line"] = chunk["end_line"]
            absorbed[-1] = True
        else:
            merged.append(chunk)
            absorbed.append(False)
    # 병합에도 붙지 못한 부스러기(주석 한 줄, 닫는 괄호)는 버린다.
    # 임베딩 비용과 인덱스 자리만 쓰고 답의 근거는 되지 못한다.
    return [c for c in merged if len(c["content"].strip()) >= 40]


def chunk_file(path: str, content: str) -> list[dict]:
    """파일 하나를 청크 목록으로. 각 항목은 code_chunks 테이블의 열과 같은 모양이다."""
    language = language_for(path)
    if language is None:
        return []
    if not content.strip():
        return []

    definitions = DEFINITION_NODES.get(language)
    if not definitions:
        return _merge_small(_line_chunks(path, language, content))

    try:
        from tree_sitter_language_pack import get_parser

        parser = get_parser(language)
        source = content.encode("utf-8")
        tree = parser.parse(source)
    except Exception as e:
        # 파서가 없거나 문법이 깨진 파일. 검색에서 빠지는 것보다 거칠게라도 넣는 게 낫다.
        logger.warning("%s 파싱 실패(%s) — 라인 기준으로 자릅니다: %s", path, language, e)
        return _merge_small(_line_chunks(path, language, content))

    chunks: list[dict] = []
    for child in tree.root_node.children:
        if child.type in IMPORT_NODES:
            continue
        if child.type in definitions:
            chunks.extend(_node_chunks(child, source, path, language, definitions))
        else:
            # 정의가 아닌 최상위 요소(import, 상수, 설정 블록 등)도 남긴다.
            #
            # **여기에도 상한을 건다.** 예전에는 크기를 재지 않고 통째로 넣었는데,
            # Java 저장소에서는 그런 요소가 거의 없어 드러나지 않다가 Python/JS 저장소에서
            # 터졌다 — `document.addEventListener(...)` 한 덩어리(4,115자), 긴 프롬프트
            # 상수(3,213자), `if __name__ == "__main__":` 블록이 그대로 들어가
            # 청크 11개가 입력 한도에서 잘렸다.
            text = source[child.start_byte:child.end_byte].decode("utf-8", errors="replace")
            if not text.strip():
                continue
            if len(text) <= MAX_CHUNK_CHARS:
                chunks.append({
                    "path": path,
                    "language": language,
                    "start_line": child.start_point[0] + 1,
                    "end_line": child.end_point[0] + 1,
                    "content": text,
                })
            else:
                chunks.extend(
                    _line_chunks(path, language, text, start_offset=child.start_point[0])
                )

    if not chunks:
        return _merge_small(_line_chunks(path, language, content))
    return _merge_small(chunks)


# 토큰 한도에서 남겨 둘 여유. 접두어("passage: ")와 특수토큰([CLS]/[SEP]) 몫이다.
TOKEN_LIMIT_MARGIN = 12

# 재분할 반복 상한. 목표 문자 수는 비율로 추정하므로 한 번에 안 맞을 수 있다.
MAX_SPLIT_PASSES = 3


def _split_by_tokens(chunks: list[dict], count_tokens, token_limit: int) -> list[dict]:
    """토큰 한도를 넘는 청크만 더 잘게 다시 자른다.

    **문자 상한으로는 이 일을 할 수 없다.** 문자/토큰 비율이 실측에서 1.68 ~ 17.40 까지
    벌어져서(한국어 주석 덩어리 vs 압축된 코드), MAX_CHUNK_CHARS 를 얼마로 잡든 넘는
    청크가 나온다. 한국어 비중이 높은 청크는 800자로도 512토큰을 넘겼다.

    목표 문자 수는 그 청크의 실측 비율로 정한다(안전계수 0.9). 한 번에 못 맞으면 몇 번
    더 돈다. 토크나이저에 닿지 못하면(count_tokens 가 None) 문자 상한만 믿고 지나간다.

    **count_tokens 는 실제 토큰 수여야 한다.** 잘린 값(한도에서 끊긴 512)을 주면
    "얼마나 넘었는지"를 알 수 없어 축소율이 0.879 로 고정되고, 몇 패스를 돌려도
    한도 안으로 못 들어온다 — embeddings.count_tokens 가 truncation 을 끈 사본을
    쓰는 이유가 이것이다.
    """
    target = token_limit - TOKEN_LIMIT_MARGIN
    result = chunks
    for _ in range(MAX_SPLIT_PASSES):
        counts = count_tokens([c["content"] for c in result])
        if counts is None:
            return result
        over = {i: n for i, n in enumerate(counts) if n > target}
        if not over:
            return result

        split: list[dict] = []
        for i, chunk in enumerate(result):
            tokens = over.get(i)
            if tokens is None:
                split.append(chunk)
                continue
            max_chars = max(200, int(len(chunk["content"]) * (target / tokens) * 0.9))
            split.extend(_line_chunks(
                chunk["path"], chunk["language"], chunk["content"],
                start_offset=chunk["start_line"] - 1, max_chars=max_chars,
            ))
        # 재분할이 만든 부스러기는 버린다 (_merge_small 과 같은 기준).
        result = [c for c in split if len(c["content"].strip()) >= 40]
    return result


def chunk_files(
    files: dict[str, str], count_tokens=None, token_limit: int | None = None
) -> list[dict]:
    """{경로: 내용} 전체를 청크 목록으로 편다.

    count_tokens·token_limit 을 주면 토큰 한도를 넘는 청크를 다시 자른다.
    **주입으로 받는 이유**: 여기서 임베딩 모델을 직접 부르면 청킹 테스트가 2GB 모델을
    내려받아 올려야 한다. 호출부(indexer)가 embeddings 의 함수를 넘긴다.
    """
    chunks: list[dict] = []
    for path, content in files.items():
        chunks.extend(chunk_file(path, content))
    if count_tokens and token_limit:
        chunks = _split_by_tokens(chunks, count_tokens, token_limit)
    return chunks
