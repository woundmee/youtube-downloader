"""Состояние задачи и одно обновляемое сообщение Telegram."""

from __future__ import annotations

import asyncio
import logging
import math
import threading
import time
from dataclasses import dataclass, replace
from html import escape

from telegram import LinkPreviewOptions
from telegram.error import BadRequest, RetryAfter, TelegramError

LOG = logging.getLogger("bot")
PHASES = {
    "queued": "⏳ В очереди",
    "info": "🔎 Получение информации",
    "download": "⬇️ Скачивание текущего потока",
    "processing": "⚙️ Обработка видео и аудио",
    "compressing": "⚙️ Подготовка цельного MP3",
    "upload": "📤 Отправка в Telegram",
    "done": "✅ Готово",
    "cancelled": "⏹ Отменено",
    "error": "❌ Ошибка",
}


@dataclass(frozen=True)
class Snapshot:
    phase: str = "queued"
    title: str = ""
    video_title: str = ""
    quality: str = ""
    index: int = 0
    total: int = 0
    batch: int = 0
    batches: int = 0
    downloaded: float = 0
    size: float | None = None
    percent: float | None = None
    speed: float | None = None
    eta: float | None = None
    video_width: int = 0
    video_height: int = 0
    processing_started_at: float = 0
    processing_duration: float | None = None
    processed_seconds: float = 0
    processing_speed: float | None = None
    upload_started_at: float = 0
    upload_activity_at: float = 0
    upload_waiting: bool = False
    note: str = ""
    sent: int = 0
    failed: int = 0
    error: str = ""
    cancel_requested: bool = False


def positive_number(value) -> float | None:
    if isinstance(value, (int, float)) and math.isfinite(value) and value > 0:
        return float(value)
    return None


def clock_time(seconds: float) -> str:
    seconds = max(0, int(seconds))
    hours, rest = divmod(seconds, 3600)
    minutes, seconds = divmod(rest, 60)
    return f"{hours}:{minutes:02}:{seconds:02}" if hours else f"{minutes:02}:{seconds:02}"


