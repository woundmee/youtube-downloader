"""Личный Telegram-бот для YouTube. Запуск: python bot.py."""

from __future__ import annotations

import sys

if sys.version_info < (3, 10):
    raise SystemExit("Нужен Python 3.10 или новее. Запусти бота через .venv/bin/python (Python 3.12).")

import asyncio
import json
import logging
import math
import multiprocessing
import os
import re
import secrets
import signal
import shutil
import sqlite3
import subprocess
import threading
import time
from contextlib import asynccontextmanager, closing, contextmanager
from copy import deepcopy
from dataclasses import dataclass, field
from datetime import datetime, timezone
from logging.handlers import RotatingFileHandler
from pathlib import Path
from tempfile import TemporaryDirectory
from urllib.parse import parse_qs, urlencode, urlsplit

import config as cfg
import httpx
import yt_dlp
from telegram import Bot, InlineKeyboardButton, InlineKeyboardMarkup, InputFile, Update
from telegram.error import BadRequest, NetworkError, RetryAfter, TelegramError, TimedOut
from telegram.request import HTTPXRequest
from telegram.ext import (
    Application, ApplicationHandlerStop, CallbackQueryHandler, CommandHandler,
    ContextTypes, MessageHandler, TypeHandler, filters,
)
from yt_dlp.utils import DownloadCancelled, DownloadError, ExtractorError
from progress import JobProgress, ProgressReporter

ROOT = Path(__file__).resolve().parent
LOG = logging.getLogger("bot")
AUDIT = logging.getLogger("downloads")
QUALITY_LABELS = {"mp3": "🎵 MP3", "mp4": "🎬 MP4 · лучшее", "144": "144p", "240": "240p",
                  "360": "360p", "480": "480p", "720": "720p", "1080": "1080p", "1440": "2K · 1440p"}


def local_path(value: str) -> Path:
    return (ROOT / value).resolve()


def bot_api_url() -> str | None:
    value = getattr(cfg, "BOT_API_URL", None)
    return value.rstrip("/") if isinstance(value, str) and value else None


def telegram_file_limit() -> int:
    return getattr(cfg, "BOT_API_FILE_LIMIT", 1_950_000_000) if bot_api_url() else cfg.TELEGRAM_FILE_LIMIT


def api_upload_timeout() -> float:
    return getattr(cfg, "BOT_API_UPLOAD_TIMEOUT", 7200) if bot_api_url() else cfg.UPLOAD_MAX_TIMEOUT


def redact(value: str) -> str:
    value = value.replace(cfg.BOT_TOKEN, "[TOKEN]") if cfg.BOT_TOKEN else value
    value = re.sub(r"\b\d{6,}:[A-Za-z0-9_-]{20,}\b", "[TOKEN]", value)
    value = re.sub(r"((?:https?|socks5h?|socks4a?)://)[^/\s@]+@", r"\1[AUTH]@", value)
    return re.sub(r"\x1b\[[0-?]*[ -/]*[@-~]", "", value)


def user_error(error: Exception) -> str:
    if isinstance(error, (UploadError, MetadataTimeoutError)):
        return redact(str(error))[:1500]
    if isinstance(error, asyncio.TimeoutError) or (isinstance(error, NetworkError) and not isinstance(error, BadRequest)):
        if bot_api_url():
            return ("Нет ответа от собственного Bot API. Проверь его службу, адрес BOT_API_URL "
                    "и доступ сервера к Telegram.")
        return "Нет устойчивого соединения с Telegram. Проверь доступ к api.telegram.org с сервера."
    message = redact(str(error))
    if "the page needs to be reloaded" in message.lower():
        return ("YouTube отклонил запрос проигрывателя. Обнови yt-dlp вместе с EJS "
                "до исправленной версии из requirements.txt и перезапусти бота. "
                "Если ошибка сохраняется, проверь версию Node.js/Deno и журнал bot.log.")
    if any(text in message.lower() for text in ("sign in", "confirm you", "cookies", "login required")):
        return ("YouTube запросил вход или подтверждение «не бот». "
                "Добавь cookies в настройку COOKIE_FILE (инструкция в README.md) и повтори запрос.")
    if isinstance(error, (DownloadError, ExtractorError)) and is_dns_error(error):
        return ("Не удалось определить адрес видеосервера YouTube. "
                "Проверь DNS и доступ к googlevideo.com с сервера. Подробности — в logs/bot.log.")
    return message[:1500]


def error_details(error: BaseException) -> str:
    parts, seen = [], set()
    while error and id(error) not in seen:
        seen.add(id(error))
        parts.append(f"{type(error).__name__}: {str(error) or repr(error)}")
        error = error.__cause__ or error.__context__
    return redact(" <- ".join(parts))


def can_retry_connection(error: BaseException) -> bool:
    """Повторяем только сбой до передачи запроса: прочие ошибки допускают дубликаты."""
    seen = set()
    while error and id(error) not in seen:
        seen.add(id(error))
        if isinstance(error, (httpx.ConnectError, httpx.ConnectTimeout, httpx.PoolTimeout)):
            return True
        error = error.__cause__ or error.__context__
    return False


class UploadError(RuntimeError):
    pass


class MetadataTimeoutError(TimeoutError):
    pass


class SecretFormatter(logging.Formatter):
    def format(self, record):
        return redact(super().format(record))


def setup_logging() -> None:
    directory = local_path(cfg.LOG_DIR)
    directory.mkdir(parents=True, exist_ok=True)
    formatter = SecretFormatter("%(asctime)s %(levelname)s %(message)s")
    console = logging.StreamHandler()
    console.setFormatter(formatter)
    service = RotatingFileHandler(directory / "bot.log", maxBytes=cfg.LOG_MAX_BYTES,
                                  backupCount=cfg.LOG_BACKUP_COUNT, encoding="utf-8")
    service.setFormatter(formatter)
    logging.basicConfig(level=logging.INFO, handlers=[console, service], force=True)
    audit_handler = RotatingFileHandler(directory / "downloads.log", maxBytes=cfg.LOG_MAX_BYTES,
                                        backupCount=cfg.LOG_BACKUP_COUNT, encoding="utf-8")
    audit_handler.setFormatter(SecretFormatter("%(message)s"))
    for handler in AUDIT.handlers[:]:
        AUDIT.removeHandler(handler)
        handler.close()
    AUDIT.addHandler(audit_handler)
    AUDIT.setLevel(logging.INFO)
    AUDIT.propagate = False
    # HTTP URL Telegram содержит токен; не записываем запросы библиотеки.
    logging.getLogger("httpx").setLevel(logging.WARNING)
    logging.getLogger("httpcore").setLevel(logging.WARNING)


def audit(event: str, user, **details) -> None:
    AUDIT.info(json.dumps({"time": datetime.now(timezone.utc).isoformat(), "event": event,
                           "user_id": user.id, "username": user.username, **details},
                          ensure_ascii=False, default=str))


def allowed(username: str | None) -> bool:
    return bool(username) and username.casefold() in {
        name.strip().lstrip("@").casefold() for name in cfg.users if name.strip()
    }


def normalized_username(username: str) -> str:
    return username.strip().lstrip("@").casefold()


def is_admin(username: str | None) -> bool:
    return bool(username) and allowed(username) and normalized_username(username) == normalized_username(
        getattr(cfg, "ADMIN_BOT", "rrojab"))


@contextmanager
def user_database():
    path = local_path(getattr(cfg, "USERS_DB_PATH", "data/users.sqlite3"))
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    path.touch(mode=0o600, exist_ok=True)
    with closing(sqlite3.connect(path, timeout=5)) as database, database:
        database.execute("CREATE TABLE IF NOT EXISTS participants "
                         "(user_id INTEGER PRIMARY KEY, username TEXT NOT NULL UNIQUE, chat_id INTEGER NOT NULL)")
        yield database


def remember_user(user, chat) -> None:
    if not allowed(user.username) or chat.type != "private":
        return
    username = normalized_username(user.username)
    with user_database() as database:
        database.execute("DELETE FROM participants WHERE username = ? AND user_id != ?", (username, user.id))
        database.execute("INSERT INTO participants (user_id, username, chat_id) VALUES (?, ?, ?) "
                         "ON CONFLICT(user_id) DO UPDATE SET username=excluded.username, chat_id=excluded.chat_id",
                         (user.id, username, chat.id))


def broadcast_recipients() -> tuple[list[tuple[str, int]], list[str]]:
    names = sorted({normalized_username(name) for name in cfg.users if name.strip()})
    with user_database() as database:
        known = dict(database.execute("SELECT username, chat_id FROM participants"))
    # История обращений не даёт доступа: получатели определяются только текущим users.
    return ([(name, known[name]) for name in names if name in known],
            [name for name in names if name not in known])


