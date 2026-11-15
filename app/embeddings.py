"""Local text embeddings (ONNX via fastembed). No external API is called."""

import logging
import threading

import anyio
import numpy as np

log = logging.getLogger(__name__)


class Embedder:
    """Loads the model once and embeds text in a worker thread."""

    def __init__(self, model_name: str, dim: int, enabled: bool, cache_dir: str | None, threads: int = 2) -> None:
        self.model_name = model_name
        self.dim = dim
        self.enabled = enabled
        self._cache_dir = cache_dir
        self._threads = threads
        self._model = None
        self._lock = threading.Lock()

    @property
    def ready(self) -> bool:
        """True once the model is loaded (always False when embeddings are disabled)."""
        return self._model is not None

    def load(self) -> None:
        """Load the model and check that its vector size matches the database column."""
        if not self.enabled or self._model is not None:
            return
        from fastembed import TextEmbedding

        with self._lock:
            model = TextEmbedding(self.model_name, cache_dir=self._cache_dir, threads=self._threads)
            probe = next(iter(model.embed(["dimension check"])))
            if len(probe) != self.dim:
                raise RuntimeError(f"{self.model_name} gives {len(probe)} dims, schema expects {self.dim}")
            self._model = model
        log.info("embedding model loaded: %s", self.model_name)

    def _embed_sync(self, texts: list[str]) -> list[np.ndarray]:
        """Embed in the current thread (the model is not re-entrant, hence the lock)."""
        with self._lock:
            return [np.asarray(v, dtype=np.float32) for v in self._model.embed(texts, batch_size=16)]

    async def embed(self, texts: list[str]) -> list[np.ndarray] | None:
        """Embed texts off the event loop; returns None when embeddings are off or not loaded."""
        if not self.ready or not texts:
            return None
        return await anyio.to_thread.run_sync(self._embed_sync, texts)

    async def embed_one(self, text: str) -> np.ndarray | None:
        """Embed a single text."""
        vectors = await self.embed([text])
        return vectors[0] if vectors else None
