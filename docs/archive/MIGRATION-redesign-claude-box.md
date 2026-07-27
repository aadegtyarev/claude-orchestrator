# Миграция: слияние `redesign/claude-box` → `main`

Чеклист для оператора при выкатке редизайна в прод. Составлен по аудиту
готовности (вердикт: ГОТОВО с оговорками, блокеров нет; 426 тестов зелёные на
Python 3.10 и 3.12, `ruff` чист). Деплой — решение оператора; этот документ — что
знать и проверить, а не команда катить.

## Кратко

Редизайн выносит кошелёк и launcher в автономные пакеты и добавляет standalone
CLI `claude-box`. **Поведение прод-оркестратора (Telegram + веб) сохранено 1:1** —
модуль `orchestrator/modules/wallet` стал тонким адаптером поверх `vault/`.
Ничего обязательного к настройке нет: без новых env всё работает как раньше.

## Что переехало (обратно совместимо)

- Кошелёк: `orchestrator/modules/wallet/{module,policy}.py` → тонкие адаптеры/
  ре-экспорты поверх пакета **`vault/`** (домен, демон, MITM-прокси, коннекторы,
  policy). Публичный API (`PolicyEditor`, команда `/wallet` в боте) не изменился.
- Launcher сессий: launch-механика из `orchestrator/core/sessions.py` → пакет
  **`box/`** (PTY-запуск, авто-диалоги, готовность «по тишине»).
- Новые исполняемые файлы: **`bin/claude-box`** (standalone-запуск claude в
  песочнице вне оркестратора), **`bin/vault`** (host-side CLI демона кошелька).
  `bin/wallet` (клиент в песочнице) — как был.

## `secrets.toml` — расширен обратно совместимо

- Новые ОПЦИОНАЛЬНЫЕ поля прокси-секретов: `connector` (напр. `generic-bearer`,
  `gdocs`) и `scope.url_prefixes` / `scope.ask_prefixes`. Старые секреты без них
  (host/inject/shared) работают как раньше (код читает `connector` с дефолтом "").
- Правки policy теперь идут под межпроцессным `flock` → рядом появляется файл
  **`secrets.toml.lock`** (пустой, 0600). Это нормально, не удалять на ходу.
- ASK-грант «навсегда» дописывает `scope.url_prefixes`; отзыв — `/wallet scope
  <имя> -<url>` (бот) или `vault policy scope <имя> -<url>` (CLI), либо правка
  файла. `rm secrets.toml` живой прокси трактует как отзыв доступа.

## Новые env (все опциональны, дефолты безопасны)

Для оркестратора (`.env`) — задокументированы в `.env.example`:
- `AGENT_VM_EGRESS_PROXY` / `AGENT_VM_EGRESS_CA` — egress гостя agent-vm на
  внешний прокси. **Вторая волна**: требуют форк agent-vm; на апстрим-бинаре
  (0.1.25) preflight ЧЕСТНО откажет при их установке. Не заданы → egress гостя
  MITM-ит сам agent-vm (как сейчас).
- `AGENT_VM_MEMORY_GIB` теперь разбирается как ЦЕЛОЕ (agent-vm дробные отвергал —
  кривое значение теперь ловится внятным отказом на старте, а не падением сессий).

Только для standalone `claude-box` (НЕ читается оркестратором, не в `.env`):
- `CLAUDE_BOX_HOME` — корень профилей claude-box (дефолт `~/.local/share/claude-box`).
- `AGENT_VM_*` (те же имена) — ресурсы/egress VM при запуске через `claude-box --vm`.

## Изменение в bwrap (прод-путь — без изменений)

`orchestrator/runners/bwrap.py`: реальный `~/.claude.json` биндится в песочницу
только когда `CLAUDE_CONFIG_DIR` НЕ задан явно. Для прод-оркестратора
`CLAUDE_CONFIG_DIR` задан всегда (`~/.claude-proxy`) → поведение байт-в-байт
прежнее; изменение защищает лишь профили claude-box (чужой config-dir не тащит
реальный `.claude.json` оператора). Покрыто тестом
`test_bwrap_claude_json_only_without_config_dir`.

## Осознанно отложено во вторую волну (в проде — честный отказ, не полуфича)

Оператор подтвердил отсрочку. Во всех случаях — код выхода 2/1 с объяснением,
никаких «полурабочих» путей («выключено = не существует»):
- `claude-box --profile --vm` и `--wallet --vm` → код 2 (agent-vm игнорирует
  `CLAUDE_CONFIG_DIR` и MITM-ит egress своим CA; F4/F1/F10).
- `AGENT_VM_EGRESS_PROXY/CA` без форка agent-vm → preflight-отказ.
- `claude-box connect` и live-OAuth коннекторов (`oauth_flow`/`resolve_scope`/
  `mint`/`refresh` у gdocs) → код 2 / честный `None` по контракту. Подкоманды
  `vault connect` не существует (не заявлена).

## Проверка после мержа (рекомендуется)

1. `PY=.venv/bin/python tests/run_all.sh` → `ALL TESTS OK`; `ruff check .` чисто.
2. Старт оркестратора без новых env — поведение кошелька/бота/веба как раньше.
3. Существующий `secrets.toml` читается, `/wallet` показывает секреты.
4. (Опц.) ASK-грант «навсегда» в боте и вебе рисует третью кнопку и пишет scope.

## Дизайн-контекст

`docs/ARCHITECTURE-claude-box.md`, `docs/DECISIONS-claude-box.md`,
`docs/FORK-agent-vm-egress-proxy.md` (форк `aadegtyarev/agent-vm`, ветка
`feat/wallet-env-and-egress-ca` — НЕ в апстриме, вторая волна),
`docs/secrets-wallet.md` (гайд кошелька для оператора).
