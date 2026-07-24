"""Box — автономный launcher сессий Claude Code (Слой 2 редизайна).

Пакет БЕЗ зависимостей оркестратора (нет aiogram/Telegram, нет
orchestrator.*): PTY-запуск, авто-ответы на стартовые диалоги и ожидание
готовности «по тишине» — самодостаточная механика поднятия одной сессии
claude. Оркестратор — надстройка над этим пакетом: он клиент launcher'а через
тонкий адаптер, ровно как для кошелька (пакет `vault/`).

Фаза 3 редизайна (docs/ARCHITECTURE-claude-box.md §5, §11 «Launcher-extract:
пакет box/») выносит сюда launch-механику из orchestrator/core/sessions.py.
Самодостаточные хелперы без зависимостей от SessionManager/оркестратора:
  • ansi.py   — снятие ANSI-последовательностей (strip_ansi), чтобы пакет был
                автономен (не тянул orchestrator.core.ansi);
  • dialog.py — матчер стартовых диалогов экран→клавиши (_DialogAnswerer,
                _DIALOGS);
  • ready.py  — дедлайн готовности «по тишине» (_ReadyDeadline) + окна времени
                (READY_SILENCE_SEC, READY_TIMEOUT_MAX);
  • pty.py    — ядро PTY-запуска: open_pty (openpty + размер терминала) и
                драйвер (дренаж вывода + авто-ответы на стартовые диалоги),
                отдающий вывод наружу через колбэк on_output;
  • launch.py — композиция спавна: launch(argv, …)->LaunchHandle открывает PTY,
                спавнит готовую команду на slave (asyncio), запускает драйвер и
                возвращает ручки (process/pty_master/answerer/driver) вызывающему;
  • transcript_path.py — «конфиг клиента»: КУДА claude пишет транскрипт
                (профиль CLAUDE_CONFIG_DIR/$HOME + кодирование cwd), §5.2.
                Чистые функции resolve_config_dir/transcript_path.

launch пока принимает ГОТОВЫЕ argv/env/cwd — сборку argv/env, ожидание
готовности по /ping и resume/clear оркестратор делает сам (переедут в фасад
`launch(task)->Handle` следующими срезами вместе с CLI `claude-box`).
_DialogAnswerer/_ReadyDeadline/READY_* реэкспортятся из sessions.py для
обратной совместимости кода/тестов.
"""
