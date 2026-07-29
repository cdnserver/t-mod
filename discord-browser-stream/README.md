# Discord Browser Client

Это отдельный пользовательский Discord-клиент для демонстрации браузера, а не
расширение стандартного Discord Bot API.

Контейнер:

1. авторизует выделенный тестовый пользовательский аккаунт;
2. запускает настоящий Chromium на виртуальном экране Xvfb;
3. захватывает выбранную вкладку вместе со звуком;
4. подключает аккаунт к voice-каналу;
5. передаёт поток как Discord Screen Share / Go Live.

Для транспорта используются `discord.js-selfbot-v13`,
`puppeteer-stream` и `@dank074/discord-video-stream`.

> Автоматизация обычного пользовательского аккаунта использует неофициальный
> протокол Discord. Сервис предназначен только для отдельного тестового
> аккаунта, на который есть явное разрешение. Обычный bot token T-Mod сюда не
> подходит и не должен использоваться.

## Работа внутри T-Mod

Штатный способ — запускать весь проект корневым `run_windows.bat`. Сервис
включается только при:

```env
BROWSER_STREAM_ENABLED=true
```

в постоянном файле:

```text
C:\Users\Admin\Documents\SGLDiscordBot\.env
```

При первом запуске рядом автоматически создаётся изолированный файл:

```text
C:\Users\Admin\Documents\SGLDiscordBot\browser-stream.env
```

Именно в нём необходимо задать:

```env
BROWSER_STREAM_DISCORD_TOKEN=токен_выделенного_тестового_аккаунта
BROWSER_STREAM_ALLOWED_USER_IDS=ваш_discord_id
```

Запускатор:

- проверяет, что user token не совпадает с токеном T-Mod;
- создаёт закрытый ключ управления между контейнерами;
- подключает Docker-профиль `browser-stream`;
- сохраняет Chrome-профиль в
  `C:\Users\Admin\Documents\SGLDiscordBot\browser-stream`;
- не публикует управляющий порт в интернет или локальную сеть.

Основной бот лишь передаёт команды из `/screen` во внутренний API.
`browser-stream.env` подключается только к контейнеру эмулируемого клиента,
поэтому основной контейнер не получает user token.

## Управление

Через T-Mod доступна команда `/screen`:

- **Запустить или сменить страницу** — тестовый аккаунт входит в voice-канал
  управляющего и начинает демонстрацию;
- **Показать состояние** — аккаунт, страница, канал и стадия запуска;
- **Остановить трансляцию** — завершить поток, оставаясь в voice;
- **Остановить и выйти из голоса** — полностью завершить сессию.

Доступ получают администраторы сервера, пользователи с `Manage Server` и роль
из `BROWSER_STREAM_ALLOWED_ROLE_ID`.

Исходные текстовые команды эмулируемого аккаунта также сохранены как резерв:

```text
!screen start https://example.org
!screen status
!screen stop
!screen leave
```

Их могут отправлять только ID из `BROWSER_STREAM_ALLOWED_USER_IDS`. При
необходимости команды ограничиваются одним текстовым каналом через
`BROWSER_STREAM_COMMAND_CHANNEL_ID`.

## Самостоятельный запуск

Для диагностики сервис можно запустить без остального T-Mod:

```bash
cd discord-browser-stream
cp .env.example .env
docker compose up -d --build
docker compose logs -f
```

В самостоятельном режиме также заполните
`BROWSER_STREAM_CONTROL_TOKEN` длинным случайным значением. Оно защищает
локальный API, даже если основной бот к нему не обращается.

## Настройки

Обязательные:

- `BROWSER_STREAM_DISCORD_TOKEN` — user token тестового аккаунта;
- `BROWSER_STREAM_ALLOWED_USER_IDS` — Discord ID операторов через запятую;
- `BROWSER_STREAM_CONTROL_TOKEN` — внутренний ключ управления.

Цель и поведение:

- `BROWSER_STREAM_GUILD_ID` — сервер по умолчанию;
- `BROWSER_STREAM_VOICE_CHANNEL_ID` — voice-канал по умолчанию;
- `BROWSER_STREAM_START_URL` — начальная/последняя страница;
- `BROWSER_STREAM_AUTO_START` — автоматически начать после входа;
- `BROWSER_STREAM_COMMAND_PREFIX` — резервный префикс, по умолчанию `!screen`.

Качество:

- `BROWSER_STREAM_WIDTH`, `BROWSER_STREAM_HEIGHT`;
- `BROWSER_STREAM_FPS`;
- `BROWSER_STREAM_BITRATE_KBPS`;
- `BROWSER_STREAM_MAX_BITRATE_KBPS`;
- `BROWSER_STREAM_PAGE_LOAD_TIMEOUT_MS`;
- `BROWSER_STREAM_CAPTURE_STARTUP_DELAY_MS` — ожидание загрузки расширения
Chromium перед открытием трансляции, по умолчанию `1250`;
- `BROWSER_STREAM_CAPTURE_FOCUS_DELAY_MS` — пауза после активации вкладки,
  по умолчанию `250`;
- `BROWSER_STREAM_BROWSER_LAUNCH_ATTEMPTS` — число попыток запуска Chromium
  после аварийного завершения, по умолчанию `2`;
- `BROWSER_STREAM_VOICE_CONNECT_TIMEOUT_MS` — максимальное ожидание
  voice-handshake, по умолчанию `20000`;
- `BROWSER_STREAM_CHROMIUM_LOGS` — подробный stderr Chromium для временной
  диагностики, по умолчанию `false`.

Перед каждым запуском сервис удаляет только устаревшие служебные блокировки
профиля Chromium, не затрагивая cookies и настройки. Если основной профиль
повреждён, повторная попытка использует одноразовый изолированный профиль и
автоматически включает диагностический вывод Chromium.

Профиль Chromium хранится в `BROWSER_STREAM_PROFILE_DIR` (`/data/chrome`
внутри штатного контейнера). Cookies и токены не следует помещать в образ или
Git.

## Диагностика

Из корня T-Mod:

```bash
docker compose --profile browser-stream ps
docker compose --profile browser-stream logs --tail=200 discord-browser-stream
```

Статусы:

- `starting` — вход в voice, запуск Chromium и подготовка кодера;
- `streaming` — медиапоток передаётся;
- `idle` — активной демонстрации нет;
- `unhealthy` — клиент не вошёл в Discord или внутренний API не запустился.

При аварии Chromium увеличьте `shm_size`. Если сервер или аккаунт не
поддерживает выбранное качество, уменьшите разрешение, FPS и битрейт.

Исходный транспорт:
[Discord-RE/Discord-video-stream](https://github.com/Discord-RE/Discord-video-stream).