def youtube_urls(text: str) -> tuple[str | None, str | None]:
    """Возвращает только канонические URL видео/плейлиста, исключая чужие домены."""
    match = re.search(r"https?://[^\s<>]+", text)
    if not match:
        raise ValueError("Пришли ссылку на видео или на плейлист в формате https://youtube.com/...")
    parsed = urlsplit(match.group().rstrip(".,!);]"))
    host = (parsed.hostname or "").casefold()
    if parsed.username or parsed.password or host not in {
        "youtube.com", "www.youtube.com", "m.youtube.com", "music.youtube.com", "youtu.be", "www.youtu.be",
    }:
        raise ValueError("Поддерживаются только ссылки на youtube.com и youtu.be.")
    query = parse_qs(parsed.query)
    video_id = None
    if host in {"youtu.be", "www.youtu.be"}:
        video_id = parsed.path.strip("/").split("/")[0]
    elif parsed.path.rstrip("/") == "/watch":
        video_id = query.get("v", [None])[0]
    else:
        parts = parsed.path.strip("/").split("/")
        if len(parts) == 2 and parts[0] in {"shorts", "live", "embed"}:
            video_id = parts[1]
    playlist_id = query.get("list", [None])[0]
    if video_id and not re.fullmatch(r"[A-Za-z0-9_-]{11}", video_id):
        raise ValueError("Некорректный идентификатор видео YouTube.")
    if playlist_id and not re.fullmatch(r"[A-Za-z0-9_-]{2,200}", playlist_id):
        raise ValueError("Некорректный идентификатор плейлиста YouTube.")
    if not video_id and not playlist_id:
        raise ValueError("В ссылке нет видео или плейлиста.")
    video = f"https://www.youtube.com/watch?v={video_id}" if video_id else None
    playlist = "https://www.youtube.com/playlist?" + urlencode({"list": playlist_id}) if playlist_id else None
    return video, playlist


class YDLLogger:
    def debug(self, message):
        LOG.debug("yt-dlp: %s", message)

    def warning(self, message):
        LOG.warning("yt-dlp: %s", message)

    def error(self, message):
        LOG.error("yt-dlp: %s", message)


def ydl_options() -> dict:
    options = {"quiet": True, "noprogress": True, "logger": YDLLogger(),
               "noplaylist": True, "socket_timeout": cfg.NETWORK_TIMEOUT,
               "proxy": "",  # Прямое подключение, даже если прокси задан в окружении.
               "retries": 3, "fragment_retries": 3,
               "concurrent_fragment_downloads": cfg.DOWNLOAD_CONCURRENT_FRAGMENTS,
               "cachedir": str(local_path(cfg.YTDLP_CACHE_DIR)),
               "js_runtimes": {cfg.JS_RUNTIME: {"path": cfg.JS_RUNTIME_PATH} if cfg.JS_RUNTIME_PATH else {}}}
    if cfg.FFMPEG_DIR:
        options["ffmpeg_location"] = str(local_path(cfg.FFMPEG_DIR))
    if cfg.COOKIE_FILE:
        options["cookiefile"] = str(local_path(cfg.COOKIE_FILE))
    return options


def _metadata_worker(url: str, options: dict, connection) -> None:
    # Отдельная группа позволяет отменить также запущенный yt-dlp Node.js/Deno.
    if os.name == "posix":
        os.setsid()

    class Logger:
        def debug(self, message):
            pass

        def warning(self, message):
            connection.send(("warning", message))

        def error(self, message):
            connection.send(("log_error", message))

    try:
        with yt_dlp.YoutubeDL({**options, "logger": Logger()}) as ydl:
            if options.get("cookiefile"):
                # Читаем cookies, но не перезаписываем оригинал при прерывании процесса.
                _ = ydl.cookiejar
                ydl.params["cookiefile"] = None
            info = ydl.extract_info(url, download=False)
            info = ydl.sanitize_info(info) if info else None
        connection.send(("result", info))
    except Exception as error:
        connection.send(("download_error" if isinstance(error, DownloadError) else "error", str(error)))
    finally:
        connection.close()


def extract(url: str, playlist: bool = False, cancel: threading.Event | None = None) -> dict:
    """Ограничиваем всё получение метаданных, включая DNS, повторы и JS-проверки."""
    cancel = cancel if cancel is not None else threading.Event()
    if cancel.is_set():
        raise DownloadCancelled("Подготовка отменена пользователем")
    options = ydl_options()
    options.pop("logger", None)
    options.update(socket_timeout=min(cfg.NETWORK_TIMEOUT, 15), retries=1, extractor_retries=1)
    if playlist:
        options.update(noplaylist=False, extract_flat="in_playlist", ignoreerrors=True)
    timeout = getattr(cfg, "METADATA_TIMEOUT", 120)
    context = multiprocessing.get_context("spawn")
    receiver, sender = context.Pipe(duplex=False)
    worker = context.Process(target=_metadata_worker, args=(url, options, sender), name="youtube-metadata")
    started = time.monotonic()
    completed = False
    LOG.info("Получение метаданных: url=%s playlist=%s timeout=%s", url, playlist, timeout)
    try:
        worker.start()
        sender.close()
        while True:
            if cancel.is_set():
                raise DownloadCancelled("Подготовка отменена пользователем")
            if time.monotonic() - started >= timeout:
                raise MetadataTimeoutError(f"YouTube не ответил за {timeout:g} с. Проверь доступ с сервера "
                                           "и предупреждения yt-dlp в logs/bot.log, затем повтори запрос.")
            if not receiver.poll(0.2):
                if not worker.is_alive():
                    raise ValueError("Процесс получения информации YouTube завершился без результата. Проверь logs/bot.log.")
                continue
            try:
                kind, payload = receiver.recv()
            except EOFError:
                raise ValueError("Процесс получения информации YouTube неожиданно завершился. Проверь logs/bot.log.") from None
            if kind in {"warning", "log_error"}:
                LOG.log(logging.WARNING if kind == "warning" else logging.ERROR, "yt-dlp: %s", payload)
            elif kind == "result":
                info = payload
                completed = True
                break
            elif kind == "download_error":
                raise DownloadError(payload)
            else:
                raise ValueError(payload)
    finally:
        if completed:
            worker.join(timeout=1)
        if worker.is_alive():
            if os.name == "posix":
                try:
                    os.killpg(worker.pid, signal.SIGTERM)
                except ProcessLookupError:
                    worker.terminate()
            else:
                worker.terminate()
            worker.join(timeout=1)
            if worker.is_alive():
                if os.name == "posix":
                    try:
                        os.killpg(worker.pid, signal.SIGKILL)
                    except ProcessLookupError:
                        worker.kill()
                else:
                    worker.kill()
                worker.join()
        receiver.close()
        sender.close()
        worker.close()
        LOG.info("Получение метаданных завершено: seconds=%.3f success=%s cancelled=%s",
                 time.monotonic() - started, completed, cancel.is_set())
    if not info:
        raise ValueError("Не удалось получить информацию. Видео или плейлист недоступны.")
    if not playlist and (info.get("is_live") or info.get("live_status") == "is_upcoming"):
        raise ValueError("Дождись окончания прямого эфира, затем пришли ссылку ещё раз.")
    return info


def video_resolution(info: dict) -> int | None:
    dimensions = [value for key in ("width", "height") if isinstance(value := info.get(key), (int, float))
                  and math.isfinite(value) and value > 0]
    return int(min(dimensions)) if dimensions else None


def is_portrait(info: dict) -> bool:
    formats = [item for item in info.get("formats", []) if item.get("vcodec") not in {None, "none"}
               and video_resolution(item) and item.get("width") and item.get("height")]
    largest = max(formats, key=lambda item: item["width"] * item["height"], default=info)
    return bool(largest.get("width") and largest.get("height") and largest["width"] < largest["height"])


def available_video_resolutions(info: dict) -> list[int]:
    return sorted({value for item in info.get("formats", [])
                   if item.get("vcodec") not in {None, "none"} and not item.get("has_drm")
                   and (value := video_resolution(item))})


def qualities(info: dict) -> list[str]:
    formats = [item for item in info.get("formats", []) if not item.get("has_drm")]
    choices = ["mp3"] if any(item.get("acodec") not in {None, "none"} for item in formats) else []
    if not any(item.get("vcodec") not in {None, "none"} for item in formats):
        return choices
    resolutions = available_video_resolutions(info)
    return choices + ["mp4"] + [str(h) for h in (144, 240, 360, 480, 720, 1080, 1440) if h in resolutions]


def playlist_entries(info: dict) -> tuple[str, ...]:
    return tuple(f"https://www.youtube.com/watch?v={item['id']}"
                 for item in info.get("entries") or []
                 if item and re.fullmatch(r"[A-Za-z0-9_-]{11}", item.get("id") or ""))


def requested_resolution(quality: str, info: dict) -> int | None:
    if quality == "mp3":
        return None
    if quality != "mp4":
        return int(quality)
    return max(available_video_resolutions(info), default=None)


def format_options(quality: str, *, portrait: bool = False, resolution: int | None = None) -> dict:
    if quality == "mp3":
        return {"format": "bestaudio/best", "postprocessors": [
            {"key": "FFmpegExtractAudio", "preferredcodec": "mp3", "preferredquality": cfg.MP3_BITRATE}]}
    if quality not in QUALITY_LABELS:
        raise ValueError("Неизвестное качество.")
    dimension = "width" if portrait else "height"
    target = resolution if quality == "mp4" else int(quality)
    limit = f"[{dimension}={target}]" if target else ""
    return {"format": f"bv{limit}+ba/b{limit}/bv{limit}",
            "format_sort": ["res", "vcodec:h264", "acodec:aac", "ext:mp4:m4a"],
            "merge_output_format": "mp4", "postprocessors": [
                {"key": "FFmpegVideoRemuxer", "preferedformat": "mp4"}]}


def hls_format_options(quality: str, *, portrait: bool = False, resolution: int | None = None) -> dict:
    # При резервном MP3 выбираем полный поток: отдельное HLS-аудио
    # иногда находится на том же недоступном CDN.
    options = format_options(quality, portrait=portrait, resolution=resolution)
    if quality == "mp3":
        options["format_sort"] = format_options("mp4")["format_sort"]
    dimension = "width" if portrait else "height"
    target = None if quality == "mp3" else resolution if quality == "mp4" else int(quality)
    limit = f"[{dimension}={target}]" if target else ""
    options["format"] = f"b[protocol^=m3u8]{limit}"
    if quality != "mp3":
        options["format"] += f"/bv[protocol^=m3u8]{limit}"
    return options


