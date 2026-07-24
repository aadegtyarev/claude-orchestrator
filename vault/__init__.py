"""Vault — автономный кошелёк секретов (домен + демон + CLI).

Пакет БЕЗ зависимостей оркестратора (нет aiogram/Telegram): секреты, policy,
guard, редакция и транспорты живут здесь и работают без бота. Оркестратор —
клиент этого пакета через тонкий модуль-адаптер.

Фаза 1 редизайна (docs/ARCHITECTURE-claude-box.md) вынесла сюда домен и демон из
orchestrator/modules/wallet: Secret+guard (secret.py), редакция вывода
(redact.py), чтение secrets.toml (store.py), правка policy (policy.py), решение
policy (verdict.py), исполнение под секретом (execute.py), seam окружения
(host.py) и HTTP-демон секретов (daemon.py). Плюс host-side CLI (cli.py): `vault
serve` поднимает демон standalone (tty_host.py), `vault policy` правит secrets.toml
— пакет работает ПОЛНОСТЬЮ без оркестратора. Клиент внутри песочницы остаётся
stdlib-only bin/wallet (venv там недоступен).
"""
