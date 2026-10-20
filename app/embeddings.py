from threading import Lock

import numpy as np

from app.config import Settings


class EmbeddingError(RuntimeError):
    pass


class Embedder:
    """첫 업로드 또는 검색에서 모델을 로드합니다. 문서와 질문에 동일하게 사용합니다."""

    def __init__(self, settings: Settings):
        self.settings = settings
        self._model = None
        self._lock = Lock()

    def encode(self, texts: list[str]) -> np.ndarray:
        try:
            with self._lock:
                if self._model is None:
                    from sentence_transformers import SentenceTransformer

                    self._model = SentenceTransformer(
                        self.settings.embedding_model,
                        device=self.settings.embedding_device,
                        cache_folder=str(self.settings.embedding_cache_dir),
                        trust_remote_code=False,
                    )
                vectors = np.asarray(self._model.encode(
                    texts, batch_size=8, normalize_embeddings=True,
                    convert_to_numpy=True, show_progress_bar=False,
                ), dtype=np.float32)
            if vectors.shape != (len(texts), 1024) or not np.isfinite(vectors).all():
                raise ValueError("bge-m3 임베딩은 유한한 1024차원 벡터여야 합니다.")
            norms = np.linalg.norm(vectors, axis=1, keepdims=True)
            if np.any(norms == 0) or not np.isfinite(norms).all():
                raise ValueError("임베딩 벡터의 크기가 유효하지 않습니다.")
            return vectors / norms
        except Exception as exc:
            raise EmbeddingError("bge-m3 모델 로드 또는 임베딩 생성에 실패했습니다.") from exc
