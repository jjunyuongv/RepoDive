"""코드 청크와 질문을 벡터로 바꾼다.

로컬 ONNX 모델(fastembed)이라 API 호출도 과금도 없다. 대신 최초 1회 모델 가중치를
내려받아야 하고(수백 MB), 첫 호출에서 로딩 시간이 든다.

모델은 **지연 로드**한다 — import 시점에 올리면 모델을 쓰지 않는 요청과 테스트까지
로딩 비용을 물게 된다 (db/pool.py 가 풀을 지연 생성하는 것과 같은 이유).
"""

import logging
import threading

from app.config import (
    EMBEDDING_CACHE_DIR,
    EMBEDDING_DIM,
    EMBEDDING_MODEL,
    EMBEDDING_MODEL_FILE,
    EMBEDDING_PASSAGE_PREFIX,
    EMBEDDING_QUERY_PREFIX,
    EMBEDDING_SOURCE_REPO,
)

logger = logging.getLogger(__name__)

_model = None
_lock = threading.Lock()

# 한 번에 넘길 청크 수. fastembed 기본값(256)보다 작게 잡는다 — 배치가 너무 크면
# 짧은 청크까지 가장 긴 청크 길이에 맞춰 패딩되어 계산이 낭비된다.
#
# 값은 모델마다 다르다. 8 은 jina-code(768) 시절에 정한 값이었고, e5-large(1024)로
# 바꾼 뒤 실측(8코어 CPU, 실제 저장소 청크 200개, 평균 982자)에서는 32 가 가장 빨랐다:
#   8: 2461ms/청크 · 16: 1616 · **32: 1032** · 64: 1173
# threads 를 코어 수로 명시하는 것은 오히려 느렸다(onnxruntime 기본값 0 = 자동이 낫다).
#
# **1 로 내렸다가 되돌렸다(2026-08-20). 그리고 이 기기로는 답을 못 냈다.**
# 마이크로벤치는 1 이 32 보다 2.2배 빠르다고 했지만, 실제 재색인(4,365청크) 세 번에서
# 결론이 세 번 뒤집혔다. 원인은 배치가 아니라 **CPU 점유**였다 —
#   #100 배치32 CPU 7.7x → 591.6초 · #101 배치1 CPU 6.7x → 1,105.7초 · #102 배치32 CPU 6.6x → 1,071.0초
# 같은 배치인데 점유 14% 차이가 시간 1.84배를 만들고, 같은 점유에서 배치 32 와 1 의
# 차이는 3.5%(노이즈)였다. 재려는 효과보다 통제 못 하는 변수가 크다.
#
# **그래서 32 는 '측정으로 이긴 값'이 아니라 '검증된 채로 지킨 값'이다.**
# 바꾸려면 배포 기기에서 재거나 CPU 상태를 고정할 것.
# 그리고 **배치를 바꾸면 벡터도 달라진다**(같은 청크 코사인 평균 0.993, 최소 0.977 —
# int8↔fp32 차이와 같은 크기) → 재색인과 검색 품질 재측정이 따라온다. 경위는 docs/evaluation.md §4.
EMBED_BATCH_SIZE = 32

# 진행률을 몇 청크마다 보고할지. **배치 크기와 묶지 않는다** — 보고 한 번이 DB 쓰기
# 한 번이라(indexer._report → index_status.advance), 배치에 묶어 두면 배치를 줄이는
# 순간 DB 쓰기가 같은 비율로 늘어난다 (배치 32→1 이면 4,365청크 저장소에서 136회 → 4,365회).
PROGRESS_EVERY = 32


def _register_if_custom(model_name: str) -> None:
    """fastembed 가 모르는 모델이면 등록한다. 아는 모델이면 아무것도 하지 않는다.

    fastembed 0.8.0 의 내장 목록에 든 다국어 모델은 다섯인데, 그중 검색에 쓸 수 있는
    것은 e5-large(2,132MB) 하나뿐이다 (`paraphrase-multilingual-*` 은 STS 모델,
    `jina-v2-base-de/code` 는 한국어가 아니다). **더 작은 다국어 모델은 목록 밖에 있다.**

    아래 두 값은 짐작이 아니라 모델 저장소에서 확인한 것이다 —
    `1_Pooling/config.json` 의 `pooling_mode_mean_tokens: true`, 그리고 E5 가
    코사인 유사도를 전제로 학습돼 정규화된 벡터를 쓴다는 점.
    **E5 계열 기준이므로 다른 계열을 넣을 때는 그 모델의 pooling 을 다시 확인할 것.**

    가중치를 **어디서 어떤 파일로** 받을지는 `EMBEDDING_SOURCE_REPO`/`EMBEDDING_MODEL_FILE`
    이 정한다. 같은 저장소의 양자화 파일처럼 이름만 우리가 따로 붙이는 경우가 있어서다.
    """
    from fastembed import TextEmbedding
    from fastembed.common.model_description import ModelSource, PoolingType

    if any(m["model"] == model_name for m in TextEmbedding.list_supported_models()):
        return

    logger.info(
        "fastembed 목록에 없는 모델이라 등록합니다: %s (%s / %s)",
        model_name, EMBEDDING_SOURCE_REPO, EMBEDDING_MODEL_FILE,
    )
    TextEmbedding.add_custom_model(
        model=model_name,
        pooling=PoolingType.MEAN,
        normalization=True,
        sources=ModelSource(hf=EMBEDDING_SOURCE_REPO),
        dim=EMBEDDING_DIM,
        model_file=EMBEDDING_MODEL_FILE,
    )


