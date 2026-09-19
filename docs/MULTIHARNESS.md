# Мульти-харнесс: один box, много адаптеров (design)

> Статус: предложение дизайна (19.09.2026, обсуждение с оператором).
> Связано: `ARCHITECTURE-claude-box.md` (слои 0–3), issue #133 (claude → stream-json).

## 1. Цель

Оркестратор управляет сессиями **любого** харнесса — claude, codex, opencode,
deepseek и далее — через единую абстракцию, не теряя ничего из UX: Telegram/Web,
статус-бабл, resume, permission-relay, история, скопы кошелька.

**Не-цели:**

- Agent SDK (Claude) — подписка, работаем только сырым CLI харнесса.
- Автоперенос сессии между харнессами (продолжить codex-сессию в claude).
- Один промт, N харнессов «для сравнения» в одной сессии.

## 2. Принцип: НЕ «бокс на харнесс», а «один box + адаптеры»

Вариант «codex-box, deepseek-box, …» отклонён: каждый такой бокс продублирует
Слой 0 (движки изоляции) и Слой 1 (Vault, shims, аудит, секреты) — а это общее
и это самая ценная часть. Дублировать её по числу харнессов — размножить
поверхность секретов и багов.

Вместо этого:

```
Слой 3  ОРКЕСТРАТОР     сессии, Telegram/Web, бабл, resume, permission-relay
                        = клиент адаптеров. Про креды и про протокол
                          харнесса НЕ знает.
        ────────────────────────────────────────────────────────────
Слой 2b HARNESS ADAPTERS box/harnesses/{claude,codex,opencode,deepseek}.py
                        общий интерфейс Harness: argv, конфиг-каталог учётки,
                        готовность, диалоги, транскрипт, статус, логин,
                        пермиссии, capabilities. Знает ОДИН харнесс.
        ────────────────────────────────────────────────────────────
Слой 2a LAUNCHER (box)   спавн, PTY/pipes/stdio, движок (runners), env, секреты
                        из Vault, дренаж вывода. Харнесс-агностик:
                        принимает готовый argv, не знает, чей он.
        ────────────────────────────────────────────────────────────
Слой 1  VAULT            без изменений (+ регистрация кред-путей харнессов,
                        напр. ~/.codex/auth.json — уже в wallet)
Слой 0  ENGINE           без изменений (bwrap | agent-vm | off)
```

Граница «знает про харнесс» проходит ВНУТРИ того, что сейчас box. Всё
claude-специфичное из box/ и sessions.py переезжает в `box/harnesses/claude.py`;
общий костяк (spawn, PTY-дрессура, готовность «по тишине», автономия) остаётся
в box/ и переиспользуется любым адаптером.

Правило автономии сохраняется и усиливается: `box/` не импортирует
`orchestrator.*` (существующий тест) — то же для `box/harnesses/`. Адаптер —
это знание о чужом бинаре, не о нашем UX.

## 3. Текущее состояние

Уже харнесс-агностично:

- `orchestrator/runners/` — движки изоляции (Protocol Runner: preflight/wrap/
  unique_cwd/supports_prefix);
- `box/launch.py` — принимает готовый argv, спавнит, отдаёт
  `LaunchHandle(process, pty_master, answerer, driver_thread)`;
- modelpipe — маршрутизация «модель → бэкенд» уже стоит на шве харнесс/бэкенд;
- `features()`/`COMMAND_FEATURE` — единый реестр доступных команд, UI уже умеет
  прятать нерабочее (правило «выключено = не существует»).

Зашито на claude (вынос в адаптер):