def is_dns_error(error: Exception) -> bool:
    return bool(re.search(r"failed to resolve|name resolution|nameresolutionerror|getaddrinfo failed"
                          r"|name or service not known|nodename nor servname|no address associated"
                          r"|address family for hostname not supported", str(error), re.IGNORECASE))


def can_retry_media(error: Exception) -> bool:
    if is_dns_error(error):
        return True
    return bool(re.search(r"network is unreachable|failed to establish|timed? out|connection (?:reset|refused|aborted)"
                          r"|temporary failure|HTTP Error (?:403|410|5\d\d)", str(error), re.IGNORECASE))


def metadata_for_selection(info: dict) -> dict:
    result = deepcopy(info)
    # process_ie_result сохраняет эти поля из прежнего выбора, даже если новый
    # формат — один HLS-поток или только аудио. Полный список formats оставляем.
    for key in ("requested_formats", "requested_downloads", "__postprocessors", "__pending_error"):
        result.pop(key, None)
    return result


def download(url: str, quality: str, directory: Path, cancel: threading.Event,
             tracker: JobProgress | None = None, cached_info: dict | None = None) -> tuple[Path, dict]:
    postprocess_starts = {}
    postprocess_seconds = 0.0

    def progress(data):
        if cancel.is_set():
            raise DownloadCancelled("Загрузка отменена пользователем")
        if tracker:
            tracker.download_hook(data)

    def postprocess(data):
        nonlocal postprocess_seconds
        if cancel.is_set():
            raise DownloadCancelled("Обработка отменена пользователем")
        name = data.get("postprocessor", "unknown")
        if data.get("status") == "started":
            postprocess_starts[name] = time.perf_counter()
        elif data.get("status") == "finished":
            started = postprocess_starts.pop(name, None)
            if started is not None:
                postprocess_seconds += time.perf_counter() - started
        if tracker:
            tracker.postprocessor_hook(data)

    def reject_live(info, *, incomplete=False):
        if info.get("is_live") or info.get("live_status") == "is_upcoming":
            return "Прямые эфиры и ещё не начавшиеся премьеры не поддерживаются."
        return None

    if cancel.is_set():
        raise DownloadCancelled("Загрузка отменена пользователем")
    if cached_info is None:
        cached_info = extract(url, cancel=cancel)
    portrait = is_portrait(cached_info)
    expected_resolution = requested_resolution(quality, cached_info)
    available = available_video_resolutions(cached_info)
    if quality not in {"mp3", "mp4"} and available and expected_resolution not in available:
        LOG.info("Выбранного разрешения нет: url=%s requested=%sp available=%s", url, expected_resolution, available)
        listed = ", ".join(f"{value}p" for value in available)
        raise ValueError(f"YouTube не предоставил для этого видео {expected_resolution}p. "
                         f"Максимум: {max(available)}p. Доступно: {listed}. "
                         "Выбери доступное качество или «MP4 · лучшее».")
    options = {**ydl_options(), **format_options(quality, portrait=portrait, resolution=expected_resolution),
               "outtmpl": str(directory / "%(id)s.%(ext)s"),
               "progress_hooks": [progress], "postprocessor_hooks": [postprocess],
               "match_filter": reject_live, "skip_unavailable_fragments": False}
    hls_available = any(
        str(item.get("protocol", "")).startswith("m3u8") for item in cached_info.get("formats", []))
    modes = ["direct", "hls"] if hls_available else ["direct"]
    output_directory = directory
    for index, mode in enumerate(modes):
        attempt_options = options.copy()
        if mode == "hls":
            # Части прямого HTTP-файла нельзя продолжать фрагментами другого потока.
            output_directory = directory / mode
            output_directory.mkdir(exist_ok=True)
            attempt_options.update(outtmpl=str(output_directory / "%(id)s.%(ext)s"),
                                   socket_timeout=min(cfg.NETWORK_TIMEOUT, getattr(cfg, "YOUTUBE_FALLBACK_TIMEOUT", 10)),
                                   retries=1, fragment_retries=1)
            attempt_options.update(hls_format_options(quality, portrait=portrait, resolution=expected_resolution))
            if tracker:
                tracker.phase("info", note="Пробую другой поток YouTube…")
        elif hls_available:
            attempt_options.update(socket_timeout=min(cfg.NETWORK_TIMEOUT, getattr(cfg, "YOUTUBE_FALLBACK_TIMEOUT", 10)),
                                   retries=1)
        try:
            with yt_dlp.YoutubeDL(attempt_options) as ydl:
                if mode == "direct":
                    try:
                        info = ydl.process_ie_result(metadata_for_selection(cached_info), download=True)
                    except DownloadError as error:
                        if cancel.is_set() or not re.search(r"\b(?:403|410)\b", str(error)):
                            raise
                        LOG.info("Ссылка на медиапоток устарела, обновляю метаданные: %s", url)
                        info = ydl.extract_info(url, download=True)
                else:
                    info = ydl.extract_info(url, download=True)
        except ValueError as error:
            # При ошибке параллельного фрагмента yt-dlp может закрыть общий поток
            # раньше, чем соседний поток закончит запись. Неполный файл не отправляем.
            if str(error) == "write to closed file":
                raise DownloadError("Не удалось получить все фрагменты YouTube. Неполный файл не отправлен.") from error
            raise
        except (DownloadError, ExtractorError) as error:
            unavailable = "requested format is not available" in str(error).lower()
            if cancel.is_set() or index == len(modes) - 1 or not can_retry_media(error):
                if unavailable and expected_resolution and not cancel.is_set():
                    raise ValueError(f"Не удалось получить доступный поток {expected_resolution}p. "
                                     "Видео в меньшем разрешении не отправлено. Повтори запрос позже "
                                     "или выбери другое качество.") from error
                raise
            LOG.warning("Поток YouTube недоступен: method=%s next=%s url=%s error=%s",
                        mode, modes[index + 1], url, redact(str(error)))
            continue
        break
    if not info:
        raise ValueError("Видео недоступно.")
    path = output_directory / f"{info['id']}.{'mp3' if quality == 'mp3' else 'mp4'}"
    if not path.is_file():
        raise ValueError("yt-dlp не создал итоговый файл.")
    if quality == "mp3":
        duration = validate_audio_duration(path, info.get("duration"), cancel)
        info["_bot_audio_duration"] = duration
        if tracker:
            seconds = round(duration)
            tracker.update(note=f"Полная запись · {seconds // 60}:{seconds % 60:02} · {cfg.MP3_BITRATE} кбит/с")
    else:
        started = time.perf_counter()
        path, info["_bot_video_metadata"] = prepare_video(path, info.get("duration"), cancel, tracker)
        actual_resolution = video_resolution(info["_bot_video_metadata"])
        if expected_resolution and actual_resolution and actual_resolution != expected_resolution:
            raise ValueError(f"Ожидалось {expected_resolution}p, получено {actual_resolution}p. "
                             "Файл с неподходящим разрешением не отправлен.")
        postprocess_seconds += time.perf_counter() - started
        if tracker:
            tracker.update(note="")
    info["_bot_postprocess_seconds"] = postprocess_seconds
    info["_bot_download_method"] = mode
    return path, info


def executable(name: str) -> str:
    if cfg.FFMPEG_DIR:
        candidate = local_path(cfg.FFMPEG_DIR) / (name + (".exe" if os.name == "nt" else ""))
        if not candidate.is_file():
            raise ValueError(f"Не найден {candidate}. Проверь FFMPEG_DIR.")
        return str(candidate)
    found = shutil.which(name)
    if not found:
        raise ValueError(f"Не найден {name}. Установи FFmpeg и добавь его в PATH.")
    return found


def check_js_runtime() -> None:
    runtime = cfg.JS_RUNTIME_PATH or shutil.which(cfg.JS_RUNTIME)
    if not runtime or not Path(runtime).is_file():
        raise ValueError(f"Не найден {cfg.JS_RUNTIME}. Установи Node.js >=22 или Deno >=2.3.")
    minimums = {"node": (22, 0, 0), "deno": (2, 3, 0)}
    minimum = minimums.get(cfg.JS_RUNTIME)
    if minimum is None:
        return
    try:
        result = subprocess.run([str(runtime), "--version"], capture_output=True,
                                text=True, check=True, timeout=10)
    except (OSError, subprocess.SubprocessError):
        raise ValueError(f"Не удалось запустить {cfg.JS_RUNTIME}. Проверь JS_RUNTIME_PATH и права запуска.") from None
    match = re.search(r"(?m)^(?:v|deno\s+)?(\d+)\.(\d+)\.(\d+)\b", result.stdout)
    if not match:
        raise ValueError(f"Не удалось определить версию {cfg.JS_RUNTIME}. Проверь JS_RUNTIME_PATH.")
    actual = tuple(map(int, match.groups()))
    if actual < minimum:
        current, required = ".".join(map(str, actual)), ".".join(map(str, minimum))
        raise ValueError(f"Установлен {cfg.JS_RUNTIME} {current}; для YouTube нужен минимум {required}. "
                         "Установи подходящую версию и укажи её бинарник в JS_RUNTIME_PATH.")


