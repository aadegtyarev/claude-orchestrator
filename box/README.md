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

Флаги, профили, работа с кошельком и границы движков — в руководстве
[`docs/BOX.md`](../docs/BOX.md); здесь не дублируем, чтобы описание команды не
разъехалось в двух местах. Устройство: `box_cli/cli.py` (разбор аргументов и
сборка запуска), `box_cli/profiles.py` (профили, чистый stdlib),
`box_cli/wallet.py` (перехват под секрет), `box_cli/tty.py` (арбитр stdin —
единственный владелец fd, через него идут вопросы кошелька в tty).

## Автономность

`box/` не импортирует `orchestrator.*` — проверяется тестом
`tests/box_autonomy_test.py` (walk_packages в свежем процессе). `box_cli` как
app-слой тянет `orchestrator.runners` (Engine, Слой 0) — это допустимо и
единственная его связь с оркестратором.

## Тесты

`tests/box_*_test.py`, `tests/runner_*_test.py`. Прогон — как весь проект:
`.venv/bin/python -m pytest -q` и `PY=.venv/bin/python tests/run_all.sh`.
