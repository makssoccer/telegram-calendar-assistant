"""Рабочий процесс Whisper. stdout содержит только JSON результата."""
import json
import os
import sys


def main():
    from faster_whisper import WhisperModel
    from faster_whisper.audio import decode_audio
    cache = os.environ.get("WHISPER_CACHE", "work/whisper-models")
    download = sys.argv[1] == "--download"
    # base — многоязычная модель; CPU/int8 не требует видеокарты.
    # В рабочем режиме сеть для модели отключена: веса готовим при установке.
    model = WhisperModel("base", device="cpu", compute_type="int8", cpu_threads=2,
                         download_root=cache, local_files_only=not download)
    if download:
        print("Local Whisper model ready")
        return
    audio = decode_audio(sys.argv[1], sampling_rate=16000)
    if len(audio) > 61 * 16000:
        print(json.dumps({"error": "Запись длиннее минуты. Отправь голосовое до 60 секунд."}, ensure_ascii=False))
        return
    # VAD отсекает тишину; condition_on_previous_text=False уменьшает повторы.
    segments, _ = model.transcribe(audio, language="ru", beam_size=3, vad_filter=True,
                                   condition_on_previous_text=False)
    text = " ".join(segment.text.strip() for segment in segments).strip()
    print(json.dumps({"text": text}, ensure_ascii=False))


if __name__ == "__main__":
    main()