def run_process(args: list[str], cancel: threading.Event, poll=None) -> str:
    # communicate с небольшим timeout позволяет /cancel прервать FFmpeg.
    with subprocess.Popen(args, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True) as process:
        deadline = time.monotonic() + cfg.PROCESS_TIMEOUT
        while True:
            try:
                if cancel.is_set():
                    raise DownloadCancelled("Обработка отменена пользователем")
                if time.monotonic() > deadline:
                    raise TimeoutError("Превышено время обработки FFmpeg.")
                if poll:
                    poll()
                stdout, stderr = process.communicate(timeout=0.5)
                if process.returncode:
                    raise ValueError(f"Ошибка FFmpeg: {stderr[-1000:]}")
                if poll:
                    poll()
                return stdout
            except subprocess.TimeoutExpired:
                continue
            except BaseException:
                process.kill()
                process.communicate()
                raise


def run_ffmpeg(args: list[str], cancel: threading.Event, tracker: JobProgress | None = None) -> str:
    if tracker is None:
        return run_process(args, cancel)
    # Читаем отдельный поток -progress порциями, не накапливая вывод FFmpeg в RAM.
    with TemporaryDirectory(prefix="ffmpeg-progress-") as temp:
        progress_path = Path(temp) / "progress.txt"
        progress_path.touch()
        with progress_path.open(encoding="utf-8") as stream:
            pending = ""
            record = {}

            def poll():
                nonlocal pending
                pending += stream.read()
                lines = pending.split("\n")
                pending = lines.pop()
                for line in lines:
                    key, separator, value = line.partition("=")
                    if separator:
                        record[key] = value.strip()
                    if key == "progress":
                        tracker.processing_hook(record)
                        record.clear()

            return run_process([args[0], "-nostats", "-stats_period", "0.5", "-progress", str(progress_path),
                                *args[1:]], cancel, poll=poll)


def validate_audio_duration(path: Path, expected: float | None, cancel: threading.Event) -> float:
    actual = float(run_process([executable("ffprobe"), "-v", "error", "-show_entries",
                                "format=duration", "-of", "default=nw=1:nk=1", str(path)], cancel).strip())
    if not math.isfinite(actual) or actual <= 0:
        raise ValueError("Не удалось определить длительность аудио.")
    if isinstance(expected, (int, float)) and math.isfinite(expected) and expected > 0:
        if abs(actual - expected) > max(2, expected * 0.001):
            raise ValueError(f"Длительность аудио не совпадает с оригиналом: {actual:.0f} с вместо {expected:.0f} с. "
                             "Неполный файл не отправлен. Повтори запрос.")
    LOG.info("Длительность MP3 проверена: expected=%s actual=%.3f bytes=%s", expected, actual, path.stat().st_size)
    return actual


def probe_video(path: Path, cancel: threading.Event) -> tuple[dict, dict | None, float]:
    data = json.loads(run_process([executable("ffprobe"), "-v", "error", "-show_streams",
                                   "-show_format", "-of", "json", str(path)], cancel))
    streams = data.get("streams", [])
    video = next((s for s in streams if s.get("codec_type") == "video"
                  and not s.get("disposition", {}).get("attached_pic")), None)
    audio = next((s for s in streams if s.get("codec_type") == "audio"), None)
    if video is None:
        raise ValueError("В скачанном файле нет видеодорожки.")
    duration = float(video.get("duration") or data.get("format", {}).get("duration") or 0)
    if not math.isfinite(duration) or duration <= 0:
        raise ValueError("Не удалось определить длительность видео.")
    return video, audio, duration


def mp4_has_faststart(path: Path) -> bool:
    """Читаем только заголовки MP4, не загружая медиаданные в память."""
    length = path.stat().st_size
    found_index = False
    with path.open("rb") as media:
        while media.tell() + 8 <= length:
            start = media.tell()
            header = media.read(8)
            size = int.from_bytes(header[:4], "big")
            if size == 1:
                size = int.from_bytes(media.read(8), "big")
                minimum = 16
            else:
                minimum = 8
            if size < minimum or start + size > length:
                return False
            if header[4:] == b"moov":
                found_index = True
            if header[4:] == b"moof":
                return False
            if header[4:] == b"mdat":
                return found_index
            media.seek(start + size)
    return False


def prepare_video(path: Path, expected: float | None, cancel: threading.Event,
                  tracker: JobProgress | None = None) -> tuple[Path, dict]:
    """Готовим цельный MP4 H.264/AAC для медиатеки Android и iPhone."""
    video, audio, duration = probe_video(path, cancel)
    if isinstance(expected, (int, float)) and math.isfinite(expected) and expected > 0:
        if abs(duration - expected) > max(2, expected * 0.001):
            raise ValueError("Длительность видео не совпадает с оригиналом. Неполный файл не отправлен.")
    hdr = video.get("color_transfer") in {"smpte2084", "arib-std-b67"}
    copy_video = video.get("codec_name") == "h264" and video.get("pix_fmt") == "yuv420p" and not hdr
    copy_audio = audio is None or (audio.get("codec_name") == "aac" and audio.get("profile") == "LC")
    metadata = {"width": video["width"], "height": video["height"], "duration": round(duration)}
    if tracker:
        tracker.update(video_width=video["width"], video_height=video["height"])
    LOG.info("Проверка MP4: video=%s pixel_format=%s audio=%s duration=%.3f",
             video.get("codec_name"), video.get("pix_fmt"), audio.get("codec_name") if audio else "none", duration)
    if copy_video and copy_audio and mp4_has_faststart(path):
        return path, metadata
    if tracker:
        tracker.phase("processing", percent=0, processing_duration=duration,
                      note="🔄 Готовлю видео")
    output = path.with_name(path.stem + "_mobile.mp4")
    args = [executable("ffmpeg"), "-hide_banner", "-loglevel", "error", "-nostdin", "-y",
            "-threads", str(getattr(cfg, "VIDEO_THREADS", 1)), "-i", str(path),
            "-map", "0:v:0", "-map", "0:a:0?", "-map_metadata", "0"]
    if copy_video:
        args += ["-c:v", "copy"]
    else:
        filters = []
        if hdr:
            filters += ["zscale=t=linear:npl=100", "format=gbrpf32le", "zscale=p=bt709",
                        "tonemap=tonemap=hable:desat=0", "zscale=t=bt709:m=bt709:r=tv"]
        filters += ["pad=ceil(iw/2)*2:ceil(ih/2)*2", "format=yuv420p"]
        args += ["-vf", ",".join(filters), "-filter_threads", str(getattr(cfg, "VIDEO_THREADS", 1)),
                 "-c:v", "libx264", "-preset", "veryfast", "-crf", str(getattr(cfg, "VIDEO_CRF", 20)),
                 "-threads:v", str(getattr(cfg, "VIDEO_THREADS", 1)), "-pix_fmt", "yuv420p"]
        if hdr:
            args += ["-color_primaries", "bt709", "-color_trc", "bt709", "-colorspace", "bt709"]
    if copy_audio:
        args += ["-c:a", "copy"]
    else:
        args += ["-c:a", "aac", "-profile:a", "aac_low", "-b:a", "128k", "-ac", "2"]
    args += ["-tag:v", "avc1", "-movflags", "+faststart", str(output)]
    try:
        run_ffmpeg(args, cancel, tracker)
        if tracker:
            tracker.update(percent=99, eta=None, note="Проверяю готовое видео")
        result_video, result_audio, result_duration = probe_video(output, cancel)
        expected_width = video["width"] if copy_video else (video["width"] + 1) // 2 * 2
        expected_height = video["height"] if copy_video else (video["height"] + 1) // 2 * 2
        if (result_video.get("codec_name") != "h264" or result_video.get("pix_fmt") != "yuv420p"
                or result_video.get("width") != expected_width or result_video.get("height") != expected_height
                or (hdr and result_video.get("color_transfer") != "bt709")
                or (audio is not None and (result_audio is None or result_audio.get("codec_name") != "aac"
                                          or result_audio.get("profile") != "LC"))
                or abs(result_duration - duration) > max(0.5, duration * 0.001)
                or not mp4_has_faststart(output)):
            raise ValueError("Не удалось подготовить совместимое видео целиком. Файл не отправлен.")
        metadata.update(width=result_video["width"], height=result_video["height"], duration=round(result_duration))
        LOG.info("Подготовлен MP4 H.264/AAC: transcode_video=%s transcode_audio=%s bytes=%s duration=%.3f",
                 not copy_video, not copy_audio, output.stat().st_size, result_duration)
        output.replace(path)
        if tracker:
            tracker.update(percent=100, processed_seconds=result_duration, eta=None,
                           video_width=result_video["width"], video_height=result_video["height"],
                           note="Видео готово к отправке")
    except BaseException:
        output.unlink(missing_ok=True)
        raise
    return path, metadata


