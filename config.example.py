"""Все секреты и настройки бота. На prod переименуй файл в config.py и обнови параметры!"""

BOT_TOKEN = "TELEGRAM_BOT_TOKEN"  # нужно создать бота и получить его токен через @botfather.
users = ["username1, username2"]  # Можно с @ или без; регистр не учитывается.
ADMIN_BOT = "admin_username"  # Выполнение команды [/all message].

DOWNLOAD_DIR = "downloads"
LOG_DIR = "logs"
USERS_DB_PATH = "data/users.sqlite3"  # Путь до БД
PLAYLIST_BATCH_SIZE = 5  # При скачивании плейлиста обработка по 5 видео за "пачку".
MAX_CONCURRENT_JOBS = 1
DOWNLOAD_CONCURRENT_FRAGMENTS = 4  # Параллельные фрагменты DASH/HLS одного ролика.
YTDLP_CACHE_DIR = ".cache/yt-dlp"
METADATA_CACHE_TTL = 120  # Повторно использовать свежие метаданные после выбора формата.

TELEGRAM_FILE_LIMIT = 49_000_000  # Запас относительно облачного лимита 50 MB.
BOT_API_URL = "http://127.0.0.1:8081"  # Адрес к локальному BotAPI. Позволяется скачивать до 2ГБ. None — облачный API (до 50 МБ).
BOT_API_FILE_LIMIT = 1_950_000_000  # Лимит для собственного API с --local (т.е. 1.95 ГБ)
BOT_API_UPLOAD_TIMEOUT = 7200  # Общее ожидание отправки через собственный API.

MP3_BITRATE = "128"  # Битрейт аудио. Около 22 МБ на 23 минуты аудио.
MP3_MIN_BITRATE = 64  # Минимальный битрейт цельного MP3 под лимит Telegram.
MENU_TTL_SECONDS = 3600
MAX_MENUS_PER_USER = 10
PROGRESS_UPDATE_INTERVAL = 3  # Период обновления одного сообщения прогресса, в секундах.
NETWORK_TIMEOUT = 30
UPLOAD_TIMEOUT = 180  # Базовое время отправки и предел ожидания отдельной сетевой операции.
UPLOAD_MAX_TIMEOUT = 900  # Верхний предел общего времени для больших файлов.
UPLOAD_MIN_BYTES_PER_SECOND = 64_000  # Ожидаемая минимальная скорость для расчёта бюджета.
TELEGRAM_UPLOAD_ATTEMPTS = 3  # Повторы при ошибке соединения до передачи файла.
TELEGRAM_RETRY_DELAY = 2
PROCESS_TIMEOUT = 3600
LOG_MAX_BYTES = 10_000_000
LOG_BACKUP_COUNT = 5

COOKIE_FILE = "data/cookies.txt"  # Путь к cookies.txt в формате Netscape, если нужен вход.
FFMPEG_DIR = None  # Путь до каталога с ffmpeg и ffprobe, если их нет в PATH (если скачал/установил в другое место). 
JS_RUNTIME = "node"  # Node.js >=22; также поддерживается "deno".
JS_RUNTIME_PATH = "/opt/youtube-downloader/runtime/node22/bin/node"  # Полный путь к исполняемому файлу, если его нет в PATH.

