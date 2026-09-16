# LUMEN — Технологии Товарищества

LUMEN — закрытая owner edition и будущий преемник T-Mod Desktop. Пока продукт развивается, текущий Desktop остаётся самостоятельным стабильным клиентом.

## Изоляция

- отдельный app ID `lat.tvr.technology.lumen` и протокол `lumen://`;
- отдельные каталог данных, cookie-partition, настройки и installation credential;
- заголовок `X-TMod-Desktop-Edition: lumen` на каждом запросе;
- backend допускает только Discord ID из `TMOD_LUMEN_OWNER_IDS`;
- fail-closed значение по умолчанию — владелец `902235631952998410`;
- публичные Beta/Dev обновления T-Mod отключены;
- установщики собираются в `desktop/release-lumen/` и не публикуются в публичный репозиторий релизов.

## Сборка

```bash
cd desktop
pnpm install --frozen-lockfile
pnpm build:lumen
pnpm dist:lumen
```

Для Windows используется `electron-builder.lumen.yml`. Это отдельный профиль NSIS с собственным именем приложения и точкой расширения `resources/lumen/installer.nsh`. В следующем этапе стандартные bitmap-панели заменяются полностью кастомным анимированным bootstrapper — без изменения стабильного установщика T-Mod.

## Безопасная публикация

Исходный код хранится в приватном `cdnserver/t-mod`. Бинарный LUMEN нельзя загружать в публичный `cdnserver/t-mod-releases`: доступ к продукту в любом случае проверяется сервером, но приватный установщик должен оставаться приватным.