def prepare_upload(path: Path, cancel: threading.Event, tracker: JobProgress | None = None) -> Path:
    """Готовит один полный файл. MP3 подгоняется под лимит без обрезки записи."""
    limit = telegram_file_limit()
    if path.stat().st_size <= limit:
        return path
    if path.suffix != ".mp3":
        hint = "" if bot_api_url() else " Для большого файла целиком в исходном качестве нужен локальный сервер Bot API."
        raise ValueError(f"Видео размером {path.stat().st_size / 1_000_000:.1f} МБ превышает лимит "
                         f"{limit / 1_000_000:.1f} МБ. Выбери качество ниже." + hint)
    duration = float(run_process([executable("ffprobe"), "-v", "error", "-show_entries",
                                "format=duration", "-of", "default=nw=1:nk=1", str(path)], cancel).strip())
    if not math.isfinite(duration) or duration <= 0:
        raise ValueError("Не удалось определить длительность файла.")
    budget = limit * 0.96 * 8 / duration / 1000
    candidates = [rate for rate in (320, 256, 224, 192, 160, 128, 112, 96, 80, 64, 56, 48, 40, 32)
                  if cfg.MP3_MIN_BITRATE <= rate <= min(budget, int(cfg.MP3_BITRATE))]
    output = path.with_name(path.stem + "_full.mp3")
    for bitrate in candidates:
        if tracker:
            seconds = round(duration)
            tracker.phase("compressing", percent=0, processing_duration=duration,
                          note=f"Полная запись · {seconds // 60}:{seconds % 60:02} · {bitrate} кбит/с")
        run_ffmpeg([executable("ffmpeg"), "-hide_banner", "-loglevel", "error", "-nostdin", "-y",
                     "-i", str(path), "-map", "0:a:0", "-vn", "-map_metadata", "0", "-c:a", "libmp3lame",
                     "-b:a", f"{bitrate}k", str(output)], cancel, tracker)
        if 0 < output.stat().st_size <= limit:
            validate_audio_duration(output, duration, cancel)
            if tracker:
                tracker.update(percent=100, processed_seconds=duration, eta=None)
            LOG.info("Подготовлен цельный MP3: bitrate=%s bytes=%s duration=%.3f", bitrate, output.stat().st_size, duration)
            return output
        output.unlink(missing_ok=True)
    raise ValueError("Запись слишком длинная для цельного MP3 в пределах лимита Telegram "
                     f"при минимальном битрейте {cfg.MP3_MIN_BITRATE} кбит/с. Нужен локальный сервер Bot API.")


@dataclass
class Menu:
    owner: int
    url: str
    playlist: bool
    title: str
    choices: list[str]
    created: float = field(default_factory=time.monotonic)
    entries: tuple[str, ...] | None = None
    message_id: int | None = None
    info: dict | None = field(default=None, repr=False)


class State:
    def __init__(self):
        self.menus: dict[str, Menu] = {}
        self.jobs: dict[int, threading.Event] = {}
        self.progress: dict[int, ProgressReporter] = {}
        self.slots = asyncio.Semaphore(cfg.MAX_CONCURRENT_JOBS)
        self.broadcast_cancel: threading.Event | None = None

    def add_menu(self, menu: Menu) -> str:
        now = time.monotonic()
        for key, old in list(self.menus.items()):
            if now - old.created > cfg.MENU_TTL_SECONDS:
                del self.menus[key]
        own = [key for key, old in self.menus.items() if old.owner == menu.owner]
        for key in own[:max(0, len(own) - cfg.MAX_MENUS_PER_USER + 1)]:
            del self.menus[key]
        key = secrets.token_hex(6)
        self.menus[key] = menu
        return key


def state(context) -> State:
    return context.application.bot_data["state"]


@asynccontextmanager
async def job_slot(slots: asyncio.Semaphore, cancel: threading.Event):
    acquired = False
    try:
        while not cancel.is_set():
            try:
                await asyncio.wait_for(slots.acquire(), timeout=0.25)
                acquired = True
                break
            except asyncio.TimeoutError:
                continue
        if cancel.is_set():
            raise DownloadCancelled("Задача отменена пользователем")
        yield
    finally:
        if acquired:
            slots.release()


async def run_worker(function, *args, cancel: threading.Event):
    """Не удаляем временные файлы, пока фоновый поток ещё пишет в них."""
    worker = asyncio.create_task(asyncio.to_thread(function, *args))
    try:
        return await asyncio.shield(worker)
    except asyncio.CancelledError:
        cancel.set()
        try:
            await worker
        except Exception:
            pass
        raise


