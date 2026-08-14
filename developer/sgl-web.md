# Изолированная разработка SGL Web

Репозиторий интерфейса: `https://github.com/cdnserver/t-mod-sgl-web` (private).

## Модель доступа

Второй разработчик получает доступ только к `t-mod-sgl-web`. Там находятся интерфейс, mock API, контракт и документация. Основной `t-mod`, база, Discord-бот, серверные модули и секреты ему не доступны.

Для приглашения нужен GitHub username разработчика:

```powershell
gh api -X PUT repos/cdnserver/t-mod-sgl-web/collaborators/USERNAME -f permission=push
```

Разработчик работает в `feature/*`, запускает `npm test` и передаёт Pull Request либо полный commit SHA.

## Проверка и импорт

Из корня основного T-Mod:

```powershell
python tools/import_sgl_web.py COMMIT_SHA --dry-run
python tools/import_sgl_web.py COMMIT_SHA
git diff -- web/sgl
python -m unittest tests.test_sgl_web_import tests.test_consensus_web
```

Импортируется только `web/sgl`. Команда не делает commit и не отправляет изменения: владелец сначала видит diff и тесты. Источник фиксируется в `web/sgl/.source.json`.

## Если нужен новый backend

Разработчик описывает в Pull Request требуемый endpoint, поля, права и состояния ошибок и может реализовать UI на mock-данных. Серверная реализация выполняется отдельно в основном репозитории. Так возможности интерфейса не ограничены, но граница доступа остаётся безопасной.
