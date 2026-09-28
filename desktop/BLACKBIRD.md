# BLACKBIRD — Технологии Товарищества

BLACKBIRD — преемница T-Mod Desktop. Это отдельная редакция приложения с собственными данными, протоколом, сборкой и визуальным кодом. Публичный T-Mod Desktop продолжает работать независимо.

## Основа продукта

- главный Command Hub разделяет экосистему на два понятных контура: **Сенат** и **Atlas**;
- Сенат объединяет личный Reactor, Consensus, SGL, ОВР, задачи, игры и административный контур;
- Atlas объединяет интеллектуальный интерфейс и игровой Overlay;
- центр событий поддерживает одновременно карточки внутри Blackbird и нативные уведомления операционной системы;
- способ доставки и звук уведомлений настраиваются пользователем;
- Windows-установщик использует отдельные изображения и тексты Blackbird.

## Изоляция

- заголовок клиента: `X-TMod-Desktop-Edition: blackbird`;
- профиль Chromium: `persist:tt-blackbird-private-v1`;
- протокол: `blackbird://`;
- идентификатор приложения: `lat.tvr.technology.blackbird`;
- любой владелец действующего T-Mod аккаунта может войти в установленный Blackbird;
- отдельные сервисы по-прежнему проверяют свои права и глобальную блокировку;
- `TMOD_LUMEN_OWNER_IDS` относится только к прежнему LUMEN.

## Локальная сборка

```bash
cd desktop
pnpm install
pnpm test
pnpm typecheck
pnpm build:blackbird
pnpm package:blackbird
pnpm dist:blackbird
```

Готовые артефакты появляются в `desktop/release-blackbird`. Репозиторий выпусков Blackbird остаётся закрытым. Для бесшовных обновлений установщик, blockmap и `latest.yml` публикуются на сервере через `publish_blackbird_update_windows.ps1`; клиент скачивает их только после входа в T-Mod аккаунт. Уже открытые веб-сервисы обновляются на сервере без переустановки Blackbird. Версия p8 требует однократной ручной установки: выпущенная ранее p7 ещё не содержит этого обновлятора.

На Windows-сервере после проверки сборки:

```powershell
powershell -NoProfile -ExecutionPolicy Bypass -File .\publish_blackbird_update_windows.ps1
```

Скрипт сначала собирает приложение, затем проверяет файлы и последним атомарно заменяет `latest.yml` в `%USERPROFILE%\Documents\SGLDiscordBot\blackbird-updates` (или в `TMOD_PERSISTENT_DIR`). Сервер должен быть обновлён до версии с маршрутом `/api/desktop/v1/updates/blackbird/` до публикации p8. Только после успешной проверки маршрута можно задать `TMOD_BLACKBIRD_MIN_VERSION`.

Локальный визуальный стенд хаба доступен в dev/localhost-сборке по параметру `?hub-preview=1`. В установленной версии этот режим недоступен.