class JobProgress:
    """Hooks yt-dlp пишут из потока; Telegram читает только последний снимок."""

    def __init__(self, **values):
        self._snapshot = Snapshot(**values)
        self._lock = threading.Lock()

    def snapshot(self) -> Snapshot:
        with self._lock:
            return self._snapshot

    def update(self, **values) -> None:
        with self._lock:
            self._snapshot = replace(self._snapshot, **values)

    def phase(self, phase: str, **values) -> None:
        changes = dict(phase=phase, percent=None, speed=None, downloaded=0,
                       size=None, eta=None, processing_started_at=0, processing_duration=None,
                       processed_seconds=0, processing_speed=None,
                       upload_started_at=0, upload_activity_at=0, upload_waiting=False)
        if phase in {"processing", "compressing"}:
            changes["processing_started_at"] = time.monotonic()
        if phase == "upload":
            changes.update(upload_started_at=time.monotonic(), upload_activity_at=time.monotonic(), percent=0)
        self.update(**(changes | values))

    def download_hook(self, data: dict) -> None:
        info = data.get("info_dict") or {}
        title = info.get("title") or self.snapshot().video_title
        if data.get("status") == "downloading":
            downloaded = positive_number(data.get("downloaded_bytes")) or 0
            size = positive_number(data.get("total_bytes")) or positive_number(data.get("total_bytes_estimate"))
            percent = min(100.0, downloaded / size * 100) if size else None
            self.update(phase="download", video_title=title, downloaded=downloaded,
                        size=size, percent=percent, speed=positive_number(data.get("speed")),
                        eta=positive_number(data.get("eta")))
        elif data.get("status") == "finished":
            self.phase("processing", video_title=title)

    def postprocessor_hook(self, data: dict) -> None:
        info = data.get("info_dict") or {}
        self.phase("processing", video_title=info.get("title") or self.snapshot().video_title)

    def processing_hook(self, data: dict) -> None:
        """FFmpeg сообщает время уже обработанного медиапотока, а не долю байтов."""
        try:
            processed = float(data.get("out_time_us", "")) / 1_000_000
        except (ValueError, TypeError):
            return
        if not math.isfinite(processed):
            return
        snapshot = self.snapshot()
        duration = positive_number(snapshot.processing_duration)
        if not duration:
            return
        processed = max(snapshot.processed_seconds, min(duration, max(0, processed)))
        try:
            speed = positive_number(float(str(data.get("speed", "")).rstrip("x")))
        except (ValueError, TypeError):
            speed = None
        # 100% — только после успешной проверки готового файла.
        self.update(processed_seconds=processed, percent=min(99, processed / duration * 100),
                    processing_speed=speed,
                    eta=(duration - processed) / speed if speed and processed > 0 else None)

    def render(self) -> str:
        data = self.snapshot()
        heading = PHASES[data.phase]
        if data.phase == "done" and data.failed:
            heading = "⚠️ Завершено с ошибками"
        sections = [f"<b>{heading}</b>"]
        if data.cancel_requested and data.phase not in {"done", "cancelled", "error"}:
            sections.append("⏹ Запрошена отмена…")
        terminal = data.phase in {"done", "cancelled", "error"}
        title = (data.title if terminal and data.total > 1 else data.video_title or data.title)
        if title:
            sections.append(escape(title[:220]))
        details = []
        if data.quality:
            quality = data.quality
            if not quality.startswith(("🎵", "🎬")):
                quality = "🎬 " + quality
            details.append(f"Выбрано: <b>{escape(quality)}</b>")
        if data.video_width and data.video_height:
            details.append(f"Разрешение: {data.video_width}×{data.video_height}")
        if data.total > 1:
            if data.title and data.title != title:
                details.append(f"Плейлист: {escape(data.title[:200])}")
            details.append(f"Видео: {data.index}/{data.total}" if data.index else f"Видео в задаче: {data.total}")
        if data.batches > 1:
            details.append(f"📦 Пакет: {data.batch}/{data.batches}")
        if details:
            sections.append("\n".join(details))
        if data.note:
            sections.append(escape(data.note))
        transfer = []
        if data.phase in {"download", "upload", "processing", "compressing"}:
            if data.percent is not None:
                filled = max(0, min(10, int(data.percent // 10)))
                transfer.append(f"<code>{'█' * filled}{'░' * (10 - filled)}</code> <b>{data.percent:.1f}%</b>")
            if data.phase in {"download", "upload"}:
                amount = f"{data.downloaded / 1_000_000:.1f} МБ"
                if data.size:
                    amount += f" / {data.size / 1_000_000:.1f} МБ"
                transfer.append(amount)
            if data.phase in {"processing", "compressing"}:
                if data.processing_duration:
                    transfer.append(f"Обработано: {clock_time(data.processed_seconds)} / {clock_time(data.processing_duration)}")
                transfer.append(f"Прошло: {clock_time(time.monotonic() - data.processing_started_at)}")
                if data.eta is not None and data.percent is not None and data.percent < 99:
                    transfer.append(f"Осталось примерно: {clock_time(math.ceil(data.eta))}")
                elif data.percent is not None and 99 <= data.percent < 100:
                    transfer.append("Завершаю обработку и проверяю файл…")
            elif data.phase == "upload":
                elapsed = max(0, time.monotonic() - data.upload_started_at)
                speed = data.downloaded / max(elapsed, 0.001)
                transfer.append(f"Средняя скорость: {speed / 1_000_000:.2f} МБ/с")
                seconds = int(elapsed)
                transfer.append(f"Время отправки: {seconds // 60:02}:{seconds % 60:02}")
                if data.upload_waiting:
                    transfer.append("⏳ Ожидаю подтверждение доставки…")
                elif time.monotonic() - data.upload_activity_at >= 15:
                    idle = int(time.monotonic() - data.upload_activity_at)
                    transfer.append(f"⚠️ Нет новых данных: {idle} с. Ожидаю сеть…")
            else:
                transfer.append(f"Скорость: {data.speed / 1_000_000:.2f} МБ/с" if data.speed else "Скорость: определяется…")
                if data.eta is not None:
                    transfer.append(f"Осталось примерно: {clock_time(math.ceil(data.eta))}")
        if transfer:
            sections.append("\n".join(transfer))
        if data.sent or data.failed or terminal:
            result = f"Отправлено: {data.sent}"
            if data.failed:
                result += f" · Ошибок: {data.failed}"
            sections.append(result)
        if data.error:
            sections.append(f"Последняя ошибка: {escape(data.error[:400])}")
        return "\n\n".join(sections)


class ProgressReporter:
    def __init__(self, bot, chat_id: int, progress: JobProgress, interval: float,
                 message_id: int | None = None):
        self.bot = bot
        self.chat_id = chat_id
        self.progress = progress
        self.interval = interval
        self.message_id = message_id
        self._create_attempted = message_id is not None
        self._last_text = ""
        self._retry_at = 0.0
        self._lock = asyncio.Lock()
        self._stop = asyncio.Event()
        self._task: asyncio.Task | None = None

    async def start(self) -> None:
        await self.publish()
        self._task = asyncio.create_task(self._run())

    async def publish(self) -> None:
        async with self._lock:
            text = self.progress.render()
            if text == self._last_text or time.monotonic() < self._retry_at:
                return
            try:
                if self.message_id is None:
                    if self._create_attempted:
                        return
                    # Не повторяем sendMessage после неоднозначной сетевой ошибки:
                    # Telegram мог принять сообщение, хотя ответ не дошёл.
                    self._create_attempted = True
                    message = await self.bot.send_message(self.chat_id, text,
                                                          parse_mode="HTML",
                                                          link_preview_options=LinkPreviewOptions(is_disabled=True))
                    self.message_id = message.message_id
                else:
                    await self.bot.edit_message_text(chat_id=self.chat_id, message_id=self.message_id, text=text,
                                                     parse_mode="HTML",
                                                     link_preview_options=LinkPreviewOptions(is_disabled=True))
                self._last_text = text
            except RetryAfter as error:
                delay = error.retry_after
                seconds = delay.total_seconds() if hasattr(delay, "total_seconds") else delay
                self._retry_at = time.monotonic() + seconds + 1
                if self.message_id is None:
                    self._create_attempted = False
                LOG.warning("Обновление прогресса отложено Telegram на %s сек.", seconds)
            except BadRequest as error:
                message = str(error).lower()
                if "message is not modified" in message:
                    self._last_text = text
                elif "message to edit not found" in message or "message can't be edited" in message:
                    self.message_id = None
                    self._create_attempted = False
                else:
                    self._retry_at = time.monotonic() + max(10, self.interval)
                    LOG.warning("Ошибка сообщения прогресса: %s", error)
            except TelegramError as error:
                self._retry_at = time.monotonic() + max(10, self.interval)
                LOG.warning("Не удалось обновить прогресс: %s", error)

    async def _run(self) -> None:
        while not self._stop.is_set():
            try:
                await asyncio.wait_for(self._stop.wait(), timeout=self.interval)
            except asyncio.TimeoutError:
                await self.publish()

    async def stop(self) -> None:
        self._stop.set()
        if self._task:
            await self._task

    async def finish(self) -> None:
        await self.stop()
        await self.publish()
