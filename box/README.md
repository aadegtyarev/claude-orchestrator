# box — автономный launcher сессий Claude Code

Пакет поднимает одну сессию `claude` (или любой команды) под PTY: спавн,
авто-ответы на стартовые диалоги, ожидание готовности «по тишине», дренаж вывода.
Без зависимостей оркестратора (нет `aiogram`/Telegram, нет `orchestrator.*`) —
оркестратор является клиентом этого пакета через тонкий адаптер, ровно как для
кошелька (`vault/`). Слой 2 редизайна, см. `docs/ARCHITECTURE-claude-box.md` §5.

## Библиотека

Точка входа — `box.launch.launch`:

```python
from box.launch import launch

handle = await launch(
    argv,                      # готовая команда (напр. ["claude", "--session-id=…"])
    cwd=cwd, env=env,
    on_output=lambda chunk: ...,   # дренаж вывода процесса (bytes)
    rows=rows, cols=cols,
)
# handle: process (asyncio), pty_master (fd для stdin), answerer, driver_thread
code = await handle.process.wait()
handle.driver_thread.join(timeout=5)   # дослать буфер PTY и закрыть master
```

`launch` открывает PTY заданного размера (без него `claude` зондирует размер
через CPR, и под двойным PTY agent-vm ответы текут мусором в stdin), спавнит
процесс на slave-конце в своей process-группе и запускает поток-драйвер: он
дренирует вывод в `on_output` и печатает клавиши-ответы на стартовые диалоги.
Драйвер владеет master-fd и закрывает его сам, когда процесс закрыл PTY.

Модули: `pty.py` (open_pty, размеры терминала), `dialog.py` (авто-ответчик
стартовых диалогов), `ready.py` (готовность «по тишине» роста лога), `ansi.py`
(`strip_ansi`), `transcript_path.py` (путь транскрипта клиента).

## CLI `claude-box` (пакет `box_cli`)

`box_cli` — тонкий app-слой поверх `box` + Engine (`orchestrator.runners`):
собирает argv, заворачивает движком, отдаёт терминал. Запуск — `bin/claude-box`.

```
claude-box [--engine bwrap|off|agent-vm] [--vm] [--profile <имя>]
           [--wallet <секрет> [--secrets <файл>]] [-p <промпт>] [-- <аргументы claude>]
  --engine bwrap    файловая песочница bubblewrap (по умолчанию)
  --engine off      без изоляции
  --engine agent-vm | --vm   microVM через agent-vm (KVM)
  --profile <имя>   изолированная идентичность claude: свой CLAUDE_CONFIG_DIR и,
                    под bwrap, свой $HOME; реальные ~/.claude/~/.ssh скрыты
  --wallet <секрет> перехват под секрет: прокси-секрет → MITM TLS, host/inject →
                    PATH-шимы (git/gh/curl уходят на хост через кошелёк)
  -p <промпт>       unattended: без интерактива, вопросы кошелька → deny+log
  --               всё после — сквозные аргументы claude

Подкоманды: init <имя> (создать профиль), profile [rm <имя>] (список/удалить).
Не реализовано (следующий трек): connect (коннекторы Vault — нужен OAuth-флоу).
```

Границы движков честно проговорены в `--help` (напр. `--profile`/`--wallet` под
`--vm` отвергаются кодом 2: agent-vm игнорирует `CLAUDE_CONFIG_DIR` и MITM-ит
egress сам). Профили — `box_cli/profiles.py` (чистый stdlib), кошелёк —
`box_cli/wallet.py`, арбитр stdin для ASK/confirm в tty — `box_cli/tty.py`.

Окружение: `CLAUDE_BIN` (какой бинарь запускать, дефолт `claude`),
`CLAUDE_BOX_HOME` (корень профилей, дефолт `~/.local/share/claude-box`),
`AGENT_VM_*` под `--vm` (ресурсы/образ/egress VM — те же имена, что у
оркестратора). Все опциональны.

## Автономность

`box/` не импортирует `orchestrator.*` — проверяется тестом
`tests/box_autonomy_test.py` (walk_packages в свежем процессе). `box_cli` как
app-слой тянет `orchestrator.runners` (Engine, Слой 0) — это допустимо и
единственная его связь с оркестратором.

## Тесты

`tests/box_*_test.py`, `tests/runner_*_test.py`. Прогон — как весь проект:
`.venv/bin/python -m pytest -q` и `PY=.venv/bin/python tests/run_all.sh`.