async def guard(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    user = update.effective_user
    if not user:
        raise ApplicationHandlerStop
    if not allowed(user.username):
        if update.callback_query:
            await update.callback_query.answer()
        if update.effective_message:
            await update.effective_message.reply_text("🔐 У вас нет доступа.")
        audit("access_denied", user)
        raise ApplicationHandlerStop
    if not update.effective_chat or update.effective_chat.type != "private":
        if update.callback_query:
            await update.callback_query.answer()
        if update.effective_message:
            await update.effective_message.reply_text("Напиши мне в личные сообщения.")
        raise ApplicationHandlerStop
    try:
        remember_user(user, update.effective_chat)
    except (OSError, sqlite3.Error) as error:
        LOG.error("Не удалось сохранить chat_id пользователя: %s", error_details(error))


HELP = ("Бот для скачивания видео из Youtube.\n\n"
        "Пришли ссылку на видео или плейлист. Выбери доступное качества и скачивай! "
        "Плейлист отправляется автоматически пакетами по 5 видео. "
        "MP3 отправляется целиком, а для больших записей уменьшается битрейт.\n\n"
        "Команды:\n"
        "/status — состояние текущей задачи\n"
        "/cancel — отменить текущую задачу\n/help — помощь")


async def start(update: Update, context) -> None:
    text = HELP + ("\n/all текст — отправить сообщение всем из users" if is_admin(update.effective_user.username) else "")
    await update.message.reply_text(text)


async def broadcast_all(update: Update, context) -> None:
    if not is_admin(update.effective_user.username):
        await update.effective_message.reply_text("🔐 У вас нет доступа.")
        return
    parts = (update.effective_message.text or "").split(maxsplit=1)
    if len(parts) < 2 or not parts[1].strip():
        await update.effective_message.reply_text("Напиши сообщение после команды:\n/all Текст сообщения")
        return
    text = parts[1].strip()
    if len(text) > 4096:
        await update.effective_message.reply_text("Сообщение должно быть не длиннее 4096 символов.")
        return
    current = state(context)
    if current.broadcast_cancel is not None:
        await update.effective_message.reply_text("Рассылка уже выполняется. Дождись результата.")
        return
    try:
        recipients, missing = broadcast_recipients()
    except (OSError, sqlite3.Error) as error:
        LOG.error("Не удалось прочитать участников рассылки: %s", error_details(error))
        await update.effective_message.reply_text("Не удалось прочитать чаты пользователей. Проверь logs/bot.log.")
        return
    cancel = current.broadcast_cancel = threading.Event()
    try:
        notice = await update.effective_message.reply_text(f"📣 Рассылка: получателей {len(recipients)}…")
        context.application.create_task(run_broadcast(update, context, text, recipients, missing, notice.message_id, cancel),
                                        update=update)
    except BaseException:
        current.broadcast_cancel = None
        raise


async def run_broadcast(update, context, text: str, recipients: list[tuple[str, int]], missing: list[str],
                        message_id: int, cancel: threading.Event) -> None:
    sent = failed = 0
    skipped = []
    audit("broadcast_started", update.effective_user, recipients=len(recipients), missing=missing, text_length=len(text))
    try:
        for username, chat_id in recipients:
            if cancel.is_set():
                break
            if not allowed(username):
                skipped.append(username)
                continue
            for attempt in range(3):
                try:
                    chat = await context.bot.get_chat(chat_id)
                    if (chat.type != "private" or not chat.username
                            or normalized_username(chat.username) != username or not allowed(chat.username)):
                        skipped.append(username)
                        break
                    if cancel.is_set():
                        break
                    await context.bot.send_message(chat_id=chat_id, text=text, parse_mode=None)
                    sent += 1
                    audit("broadcast_delivered", update.effective_user, recipient=username, chat_id=chat_id)
                    break
                except RetryAfter as error:
                    if attempt == 2:
                        failed += 1
                        audit("broadcast_failed", update.effective_user, recipient=username, error=error_details(error))
                        break
                    delay = error.retry_after
                    seconds = delay.total_seconds() if hasattr(delay, "total_seconds") else delay
                    await wait_retry(seconds + 1, cancel)
                except TelegramError as error:
                    # После неоднозначной сетевой ошибки не повторяем sendMessage:
                    # сообщение могло дойти, несмотря на потерянный ответ.
                    failed += 1
                    audit("broadcast_failed", update.effective_user, recipient=username, error=error_details(error))
                    LOG.warning("Ошибка рассылки пользователю @%s: %s", username, error_details(error))
                    break
        summary = ("⏹ Рассылка остановлена" if cancel.is_set() else "📣 Рассылка завершена")
        summary += f"\nОтправлено: {sent} · Ошибок: {failed}"
        if missing:
            summary += "\nНеизвестны чаты: " + ", ".join(f"@{name}" for name in missing)
            summary += "\nЭтим пользователям нужно написать боту /start."
        if skipped:
            summary += "\nПропущены (доступ или username изменён): " + ", ".join(f"@{name}" for name in skipped)
        await context.bot.edit_message_text(chat_id=update.effective_chat.id, message_id=message_id, text=summary)
        audit("broadcast_finished", update.effective_user, sent=sent, failed=failed, missing=missing, skipped=skipped)
    except DownloadCancelled:
        pass
    finally:
        if state(context).broadcast_cancel is cancel:
            state(context).broadcast_cancel = None


async def cancel_job(update: Update, context) -> None:
    cancel = state(context).jobs.get(update.effective_user.id)
    if cancel:
        cancel.set()
        reporter = state(context).progress.get(update.effective_user.id)
        if reporter:
            reporter.progress.update(cancel_requested=True)
        await update.message.reply_text("⏹ Отменяю задачу. Дождусь завершения текущего сетевого запроса или конвертации.")
    else:
        await update.message.reply_text("Сейчас нет активной задачи.")


async def status_job(update: Update, context) -> None:
    current = state(context)
    uid = update.effective_user.id
    reporter = current.progress.get(uid)
    if reporter:
        text = reporter.progress.render()
    elif uid in current.jobs:
        text = "🔎 Подготавливаю выбор формата…"
    else:
        text = "Сейчас нет активной задачи."
    await update.message.reply_text(text, parse_mode="HTML")


async def receive_url(update: Update, context) -> None:
    try:
        video, playlist = youtube_urls(update.message.text or "")
    except ValueError as error:
        await update.message.reply_text(str(error))
        return
    if video and playlist:
        key = state(context).add_menu(Menu(update.effective_user.id, playlist, True, "", []))
        await update.message.reply_text("Скачать видео или весь плейлист?", reply_markup=InlineKeyboardMarkup([
            [InlineKeyboardButton("🎬 Только видео", callback_data=f"scope:{key}:{video.rsplit('=', 1)[1]}")],
            [InlineKeyboardButton("📚 Весь плейлист", callback_data=f"scope:{key}:playlist")],
        ]))
        return
    await begin_inspect(update, context, playlist or video, bool(playlist))


async def begin_inspect(update, context, url: str, playlist: bool) -> None:
    current = state(context)
    uid = update.effective_user.id
    if uid in current.jobs:
        await update.effective_message.reply_text("У тебя уже есть задача. Дождись завершения или отправь /cancel.")
        return
    cancel = current.jobs[uid] = threading.Event()
    context.application.create_task(inspect_menu(update, context, url, playlist, cancel), update=update)


async def inspect_playlist_qualities(entries: tuple[str, ...], cancel: threading.Event,
                                    tracker: JobProgress) -> list[str]:
    common = set(QUALITY_LABELS)
    for index, entry in enumerate(entries, 1):
        if cancel.is_set():
            raise DownloadCancelled("Проверка плейлиста отменена")
        tracker.update(index=index, note=f"Проверяю доступные форматы · {index}/{len(entries)}")
        try:
            info = await run_worker(extract, entry, False, cancel, cancel=cancel)
        except DownloadCancelled:
            raise
        except Exception as error:
            raise ValueError(f"Не удалось проверить видео {index}/{len(entries)}. "
                             f"Меню не показано, чтобы не предлагать непроверенные качества. {user_error(error)}") from error
        if cancel.is_set():
            raise DownloadCancelled("Проверка плейлиста отменена")
        common.intersection_update(qualities(info))
    return [quality for quality in QUALITY_LABELS if quality in common] if entries else []


async def inspect_menu(update, context, url, playlist, cancel, message_id=None) -> None:
    current = state(context)
    try:
        queued = current.slots.locked()
        text = "⏳ Ожидаю завершения другой задачи…" if queued else "🔎 Получаю информацию…"
        if message_id is None:
            message = await update.effective_message.reply_text(text)
            message_id = message.message_id
        else:
            await context.bot.edit_message_text(chat_id=update.effective_chat.id, message_id=message_id,
                                                text=text)
        async with job_slot(current.slots, cancel):
            if cancel.is_set():
                return
            if queued:
                await context.bot.edit_message_text(chat_id=update.effective_chat.id, message_id=message_id,
                                                    text="🔎 Получаю информацию…")
            info = await run_worker(extract, url, playlist, cancel, cancel=cancel)
            entries = playlist_entries(info) if playlist else None
            if playlist and not entries:
                raise ValueError("Плейлист пуст или недоступен.")
            if playlist:
                tracker = JobProgress(title=info.get("title") or "Плейлист", total=len(entries), phase="info")
                reporter = ProgressReporter(context.bot, update.effective_chat.id, tracker,
                                            cfg.PROGRESS_UPDATE_INTERVAL, message_id)
                current.progress[update.effective_user.id] = reporter
                try:
                    await reporter.start()
                    choices = await inspect_playlist_qualities(entries, cancel, tracker)
                finally:
                    await reporter.stop()
                    current.progress.pop(update.effective_user.id, None)
            else:
                choices = qualities(info)
        if cancel.is_set():
            return
        if not choices:
            raise ValueError("YouTube не предоставил общих доступных форматов для всех видео плейлиста."
                             if playlist else "YouTube не предоставил доступных форматов для этого видео.")
        title = info.get("title") or "Без названия"
        key = current.add_menu(Menu(update.effective_user.id, url, playlist, title, choices,
                                    entries=entries, message_id=message_id,
                                    info=info if not playlist else None))
        buttons = [InlineKeyboardButton(QUALITY_LABELS[q], callback_data=f"get:{key}:{q}") for q in choices]
        text = f"{'📚 Плейлист' if playlist else '🎬 Видео'}: {title[:500]}\nВыбери формат:"
        if playlist:
            text += f"\nПроверено видео: {len(entries)}/{len(entries)}."
            text += "\nПоказаны только разрешения, доступные у каждого ролика."
            if "mp4" in choices:
                text += "\nMP4 · лучшее — максимальное качество отдельно для каждого видео."
        await context.bot.edit_message_text(chat_id=update.effective_chat.id, message_id=message_id, text=text,
                                            reply_markup=InlineKeyboardMarkup(
                                                [buttons[i:i + 2] for i in range(0, len(buttons), 2)]))
    except DownloadCancelled:
        pass
    except Exception as error:
        if cancel.is_set():
            return
        audit("inspection_failed", update.effective_user, url=url, error=redact(str(error)))
        if message_id is not None:
            await context.bot.edit_message_text(chat_id=update.effective_chat.id, message_id=message_id,
                                                text="❌ " + user_error(error))
        else:
            await update.effective_message.reply_text("❌ " + user_error(error))
    finally:
        try:
            if cancel.is_set() and message_id is not None:
                await context.bot.edit_message_text(chat_id=update.effective_chat.id, message_id=message_id,
                                                    text="⏹ Подготовка отменена.")
        except TelegramError as error:
            LOG.warning("Не удалось показать отмену подготовки: %s", error_details(error))
        finally:
            current.jobs.pop(update.effective_user.id, None)


async def callback(update: Update, context) -> None:
    query = update.callback_query
    parts = (query.data or "").split(":")
    current = state(context)
    if len(parts) != 3 or parts[0] not in {"scope", "get"}:
        await query.answer("Неизвестная кнопка.")
        return
    action, key, choice = parts
    menu = current.menus.get(key)
    if not menu or menu.owner != update.effective_user.id or time.monotonic() - menu.created > cfg.MENU_TTL_SECONDS:
        await query.answer("Меню устарело. Пришли ссылку ещё раз.", show_alert=True)
        return
    if update.effective_user.id in current.jobs:
        await query.answer("Дождись текущей задачи или отправь /cancel.", show_alert=True)
        return
    if action == "get" and choice not in menu.choices:
        await query.answer("Недоступный формат.", show_alert=True)
        return
    if action == "scope" and choice != "playlist" and not re.fullmatch(r"[A-Za-z0-9_-]{11}", choice):
        await query.answer("Некорректный выбор.")
        return
    # Резервируем задачу до первого await: повторный клик не создаст дубликат.
    cancel = current.jobs[update.effective_user.id] = threading.Event()
    try:
        await query.answer()
        await query.edit_message_reply_markup(reply_markup=None)
    except TelegramError:
        current.jobs.pop(update.effective_user.id, None)
        raise
    del current.menus[key]
    if menu.message_id is None:
        menu.message_id = getattr(update.effective_message, "message_id", None)
    if action == "scope":
        playlist = choice == "playlist"
        url = menu.url if playlist else f"https://www.youtube.com/watch?v={choice}"
        context.application.create_task(inspect_menu(update, context, url, playlist, cancel, menu.message_id), update=update)
    else:
        context.application.create_task(process_job(update, context, menu, choice, cancel), update=update)


def upload_timeout(file_size: int) -> float:
    # Общий бюджет учитывает размер файла; таймаут отдельной сетевой операции
    # остаётся UPLOAD_TIMEOUT, поэтому полностью зависшее соединение прерывается.
    expected = file_size / cfg.UPLOAD_MIN_BYTES_PER_SECOND + cfg.NETWORK_TIMEOUT
    return min(api_upload_timeout(), max(cfg.UPLOAD_TIMEOUT, expected))


async def wait_upload(operation, cancel: threading.Event | None, timeout: float | None = None,
                      tracker: JobProgress | None = None) -> None:
    task = asyncio.create_task(operation)
    deadline = time.monotonic() + (cfg.UPLOAD_TIMEOUT if timeout is None else timeout)
    log_at = time.monotonic() + 30
    try:
        while not task.done():
            if cancel and cancel.is_set():
                raise DownloadCancelled("Отправка отменена пользователем")
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise TimedOut("Превышено общее время отправки файла в Telegram")
            if tracker and time.monotonic() >= log_at:
                data = tracker.snapshot()
                LOG.info("Состояние отправки: bytes=%s/%s idle=%.1fs waiting_response=%s",
                         int(data.downloaded), int(data.size or 0),
                         time.monotonic() - data.upload_activity_at, data.upload_waiting)
                log_at = time.monotonic() + 30
            await asyncio.wait([task], timeout=min(0.5, remaining))
        await task
    finally:
        if not task.done():
            task.cancel()
        await asyncio.gather(task, return_exceptions=True)


def telegram_filename(title: str, extension: str) -> str:
    name = re.sub(r'[\\/:*?"<>|\x00-\x1f\x7f]', "_", title).strip(" .") or "video"
    name = name.encode("utf-8")[:240 - len(extension.encode("utf-8"))].decode("utf-8", errors="ignore").rstrip(" .")
    return (name or "video") + extension


class UploadReader:
    """Считаем байты, читаемые потоковым HTTP-клиентом, без загрузки файла в RAM."""

    def __init__(self, media, size: int, tracker: JobProgress, cancel: threading.Event | None):
        self.media, self.size, self.tracker, self.cancel = media, size, tracker, cancel
        self.reset()

    def reset(self) -> None:
        self.started = time.monotonic()
        self.tracker.update(downloaded=0, percent=0, speed=None, size=self.size,
                            upload_started_at=self.started, upload_activity_at=self.started, upload_waiting=False)

    def __getattr__(self, name):
        return getattr(self.media, name)

    def read(self, size=-1):
        if self.cancel and self.cancel.is_set():
            raise DownloadCancelled("Отправка отменена пользователем")
        chunk = self.media.read(size)
        now = time.monotonic()
        transferred = self.media.tell()
        if chunk:
            self.tracker.update(downloaded=transferred, percent=min(99, transferred / self.size * 100),
                                speed=transferred / max(now - self.started, 0.001), upload_activity_at=now,
                                upload_waiting=transferred >= self.size)
        return chunk


async def send_file(bot, chat_id: int, path: Path, title: str, caption: str,
                    cancel: threading.Event | None = None, *, tracker: JobProgress | None = None,
                    video_metadata: dict | None = None) -> None:
    attempts = cfg.TELEGRAM_UPLOAD_ATTEMPTS
    file_size = path.stat().st_size
    timeout = upload_timeout(file_size)
    for attempt in range(attempts):
        if cancel and cancel.is_set():
            raise DownloadCancelled("Отправка отменена пользователем")
        try:
            attempt_started = time.perf_counter()
            with path.open("rb") as media:
                stream = UploadReader(media, file_size, tracker, cancel) if tracker else media
                upload = InputFile(stream, filename=telegram_filename(title, path.suffix),
                                   read_file_handle=False)
                caption = caption.encode("utf-16-le")[:2000].decode("utf-16-le", errors="ignore")
                kwargs = dict(chat_id=chat_id, caption=caption, write_timeout=cfg.UPLOAD_TIMEOUT,
                              read_timeout=api_upload_timeout() if bot_api_url() else cfg.UPLOAD_TIMEOUT,
                              connect_timeout=cfg.NETWORK_TIMEOUT)
                LOG.info("Отправка в Telegram: chat_id=%s file=%s bytes=%s attempt=%s/%s timeout=%.1fs",
                         chat_id, path.name, file_size, attempt + 1, attempts, timeout)
                try:
                    if path.suffix == ".mp3":
                        await wait_upload(bot.send_audio(audio=upload, title=title[:200], **kwargs), cancel, timeout, tracker)
                    else:
                        await wait_upload(bot.send_video(video=upload, supports_streaming=True,
                                                         **(video_metadata or {}), **kwargs), cancel, timeout, tracker)
                except BadRequest as error:
                    if not any(word in str(error).lower() for word in ("video", "audio", "content", "media")):
                        raise
                    media.seek(0)
                    if tracker:
                        stream.reset()
                    await wait_upload(bot.send_document(document=upload, **kwargs), cancel, timeout, tracker)
            elapsed = time.perf_counter() - attempt_started
            if tracker:
                tracker.update(percent=100, upload_waiting=False)
            LOG.info("Файл отправлен в Telegram: chat_id=%s file=%s seconds=%.3f speed_mbps=%.3f",
                     chat_id, path.name, elapsed, file_size / max(elapsed, 0.001) / 1_000_000)
            return
        except RetryAfter as error:
            if attempt == attempts - 1:
                raise
            delay = error.retry_after
            seconds = (delay.total_seconds() if hasattr(delay, "total_seconds") else delay) + 1
            LOG.warning("Telegram ограничил частоту отправки: ожидание %s сек.", seconds)
            await wait_retry(seconds, cancel)
        except NetworkError as error:
            LOG.error("Ошибка отправки %s: %s", path.name, error_details(error))
            if attempt < attempts - 1 and can_retry_connection(error):
                await wait_retry(cfg.TELEGRAM_RETRY_DELAY * (attempt + 1), cancel)
                continue
            raise UploadError("Не удалось отправить файл в Telegram: " + user_error(error)) from error


async def wait_retry(seconds: float, cancel: threading.Event | None) -> None:
    deadline = time.monotonic() + seconds
    while time.monotonic() < deadline:
        if cancel and cancel.is_set():
            raise DownloadCancelled("Отправка отменена пользователем")
        await asyncio.sleep(max(0, min(0.5, deadline - time.monotonic())))


async def process_job(update, context, menu: Menu, quality: str, cancel: threading.Event) -> None:
    current = state(context)
    user = update.effective_user
    chat_id = update.effective_chat.id
    sent = failed = 0
    tracker = JobProgress(title=menu.title, quality=QUALITY_LABELS[quality],
                          total=len(menu.entries) if menu.entries is not None else (0 if menu.playlist else 1))
    reporter = ProgressReporter(context.bot, chat_id, tracker, cfg.PROGRESS_UPDATE_INTERVAL, menu.message_id)
    current.progress[user.id] = reporter
    audit("requested", user, url=menu.url, quality=quality, playlist=menu.playlist)
    try:
        await reporter.start()
        async with job_slot(current.slots, cancel):
            entries = [menu.url]
            if menu.playlist:
                if menu.entries is not None:
                    entries = menu.entries
                else:
                    tracker.phase("info")
                    info = await run_worker(extract, menu.url, True, cancel, cancel=cancel)
                    entries = playlist_entries(info)
                if not entries:
                    raise ValueError("В плейлисте нет доступных видео.")
            base = local_path(cfg.DOWNLOAD_DIR)
            base.mkdir(parents=True, exist_ok=True)
            batch_size = cfg.PLAYLIST_BATCH_SIZE
            tracker.update(total=len(entries), batches=math.ceil(len(entries) / batch_size))
            for offset in range(0, len(entries), batch_size):
                if cancel.is_set():
                    break
                batch = entries[offset:offset + batch_size]
                tracker.update(batch=offset // batch_size + 1)
                for index, url in enumerate(batch, offset + 1):
                    if cancel.is_set():
                        break
                    video_started = time.perf_counter()
                    timings = {}
                    try:
                        audit("download_started", user, url=url, quality=quality, playlist_url=menu.url if menu.playlist else None)
                        tracker.phase("info", index=index, video_title=menu.title if not menu.playlist else url,
                                      video_width=0, video_height=0, note="")
                        with TemporaryDirectory(prefix=f"{user.id}_", dir=base) as temp:
                            cached_info = (menu.info if not menu.playlist
                                           and time.monotonic() - menu.created <= cfg.METADATA_CACHE_TTL else None)
                            started = time.perf_counter()
                            try:
                                path, info = await run_worker(download, url, quality, Path(temp), cancel, tracker,
                                                              cached_info, cancel=cancel)
                            finally:
                                timings["download_prepare_seconds"] = time.perf_counter() - started
                            timings["postprocess_seconds"] = info.pop("_bot_postprocess_seconds", 0.0)
                            if cancel.is_set():
                                break
                            tracker.phase("processing", video_title=info.get("title") or info["id"])
                            started = time.perf_counter()
                            try:
                                prepared = await run_worker(prepare_upload, path, cancel, tracker, cancel=cancel)
                            finally:
                                timings["prepare_upload_seconds"] = time.perf_counter() - started
                            title = info.get("title") or info["id"]
                            video_metadata = info.get("_bot_video_metadata", {})
                            width = video_metadata.get("width") or info.get("width")
                            height = video_metadata.get("height") or info.get("height")
                            audit("downloaded", user, url=url, title=title, quality=quality,
                                  actual_width=width, actual_height=height, bytes=prepared.stat().st_size, parts=1,
                                  download_method=info.get("_bot_download_method", "direct"),
                                  duration_seconds=info.get("_bot_audio_duration") or
                                  info.get("_bot_video_metadata", {}).get("duration"))
                            if path != prepared:
                                path.unlink()
                            try:
                                if cancel.is_set():
                                    break
                                resolution = video_resolution({"width": width, "height": height})
                                actual = f" · {resolution}p" if resolution and quality != "mp3" else ""
                                if actual and width and height:
                                    actual += f" · {width}×{height}"
                                caption = f"{title[:700]}{actual}\n{url}"
                                tracker.phase("upload", size=prepared.stat().st_size)
                                audit("upload_started", user, url=url, bytes=prepared.stat().st_size)
                                started = time.perf_counter()
                                try:
                                    await send_file(context.bot, chat_id, prepared, title, caption, cancel, tracker=tracker,
                                                    video_metadata=info.get("_bot_video_metadata"))
                                finally:
                                    timings["upload_seconds"] = time.perf_counter() - started
                                prepared.unlink()
                            except TelegramError as error:
                                raise UploadError(user_error(error)) from error
                            if not cancel.is_set():
                                sent += 1
                                tracker.update(sent=sent)
                                audit("delivered", user, url=url, title=title, quality=quality, parts=1)
                    except Exception as error:
                        if cancel.is_set():
                            break
                        failed += 1
                        tracker.phase("error", note="", failed=failed, error=f"Видео {index}: {user_error(error)}")
                        audit("upload_failed" if isinstance(error, UploadError) else "download_failed",
                              user, url=url, quality=quality, error=error_details(error))
                        LOG.error("Ошибка видео %s: %s", url, error_details(error))
                    finally:
                        timings["total_seconds"] = time.perf_counter() - video_started
                        timings = {name: round(value, 3) for name, value in timings.items()}
                        audit("performance", user, url=url, quality=quality, **timings)
                        LOG.info("Время этапов %s: %s", url, timings)
                # Следующий пакет начинается только после отправки текущего.
        tracker.phase("cancelled" if cancel.is_set() else "done")
    except DownloadCancelled:
        tracker.phase("cancelled")
    except asyncio.CancelledError:
        cancel.set()
        tracker.phase("cancelled")
        raise
    except Exception as error:
        audit("job_failed", user, url=menu.url, error=redact(str(error)))
        tracker.phase("error", note="", error=user_error(error))
    finally:
        try:
            await reporter.finish()
            result = tracker.snapshot()
            if (menu.playlist and result.phase == "done" and not cancel.is_set()
                    and not failed and sent == result.total and sent > 0):
                try:
                    await context.bot.send_message(
                        chat_id=chat_id, parse_mode="HTML",
                        text=f"<b>✅ Готово</b>\nВсе видео ({sent}) из плейлиста отправлены")
                except TelegramError as error:
                    LOG.warning("Не удалось отправить уведомление о завершении плейлиста: %s", error_details(error))
        finally:
            audit("job_finished", user, url=menu.url, delivered=sent, failed=failed, cancelled=cancel.is_set())
            current.progress.pop(user.id, None)
            current.jobs.pop(user.id, None)


async def error_handler(update, context) -> None:
    LOG.error("Ошибка обработчика: %s", error_details(context.error),
              exc_info=(type(context.error), context.error, context.error.__traceback__))


def check_config() -> None:
    if not cfg.BOT_TOKEN or ":" not in cfg.BOT_TOKEN:
        raise ValueError("Укажи BOT_TOKEN в начале config.py.")
    administrator = getattr(cfg, "ADMIN_BOT", "rrojab")
    if not isinstance(administrator, str) or not re.fullmatch(r"@?[A-Za-z0-9_]{1,32}", administrator):
        raise ValueError("ADMIN_BOT должен содержать Telegram username администратора.")
    registry = getattr(cfg, "USERS_DB_PATH", "data/users.sqlite3")
    if not isinstance(registry, str) or not registry.strip():
        raise ValueError("USERS_DB_PATH должен содержать путь к базе chat_id пользователей.")
    for name in ("PLAYLIST_BATCH_SIZE", "MAX_CONCURRENT_JOBS", "TELEGRAM_FILE_LIMIT", "MAX_MENUS_PER_USER",
                 "TELEGRAM_UPLOAD_ATTEMPTS", "LOG_MAX_BYTES", "LOG_BACKUP_COUNT", "DOWNLOAD_CONCURRENT_FRAGMENTS"):
        value = getattr(cfg, name)
        if type(value) is not int or value <= 0:
            raise ValueError(f"{name} должен быть целым числом больше нуля.")
    for name in ("UPLOAD_TIMEOUT", "NETWORK_TIMEOUT", "PROCESS_TIMEOUT", "TELEGRAM_RETRY_DELAY", "MENU_TTL_SECONDS",
                 "PROGRESS_UPDATE_INTERVAL", "METADATA_CACHE_TTL", "UPLOAD_MAX_TIMEOUT", "UPLOAD_MIN_BYTES_PER_SECOND"):
        value = getattr(cfg, name)
        if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value) or value <= 0:
            raise ValueError(f"{name} должен быть конечным числом больше нуля.")
    for name, default in (("METADATA_TIMEOUT", 120), ("YOUTUBE_FALLBACK_TIMEOUT", 10)):
        value = getattr(cfg, name, default)
        if (isinstance(value, bool) or not isinstance(value, (int, float))
                or not math.isfinite(value) or value <= 0):
            raise ValueError(f"{name} должен быть конечным числом больше нуля.")
    if cfg.TELEGRAM_FILE_LIMIT > 50_000_000:
        raise ValueError("Для облачного Bot API TELEGRAM_FILE_LIMIT должен быть <= 50_000_000.")
    own_api = getattr(cfg, "BOT_API_URL", None)
    if own_api is not None:
        if not isinstance(own_api, str) or not own_api.strip():
            raise ValueError("BOT_API_URL должен быть адресом http(s) или None.")
        address = urlsplit(own_api)
        if (address.scheme not in {"http", "https"} or not address.hostname or address.username
                or address.password or address.query or address.fragment or address.hostname == "api.telegram.org"
                or address.path.rstrip("/").endswith(("/bot", "/file/bot"))):
            raise ValueError("Укажи корневой адрес собственного BOT_API_URL без /bot, токена и параметров. Для облака — None.")
        try:
            port = address.port
        except ValueError:
            raise ValueError("Некорректный порт BOT_API_URL.") from None
        if port is not None and not 1 <= port <= 65535:
            raise ValueError("Некорректный порт BOT_API_URL.")
        limit = telegram_file_limit()
        if type(limit) is not int or not 0 < limit <= 2_000_000_000:
            raise ValueError("BOT_API_FILE_LIMIT должен быть целым числом до 2_000_000_000.")
        timeout = api_upload_timeout()
        if (isinstance(timeout, bool) or not isinstance(timeout, (int, float)) or not math.isfinite(timeout)
                or timeout < cfg.UPLOAD_TIMEOUT):
            raise ValueError("BOT_API_UPLOAD_TIMEOUT должен быть конечным числом не меньше UPLOAD_TIMEOUT.")
    if cfg.UPLOAD_MAX_TIMEOUT < cfg.UPLOAD_TIMEOUT:
        raise ValueError("UPLOAD_MAX_TIMEOUT должен быть не меньше UPLOAD_TIMEOUT.")
    if type(cfg.MP3_MIN_BITRATE) is not int or not 32 <= cfg.MP3_MIN_BITRATE <= 320:
        raise ValueError("MP3_MIN_BITRATE должен быть целым числом от 32 до 320.")
    if not str(cfg.MP3_BITRATE).isdigit() or int(cfg.MP3_BITRATE) < cfg.MP3_MIN_BITRATE:
        raise ValueError("MP3_BITRATE должен быть числом не меньше MP3_MIN_BITRATE.")
    video_threads = getattr(cfg, "VIDEO_THREADS", 1)
    if type(video_threads) is not int or video_threads <= 0:
        raise ValueError("VIDEO_THREADS должен быть целым числом больше нуля.")
    video_crf = getattr(cfg, "VIDEO_CRF", 20)
    if type(video_crf) is not int or not 0 <= video_crf <= 51:
        raise ValueError("VIDEO_CRF должен быть целым числом от 0 до 51.")
    executable("ffmpeg")
    executable("ffprobe")
    check_js_runtime()
    if cfg.COOKIE_FILE and not local_path(cfg.COOKIE_FILE).is_file():
        raise ValueError("COOKIE_FILE не существует.")
    if not cfg.users:
        LOG.warning("Список users пуст: доступ закрыт для всех.")


class DownloadApplication(Application):
    async def stop(self) -> None:
        # Application.stop ждёт фоновые задачи. Перед ожиданием отменяем их,
        # чтобы Ctrl+C не заставлял доскачивать весь плейлист.
        current = self.bot_data.get("state")
        if current:
            for cancel in current.jobs.values():
                cancel.set()
            if current.broadcast_cancel:
                current.broadcast_cancel.set()
        await super().stop()


def build_application() -> Application:
    own_api = bot_api_url()
    httpx_options = {"trust_env": False}
    request = HTTPXRequest(connect_timeout=cfg.NETWORK_TIMEOUT,
                           read_timeout=cfg.NETWORK_TIMEOUT, write_timeout=cfg.UPLOAD_TIMEOUT,
                           media_write_timeout=cfg.UPLOAD_TIMEOUT, httpx_kwargs=httpx_options)
    polling_request = HTTPXRequest(connect_timeout=cfg.NETWORK_TIMEOUT,
                                   read_timeout=cfg.NETWORK_TIMEOUT, httpx_kwargs=httpx_options)
    builder = (Application.builder().application_class(DownloadApplication).token(cfg.BOT_TOKEN)
               .request(request).get_updates_request(polling_request))
    if own_api:
        builder = builder.base_url(own_api + "/bot").base_file_url(own_api + "/file/bot").local_mode(True)
    application = builder.build()
    application.bot_data["state"] = State()
    application.add_handler(TypeHandler(Update, guard), group=-1)
    application.add_handler(CommandHandler(["start", "help"], start))
    application.add_handler(CommandHandler("cancel", cancel_job))
    application.add_handler(CommandHandler("status", status_job))
    application.add_handler(CommandHandler("all", broadcast_all))
    application.add_handler(CallbackQueryHandler(callback))
    application.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, receive_url))
    application.add_handler(MessageHandler(filters.ALL, start))
    application.add_error_handler(error_handler)
    return application


