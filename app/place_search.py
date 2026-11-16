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
    # Через FastEmbed мы передаём текст модели E5. ONNX Runtime выполняет
    # вычисления модели локально на CPU.
    from fastembed import TextEmbedding
    from fastembed.common.model_description import ModelSource, PoolingType
    if not any(m["model"] == MODEL for m in TextEmbedding.list_supported_models()):
        # MEAN усредняет векторы токенов в один вектор текста из 384 чисел
        # (как float[384] в Java). normalization=True приводит его длину к 1.
        TextEmbedding.add_custom_model(
            model=MODEL, pooling=PoolingType.MEAN, normalization=True,
            sources=ModelSource(hf=MODEL), dim=384, model_file="onnx/model.onnx",
        )
    # Здесь на диске лежат веса модели. При обычном поиске читаем их из кэша.
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
        self._intent_vectors = {}
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

    def intent_scores(self, query, examples):
        # Модель общая с /find, а эталоны намерений имеют собственный кэш.
        with self._lock:
            if self._model is None:
                self._model = load_model()
            for label, phrases in examples.items():
                if label not in self._intent_vectors:
                    vectors = self._model.embed(["passage: " + phrase for phrase in phrases])
                    self._intent_vectors[label] = np.stack(list(vectors))
            vector = next(iter(self._model.embed(["query: " + query])))
            return {label: float(np.max(matrix @ vector)) for label, matrix in self._intent_vectors.items()}

    def rank_fragments(self, query, documents):
        # Рассказчику нужны разные фрагменты одного места, поэтому здесь нет группировки.
        if not documents:
            return []
        # Lock защищает модель и кэш даже при одновременных вызовах из потоков.
        with self._lock:
            if self._model is None:
                self._model = load_model()
            texts = [f"{self.places[d['place_id']]['name']}. {d['title']}. {d['text']}" for d in documents]
            # В памяти храним пары «текст → embedding». Для прежнего текста
            # вектор уже готов, а изменение заголовка или фрагмента создаёт новый ключ.
            missing = list(dict.fromkeys(t for t in texts if t not in self._vectors))
            if missing:
                # Префикс passage: обозначает материал для поиска. embed выдаёт
                # по вектору на текст в том же порядке, обрабатывая до 8 текстов за раз.
                vectors = self._model.embed(["passage: " + t for t in missing], batch_size=8)
                self._vectors.update(zip(missing, vectors, strict=True))
            self._vectors = {t: self._vectors[t] for t in texts}
            # Префикс query: обозначает вопрос пользователя. Передали один
            # вопрос, поэтому next берёт единственный вектор из результата embed.
            q = next(iter(self._model.embed(["query: " + query])))
            # np.stack собирает таблицу из N строк по 384 числа. Оператор @
            # умножает каждую строку на вектор вопроса и суммирует произведения
            # соответствующих координат. Для векторов длины 1 это косинусное сходство.
            scores = np.stack([self._vectors[t] for t in texts]) @ q
            # scores[i] относится к documents[i]. Сортировка по -scores ставит
            # наиболее близкие по смыслу фрагменты первыми.
            return [(float(scores[i]), documents[i]) for i in np.argsort(-scores, kind="stable")]


if __name__ == "__main__":
    # Отдельная подготовка скачивает только публичные веса; документы не отправляются.
    load_model(download=True)
    print("Модель смыслового поиска готова.")
