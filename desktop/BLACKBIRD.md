# BLACKBIRD — Технологии Товарищества

BLACKBIRD — закрытая преемница T-Mod Desktop. Это отдельная редакция приложения с собственными данными, протоколом, сборкой и визуальным кодом. Публичный T-Mod Desktop продолжает работать независимо.

## Изоляция

- заголовок клиента: `X-TMod-Desktop-Edition: blackbird`;
- профиль Chromium: `persist:tt-blackbird-private-v1`;
- протокол: `blackbird://`;
- идентификатор приложения: `lat.tvr.technology.blackbird`;
- допуск: Discord ID из `TMOD_BLACKBIRD_OWNER_IDS`;
- прежний `TMOD_LUMEN_OWNER_IDS` остаётся резервным алиасом на период миграции.

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

Готовые артефакты появляются в `desktop/release-blackbird`. Публичный репозиторий релизов для этой редакции не используется.
