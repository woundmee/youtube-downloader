# Постоянный запуск Python-бота

## XUSS runner

На XUSS используй встроенный менеджер `run`. Перед настройкой останови старый
процесс командой `run stop ИМЯ` и заверши ручной запуск через Ctrl+C.
Из каталога проекта выполни:

```bash
run python3 deploy/run_bot.py
run status
run logs ИМЯ
```

Имя процесса возьми из `run status`. Запускатель `deploy/run_bot.py` заменяет
свой процесс на `.venv/bin/python bot.py`, устанавливает рабочий каталог проекта
и добавляет `.venv/bin` в PATH. Так выбор Python не зависит от активации
окружения в SSH-сессии. Сохраняй каталог `deploy` внутри проекта.
Укажи абсолютные пути к FFmpeg и Node.js в `FFMPEG_DIR` и `JS_RUNTIME_PATH`.

После изменения кода или настроек выполни `run reload ИМЯ`. Согласно справке
XUSS, runner перезапускает процесс после сбоя и восстанавливает его после
перезагрузки. Если ранее был зарегистрирован запуск `run python3 bot.py`,
останови его и зарегистрируй команду с `deploy/run_bot.py` заново.

## systemd

Шаблон [youtube-downloader.service](../deploy/youtube-downloader.service)
рассчитан на Linux с systemd. Он запускает Python-бота после сетевой цели
`network-online.target` и службы локального Bot API. Сначала
[настрой локальный Bot API](bot-api.md) для отправки файлов до 2000 МБ.
При использовании облачного API (`BOT_API_URL = None`) убери
`telegram-bot-api.service` из строк `Wants=` и `After=` шаблона.

Подготовь виртуальное окружение и `config.py` по [руководству](guide.md#установка-и-запуск).
Проверь ручной запуск и останови его через Ctrl+C перед запуском службы.
Если проект находится в другом каталоге, замени пути в `WorkingDirectory`
и `ExecStart`. Выполни из каталога проекта от пользователя, который будет
запускать бота. У него должны быть права на чтение конфига и cookies и на
запись в каталоги скачивания, журналов и базы данных.

```bash
sed "s/^User=REPLACE_WITH_SERVICE_USER$/User=$(id -un)/" deploy/youtube-downloader.service | \
  sudo tee /etc/systemd/system/youtube-downloader.service >/dev/null
sudo chmod 0644 /etc/systemd/system/youtube-downloader.service
sudo systemctl daemon-reload
sudo systemd-analyze verify /etc/systemd/system/youtube-downloader.service
sudo systemctl enable --now youtube-downloader
sudo systemctl status youtube-downloader --no-pager
```

При неожиданном завершении systemd перезапускает процесс через 10 секунд.
Если исчерпан лимит перезапусков, проверь журнал, устрани причину и выполни
`sudo systemctl reset-failed youtube-downloader` перед повторным запуском.

```bash
sudo journalctl -u youtube-downloader -n 60 --no-pager
sudo journalctl -u youtube-downloader -f
sudo systemctl restart youtube-downloader
sudo systemctl stop youtube-downloader
```

Порог памяти службы составляет 768 МБ, жёсткий лимит — 1 ГБ вместе с дочерними
процессами FFmpeg и Node.js. При достижении лимита проверь расход памяти и журнал.
После перезапуска отправь ссылку заново, если скачивание прервалось.
Накопленные во время остановки сообщения отбрасываются при запуске.

## Обновление прежней установки на том же сервере

Если раньше использовалась служба `telegram-vpn.service`, отключи старые
службы перед установкой новых unit-файлов:

```bash
sudo systemctl disable --now youtube-downloader telegram-bot-api telegram-vpn
sudo rm -f /etc/systemd/system/telegram-bot-api.service.d/vpn.conf
sudo systemctl daemon-reload
```

Затем установи актуальные unit-файлы по инструкциям выше и запусти службы.
Для локального Bot API на том же сервере используй прежний
`/var/lib/telegram-bot-api`. При переносе на новый сервер останови старого бота
перед первым запуском нового с тем же токеном.
