# Локальный Telegram Bot API на Linux

Бот скачивает медиа напрямую с YouTube. Запросы Python-бота идут на
`http://127.0.0.1:8081`, а локальный `telegram-bot-api` подключается к Telegram
по обычной сети сервера. На сервере нужен исходящий доступ к YouTube,
`googlevideo.com` и Telegram. Порт 8081 слушает только loopback.

Официальный сервер Telegram Bot API в режиме `--local` поддерживает отправку
файлов до 2000 МБ. При `BOT_API_URL = None` Python-бот использует
`https://api.telegram.org`, где лимит отправки составляет 50 МБ.
[Документация Telegram](https://core.telegram.org/bots/api#using-a-local-bot-api-server).

## Установка через systemd

Собери `telegram-bot-api` для своего дистрибутива по
[официальной инструкции](https://tdlib.github.io/telegram-bot-api/build.html).
Из каталога проекта выполни команды, подставив путь к собранному бинарнику.
Если служебный пользователь уже существует, пропусти `useradd`.

```bash
sudo install -m 755 /PATH/TO/telegram-bot-api /usr/local/bin/telegram-bot-api
sudo useradd --system --user-group --home-dir /var/lib/telegram-bot-api --shell /usr/sbin/nologin telegram-bot-api
sudo touch /etc/telegram-bot-api.env
sudo chown root:root /etc/telegram-bot-api.env
sudo chmod 600 /etc/telegram-bot-api.env
sudoedit /etc/telegram-bot-api.env
```

Заполни `TELEGRAM_API_ID` и `TELEGRAM_API_HASH` по примеру
[telegram-bot-api.env.example](../deploy/telegram-bot-api.env.example).
Эти данные получают через [my.telegram.org](https://core.telegram.org/api/obtaining_api_id).
Токен бота остаётся в `config.py`.

Служба использует `/var/lib/telegram-bot-api` как каталог данных. Если локальный
API уже работал с этим ботом, останови тот экземпляр. При переносе на другой
сервер следуй [инструкции Telegram по переносу между локальными серверами](https://github.com/tdlib/telegram-bot-api#moving-a-bot-from-one-local-server-to-another).
Каталог перенесённых данных должен принадлежать пользователю `telegram-bot-api`.

```bash
sudo install -m 644 deploy/telegram-bot-api.service /etc/systemd/system/telegram-bot-api.service
sudo systemctl daemon-reload
sudo systemctl enable --now telegram-bot-api
sudo systemctl status telegram-bot-api --no-pager
sudo journalctl -u telegram-bot-api -n 50 --no-pager
```

## Подключение бота

В `config.py` укажи:

```python
BOT_API_URL = "http://127.0.0.1:8081"
BOT_API_FILE_LIMIT = 1_950_000_000
BOT_API_UPLOAD_TIMEOUT = 7200
```

`BOT_API_FILE_LIMIT` оставляет запас относительно 2000 МБ. Файл передаётся
потоком с диска. Предусмотри место для временных файлов приложения и данных
локального Bot API.

### Одноразовый переход из облака

Если бот сейчас работает через `api.telegram.org`, останови все его запущенные
экземпляры и один раз выполни из каталога проекта:

```bash
.venv/bin/python bot.py --logout-cloud
```

Команда обращается к `api.telegram.org`. После успешного `logOut` вернуться
в облачный API можно через 10 минут. Для бота, который уже работает через
локальный API, этот шаг пропусти. [Метод logOut](https://core.telegram.org/bots/api#logout).

Запусти бота вручную командой `.venv/bin/python bot.py` или
[настрой запуск через systemd](python-service.md). Проверь `/start`, отправку
небольшого видео и отправку файла больше 50 МБ.
