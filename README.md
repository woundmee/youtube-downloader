<div align="center">
  <img src="docs/logo.png" width="176" alt="Логотип YouTube Downloader">


# YouTube Downloader

Telegram-бот для скачивания видео и аудио с YouTube.

<p>
  <img src="https://img.shields.io/badge/Python-3.12%2B-3776AB?style=for-the-badge&logo=python&logoColor=white" alt="Python 3.12+" />
  <img src="https://img.shields.io/badge/Telegram-Bot-26A5E4?style=for-the-badge&logo=telegram&logoColor=white" alt="Telegram Bot" />
  <img src="https://img.shields.io/badge/SQLite-Database-003B57?style=for-the-badge&logo=sqlite&logoColor=white" alt="SQLite" />
</p>

[Возможности](#features) •
[Использование](#usage) •
[Документация](#documentation) •

Отправь боту ссылку, выбери доступный формат и получи готовый файл прямо в Telegram.<br>
Бот работает в личных сообщениях и доступен только пользователям из списка доступа.
</div>
<br>

<a id="features"></a>

## ✨ Возможности

- **Видео и аудио.** Скачивание обычных роликов, Shorts и аудио в MP3.
- **Доступное качество.** Кнопки формируются по найденным потокам; для плейлиста
  предлагаются разрешения, доступные у каждого ролика.
- **Целые плейлисты.** Последовательная отправка видео пакетами по 5 с уведомлением после завершения.
- **Наглядный прогресс.** Этап, процент, скорость и оценка времени в одном сообщении.
- **Файлы для телефона и ПК.** Подготовка MP4 в H.264/AAC с сохранением разрешения.
- **Большие файлы.** Отправка видео и аудио до 2000 МБ через локальный Telegram Bot API.
- **Контроль доступа.** Список разрешённых пользователей и рассылка от администратора.
- **Порядок на сервере.** Очистка временных файлов и отдельный журнал действий.

<a id="usage"></a>

## ⚙️ Как пользоваться

1. Открой бота в Telegram и отправь `/start`.
2. Пришли ссылку на видео или плейлист.
3. Выбери формат и дождись отправки файлов.

| Команда | Назначение |
| --- | --- |
| `/start`, `/help` | Справка по работе с ботом |
| `/status` | Текущее состояние задачи |
| `/cancel` | Отмена текущей задачи |
| `/all текст` | Рассылка пользователям из списка доступа (только для администратора) |

<a id="documentation"></a>

## 📄 Документация

| Раздел | Что внутри |
| --- | --- |
| [Руководство](docs/guide.md) | Установка, настройки, cookies, журналы и проверка работы |
| [Запуск через systemd](docs/python-service.md) | Постоянная работа бота на Ubuntu-сервере |
| [Собственный Bot API](docs/bot-api.md) | Настройка локального Telegram Bot API для больших файлов |
| [Файлы развёртывания](deploy/) | Шаблоны служб systemd и настроек Bot API |

Приложение написано на Python и использует [yt-dlp](https://github.com/yt-dlp/yt-dlp), [python-telegram-bot](https://github.com/python-telegram-bot/python-telegram-bot) и
[FFmpeg](https://github.com/FFmpeg/FFmpeg).<br>Требования и команды установки — в [руководстве](docs/guide.md#установка-и-запуск).
Шаблон настроек — [config.example.py](config.example.py).
