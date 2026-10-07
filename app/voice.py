"""Локальное распознавание: отдельный процесс можно остановить по таймауту."""
import asyncio
import json
import logging
import sys
from pathlib import Path
from tempfile import TemporaryDirectory

logger = logging.getLogger(__name__)
MAX_BYTES = 8 * 1024 * 1024


class VoiceError(Exception):
    pass


class LimitedFile:
    # Telegram скачивает файл порциями. Ограничиваем фактический размер,
    # даже если размер в метаданных отсутствует или оказался неверным.
    def __init__(self, stream):
        self.stream = stream

    def write(self, data):
        if self.stream.tell() + len(data) > MAX_BYTES:
            raise VoiceError("Голосовое слишком большое. Максимум — 8 МБ.")
        return self.stream.write(data)

    def seek(self, *args):
        return self.stream.seek(*args)

    def flush(self):
        return self.stream.flush()


class VoiceService:
    # На одну запись запускаем процесс с CPU-моделью. Аудио не передаётся
    # сервису распознавания. В облачный агент позже попадёт только текст.
    async def transcribe(self, bot, voice):
        if voice.duration > 60 or (voice.file_size or 0) > MAX_BYTES:
            raise VoiceError("Отправь голосовое длительностью до 60 секунд и размером до 8 МБ.")
        try:
            # Временный каталог удаляется и при ошибке, и после успешного ответа.
            with TemporaryDirectory(prefix="assistant-voice-") as directory:
                path = Path(directory) / "voice.ogg"
                with path.open("wb") as stream:
                    await bot.download(voice, destination=LimitedFile(stream), timeout=30)
                process = await asyncio.create_subprocess_exec(
                    sys.executable, "-m", "app.voice_worker", str(path),
                    stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.DEVNULL)
                try:
                    output, _ = await asyncio.wait_for(process.communicate(), timeout=120)
                finally:
                    # asyncio timeout не останавливает внешний процесс сам.
                    if process.returncode is None:
                        process.kill()
                        await process.wait()
                if process.returncode != 0:
                    raise VoiceError("Не удалось распознать запись. Попробуй ещё раз или отправь текст.")
                result = json.loads(output)
                if result.get("error"):
                    raise VoiceError(result["error"])
                text = result["text"].strip()
                if not text:
                    raise VoiceError("Не услышал речь. Запиши сообщение ещё раз.")
                if len(text) > 4000:
                    raise VoiceError("Получился слишком длинный текст. Отправь более короткую запись.")
                return text
        except VoiceError:
            raise
        except TimeoutError:
            raise VoiceError("Распознавание заняло слишком много времени. Отправь запись покороче.") from None
        except Exception as exc:
            logger.warning("Voice processing failed: %s", type(exc).__name__)
            raise VoiceError("Не удалось обработать голосовое. Попробуй ещё раз или отправь текст.") from None
