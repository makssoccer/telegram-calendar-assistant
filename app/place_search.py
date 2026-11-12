"""Локальный semantic retrieval: текст → вектор → ближайшие места каталога."""
import asyncio
import logging
import os
import threading
from pathlib import Path
from urllib.parse import urlencode

import numpy as np
from pydantic import BaseModel, ConfigDict, Field

from app.tourism import load_catalog

logger = logging.getLogger(__name__)
MODEL = "intfloat/multilingual-e5-small"


class SearchRequest(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)
    query: str = Field(min_length=1, max_length=500)


class SearchError(Exception):
    pass


def load_model(*, download=False):
    # FastEmbed запускает ONNX на CPU. PyTorch, GPU и API-ключ не нужны.
    from fastembed import TextEmbedding
    from fastembed.common.model_description import ModelSource, PoolingType
    if not any(m["model"] == MODEL for m in TextEmbedding.list_supported_models()):
        TextEmbedding.add_custom_model(
            model=MODEL, pooling=PoolingType.MEAN, normalization=True,
            sources=ModelSource(hf=MODEL), dim=384, model_file="onnx/model.onnx",
        )
    cache = os.environ.get("EMBEDDING_CACHE", str(Path.home() / ".cache" / "assistant-embeddings"))
    return TextEmbedding(model_name=MODEL, cache_dir=cache, threads=2,
                         local_files_only=not download)


class PlaceSearch:
    """Один экземпляр на владельца: кэш не смешивает базы разных людей."""

    def __init__(self, knowledge=None):
        self.catalog = load_catalog()
        self.places = {p["id"]: p for p in self.catalog["places"]}
        self.knowledge = knowledge
        self._model = None
        self._vectors = {}
        self._lock = threading.Lock()

    async def search(self, request: SearchRequest) -> str:
        query = request.query.strip()
        if not query:
            raise SearchError("Напиши, какие места ищешь: /find места, связанные с королями")
        documents = list(self.catalog["documents"])
        if self.knowledge is not None:
            for place_id in self.places:
                documents.extend(await self.knowledge.retrieve_documents(place_id))
        try:
            # CPU-вычисления не занимают event loop: напоминания продолжают работать.
            hits = await asyncio.to_thread(self._rank, query, documents)
        except Exception as exc:
            logger.warning("Local place search failed (%s)", type(exc).__name__)
            raise SearchError("Локальная модель поиска недоступна. Подготовь её командой из docs/SEMANTIC_SEARCH.md.") from None
        lines = ["Ближайшие по смыслу места из моей базы Гродно. Это подбор кандидатов: совпадение не гарантирует выполнение всех пожеланий."]
        for score, document in hits:
            place = self.places[document["place_id"]]
            excerpt = document["text"][:450]
            if len(document["text"]) > 450:
                excerpt = excerpt.rsplit(" ", 1)[0] + "…"
            url = "https://www.google.com/maps/search/?" + urlencode({
                "api": 1, "query": f"{place['lat']},{place['lon']}"})
            lines.append(f"{place['name']}\nИз найденного материала: {excerpt}\nИсточник: {document['url']}\nКарта: {url}")
        lines.append("Можно спросить: «Расскажи про [название места]». Поиск не проверяет часы работы, наличие кафе и доступ внутрь.")
        return "\n\n".join(lines)

    def _rank(self, query, documents):
        hits, seen = [], set()
        for score, document in self.rank_fragments(query, documents):
            if document["place_id"] not in seen:
                hits.append((score, document))
                seen.add(document["place_id"])
                if len(hits) == 3:
                    break
        return hits

    def rank_fragments(self, query, documents):
        # Рассказчику нужны разные фрагменты одного места, поэтому здесь нет группировки.
        if not documents:
            return []
        # Lock защищает модель и кэш даже при одновременных вызовах из потоков.
        with self._lock:
            if self._model is None:
                self._model = load_model()
            texts = [f"{self.places[d['place_id']]['name']}. {d['title']}. {d['text']}" for d in documents]
            # Ключ — сам текст: новые/изменённые материалы пересчитываются,
            # удалённые вычищаются. Векторы живут в памяти, оригиналы — в PostgreSQL.
            missing = list(dict.fromkeys(t for t in texts if t not in self._vectors))
            if missing:
                vectors = self._model.embed(["passage: " + t for t in missing], batch_size=8)
                self._vectors.update(zip(missing, vectors, strict=True))
            self._vectors = {t: self._vectors[t] for t in texts}
            q = next(iter(self._model.embed(["query: " + query])))
            # Нормализованные векторы: скалярное произведение равно cosine similarity.
            # Это мера близости, НЕ вероятность правильного ответа.
            scores = np.stack([self._vectors[t] for t in texts]) @ q
            return [(float(scores[i]), documents[i]) for i in np.argsort(-scores, kind="stable")]


if __name__ == "__main__":
    # Отдельная подготовка скачивает только публичные веса; документы не отправляются.
    load_model(download=True)
    print("Модель смыслового поиска готова.")