def _get_model():
    """모델 싱글턴. 여러 요청이 동시에 처음 부를 때 두 번 로드되지 않게 잠근다."""
    global _model
    if _model is None:
        with _lock:
            if _model is None:
                from fastembed import TextEmbedding

                logger.info("임베딩 모델 로드 중: %s", EMBEDDING_MODEL)
                _register_if_custom(EMBEDDING_MODEL)
                _model = TextEmbedding(
                    model_name=EMBEDDING_MODEL, cache_dir=EMBEDDING_CACHE_DIR
                )
                logger.info("임베딩 모델 준비 완료")
    return _model


def document_text(text: str) -> str:
    """인덱싱할 텍스트에 문서 접두어를 붙인다."""
    return EMBEDDING_PASSAGE_PREFIX + text


def query_text(text: str) -> str:
    """검색할 질문에 질의 접두어를 붙인다."""
    return EMBEDDING_QUERY_PREFIX + text


def embed_documents(texts: list[str], on_progress=None) -> list[list[float]]:
    """인덱싱할 청크들을 벡터로. 입력 순서와 같은 순서로 돌려준다.

    on_progress 를 주면 PROGRESS_EVERY 개마다 지금까지 처리한 개수로 부른다.
    큰 저장소는 임베딩만 수십 분이 걸려서(4,365청크 ≈ 10분), 진행 상황을
    화면에 보여주려면 중간 보고가 필요하다.
    """
    if not texts:
        return []
    prefixed = [document_text(t) for t in texts]
    vectors = []
    for vector in _get_model().embed(prefixed, batch_size=EMBED_BATCH_SIZE):
        vectors.append(vector.tolist())
        if on_progress and len(vectors) % PROGRESS_EVERY == 0:
            on_progress(len(vectors))
    if on_progress:
        on_progress(len(vectors))
    return vectors


def embed_query(text: str) -> list[float]:
    """검색할 질문을 벡터로.

    query_embed() 가 아니라 embed() 를 쓴다 — fastembed 의 query_embed() 는
    접두어를 붙여주지 않고 embed() 를 그대로 호출할 뿐이라(text_embedding_base.py),
    E5 처럼 접두어가 필수인 모델에서는 아무 도움이 안 된다. 접두어는 우리가 붙인다.
    """
    return next(iter(_get_model().embed([query_text(text)]))).tolist()


def warm_up() -> None:
    """모델을 미리 올려 첫 요청의 지연을 없앤다. 실패해도 예외를 올리지 않는다."""
    try:
        _get_model()
    except Exception as e:
        # 모델을 못 올려도 서비스는 살아 있어야 한다 — 요약과 대화는 임베딩 없이도 된다.
        logger.warning("임베딩 모델을 미리 올리지 못했습니다: %s", e)


def dimension() -> int:
    """schema.sql 의 vector(N) 과 맞춰야 하는 차원 수."""
    return EMBEDDING_DIM


def input_limit() -> int | None:
    """모델의 입력 토큰 한도. 못 읽으면 None.

    모델마다 다르다 (jina-code 8192, e5-large 512). 모델 이름으로 짐작하지 않고
    토크나이저 설정에서 직접 읽는다.
    """
    try:
        return _get_model().model.tokenizer.truncation["max_length"]
    except Exception as e:
        logger.debug("입력 한도를 읽지 못했습니다: %s", e)
        return None


_counter = None
_counter_lock = threading.Lock()


def _counting_tokenizer():
    """토큰 수를 재기 위한 토크나이저 사본. **truncation 을 끈다.**

    임베딩용 토크나이저에는 truncation 이 걸려 있어 한도를 넘는 텍스트도 딱 한도만큼
    (512)으로 돌려준다. 그 값으로는 "넘었다"는 사실만 알고 **얼마나 넘었는지를 모른다.**
    청크를 얼마나 줄여야 하는지는 그 차이로 계산하므로(chunker._split_by_tokens),
    잘린 값을 쓰면 매번 12% 씩만 줄어 몇 번을 돌려도 한도 안으로 못 들어온다
    (실측: 628토큰 청크가 3패스 뒤에도 628토큰이었다).

    원본의 truncation 을 끄면 한도를 넘는 입력이 그대로 모델에 들어가므로
    **사본을 만들어** 거기서만 끈다.
    """
    global _counter
    if _counter is None:
        with _counter_lock:
            if _counter is None:
                import copy

                tokenizer = copy.deepcopy(_get_model().model.tokenizer)
                tokenizer.no_truncation()
                _counter = tokenizer
    return _counter


def count_tokens(texts: list[str]) -> list[int] | None:
    """각 텍스트의 실제 토큰 수(잘리기 전). 토크나이저에 닿지 못하면 None.

    문서 접두어를 붙인 상태로 센다 — 실제로 모델에 들어가는 것이 그것이다.
    한도에 걸리는 청크를 셀 때는 `>= input_limit()` 으로 센다(딱 한도인 것도 잘린다).

    fastembed 내부 구조에 의존하므로 실패는 조용히 넘긴다(측정 보조 기능일 뿐이다).
    """
    if not texts:
        return []
    try:
        tokenizer = _counting_tokenizer()
        return [len(tokenizer.encode(document_text(t)).ids) for t in texts]
    except Exception as e:
        logger.debug("토큰 수를 세지 못했습니다: %s", e)
        return None