| Где | Что |
|---|---|
| box/dialog.py | автоответы стартовых диалогов TUI |
| box/ready.py | готовность «по тишине» роста claude.log |
| box/transcript_path.py | путь транскрипта |
| box/profiles.py | CLAUDE_CONFIG_DIR-профили учёток |
| box_cli/ | сборка argv claude |
| sessions.py | channel_server + originprompt (уйдёт по #133), парсинг /stats, /login, баннеры «Session limit»/«Login expired», алиасы моделей, trust, hookscript |

## 4. Внутренняя модель событий

С приходом stream-json (#133) и ACP харнесс перестаёт быть «дрессурой PTY» и
становится **потоком событий**. Внутреннюю модель берём ACP-образной (ACP —
открытый протокол, на нём уже сидят codex и агентный мир; claude — нет, его
маппит адаптер):

```
session/init(session_id, model, capabilities, tools)
status(text)                        # бабл
message(delta|block)                # текст ответа
tool_use(id, name, input)
tool_result(id, content, is_error)
permission_denied(tool_name, reason)
hook_started/hook_response          # опционально, где есть
result(subtype, is_error, usage, duration_ms)
```

Маппинг адаптеров:

| Харнесс | Транспорт | Маппинг |
|---|---|---|
| claude | stdio pipes (`-p --input-format stream-json …`) | почти 1:1 (проверено спайком #133) |
| codex | ACP через `codex app-server` | ~identity, ACP-события |
| opencode | свой server/CLI | спайк перед реализацией |
| deepseek | собственный харнесс — спайк | спайк перед реализацией |

Транспорт (PTY | pipes | WS) — деталь адаптера: у оркестратора сессия одна и та
же, независимо от того, что под ней.

## 5. Интерфейс Harness

```python
class Harness(Protocol):
    id: str
    capabilities: set[Capability]     # compact, usage, model, permissions,
                                      # banner_detect, subagents, ...
    # запуск
    def argv(self, spec: LaunchSpec, *, resume: bool) -> list[str]
    def config_dir(self, profile: str) -> Path
    # жизнь сессии
    def readiness(self, transport) -> Ready          # claude: тишина роста
                                                     # лога; ACP: init-кадр
    def dialogs(self) -> DialogMap                   # стартовые TUI-диалоги
    def transcript_path(self, cwd, session_id) -> Path | None
    def parse_stats(self, text) -> Stats | None      # /stats → структура
    def decode_events(self, stream) -> Iterator[Event]
    # операции
    def login(self, profile: str) -> LoginOutcome
    def permission_relay(self, request) -> RelayHandle | None
```

`LaunchSpec` (cwd, profile, engine, wallet-scope, rows/cols) — это то, что box
уже умеет собирать; адаптер лишь добавляет харнесс-специфику.

`Capability` управляет деградацией UI: нет `compact` у codex → кнопка/команда
скрыта; нет `usage` → /usage честно отвечает «не поддерживается этим
харнессом». Наполнение `features()` переносится с «мы это умеем» на
«текущий харнесс это умеет».

## 6. Матрица харнессов

| | claude | codex | opencode | deepseek |
|---|---|---|---|---|
| Транспорт | stream-json stdin/stdout (#133) | ACP (app-server) | спайк | спайк |
| Resume | `--resume <id>` (проверено) | ACP session | спайк | спайк |
| Пермиссии | `--permission-prompt-tool` (MCP, живьём не проверено) | ACP approvals | спайк | спайк |
| Auth | OAuth/API-key (профили) | `~/.codex/auth.json` (в wallet уже есть) | спайк | спайк |
| Готовность | system/init (проверено) | init-кадр ACP | спайк | спайк |

**Харнесс ≠ бэкенд.** deepseek/glm и т.п. — это и бэкенды, достижимые из ЛЮБОГО
харнесса через base_url (ds-профиль claude уже работает; modelpipe — этот шов).
deepseek, у которого есть собственный харнесс (CLI), — это ДВЕ строки матрицы:
бэкенд через claude/opencode/… и отдельный адаптер `deepseek.py`. Одно не
отменяет другого.

## 7. Порядок реализации

Правило двух: интерфейс замораживается только на ВТОРОЙ реализации. Одна —
домыслы, две — правда.

- **Ф0 (ближайшая):** #133 — claude с каналов на stream-json. Это выдавит из
  sessions.py шов SessionDriver (сейчас PTY вварен намертво) и заодно породит
  первый, claude-адаптер — фактически рефакторинг, а не новая фича.
- **Ф1:** оформить `box/harnesses/claude.py` из того, что Ф0 и так вынесла.
  Интерфейс НЕ фиксировать, писать в нём «для codex может отличаться».
- **Ф2:** `box/harnesses/codex.py` (ACP). Критерий готовности: сессия codex из
  Telegram с баблом, resume и пермиссиями; после этого — заморозить Harness.
- **Ф3:** opencode, deepseek — по одному, только по надобности; перед каждым —
  короткий спайк (транспорт, resume, пермиссии, auth) по образцу #133.

## 8. Правила

- Автономия: `box/` и `box/harnesses/` не импортируют `orchestrator.*`.
- Прозрачность (главное правило проекта): отклонено → модель получает ошибку,
  не молчание; недоступно → оператору честный отказ.
- «Выключено = не существует»: capabilities честные, деградация UI через
  существующий `features()`.
- Песочница: требования объявляет компонент (`MODULE_REQUIRES_SANDBOX` —
  харнессы объявляют свои).

## 9. Открытые вопросы

- `--permission-prompt-tool` claude живьём не проверен (из #133) — это
  reference-реализация permission_relay, с неё списываем интерфейс.
- ACP approvals codex — не проверены живьём.
- Что из PTY-механик (dialog/ready) останется после #133: диалоги нужны
  standalone claude-box, а не stream-json-сессиям — решить на Ф1.
- deepseek-харнесс: протокол, auth, семантика сессий — спайк перед Ф3.

## 10. Название бокса (предложение, ждёт решения оператора)

«claude-box» перестаёт быть честным именем, как только ядро харнесс-агностично.
Busybox-паттерн вместо одного имени на всё:

- **ядро** — один бинарь `harness-box` (предпочтительно; «harness» — уже наш
  термин; альтернатива `llm-box` милее, но сужает до LLM, а бокс запускает
  агентные CLI; `code-box` слишком общее). Внутренний пакет `box/` уже
  нейтрален — импорты не трогаем;
- **входы для людей** — per-harness имена как тонкие symlink/диспатч по argv[0]:
  `claude-box` остаётся и не ломается, новый харнесс = новый симлинк, а не
  новый бокс.

Механический свип имён (бинарь, ARCHITECTURE-claude-box.md, README,
AppImage-рецепт) — одной правкой в Ф1, не сейчас: пока харнесс один,
переименование заденет всё без выгоды.