async def logout_cloud() -> None:
    """Явная одноразовая миграция. Обычный запуск никогда не вызывает logOut."""
    if not cfg.BOT_TOKEN or ":" not in cfg.BOT_TOKEN:
        raise ValueError("Укажи BOT_TOKEN в config.py.")
    request = HTTPXRequest(connect_timeout=cfg.NETWORK_TIMEOUT, read_timeout=cfg.NETWORK_TIMEOUT,
                           httpx_kwargs={"trust_env": False})
    async with Bot(cfg.BOT_TOKEN, request=request) as cloud:
        if not await cloud.log_out():
            raise RuntimeError("Облачный Bot API не подтвердил logOut.")
    LOG.info("logOut выполнен. Можно запустить бота через собственный Bot API. Возврат в облако доступен через 10 минут.")


def main() -> None:
    setup_logging()
    if "--logout-cloud" in sys.argv[1:]:
        try:
            asyncio.run(logout_cloud())
        except Exception as error:
            raise SystemExit("Ошибка выхода из облачного API: " + error_details(error)) from None
        return
    try:
        check_config()
    except ValueError as error:
        raise SystemExit(str(error)) from None
    LOG.info("Бот запускается. Остановка: Ctrl+C")
    application = build_application()
    application.run_polling(allowed_updates=["message", "callback_query"], drop_pending_updates=True)


if __name__ == "__main__":
    main()
