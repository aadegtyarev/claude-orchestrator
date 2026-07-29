"""История команд терминала — то, что в настоящем шелле даёт «стрелка вверх».

В чате стрелок нет: клавиатура Telegram про них не знает. Поэтому историю
держим у себя, и она превращается в меню последних команд (ткнул — выполнил
заново) и в подсказку при опечатке: «ошибся в команде» лечится выбором прошлой
вместо перенабора с телефона.

Повтор не дублирует запись, а поднимает её наверх — иначе список из десяти
`pytest` подряд был бы бесполезен. Хранение только в памяти: команды терминала
живут ровно столько, сколько сам терминал, а класть их на диск значило бы
пережить рестарт вместе с оболочкой, которой уже нет.

Ключи те же, что у оболочек (OrchestratorCore.bash_key).
"""

from __future__ import annotations

DEFAULT_LIMIT = 10


class TermHistory:
    """Последние команды каждого терминала, свежие первыми."""

    def __init__(self, limit: int = DEFAULT_LIMIT) -> None:
        self._limit = limit
        self._by_key: dict[str, list[str]] = {}

    def add(self, key: str, cmd: str) -> None:
        """Запомнить команду. Пустая — не история, а мусор; такие пропускаем."""
        cmd = cmd.strip()
        if not cmd:
            return
        items = self._by_key.setdefault(key, [])
        # Повтор поднимаем наверх, а не дублируем: десять одинаковых
        # `pytest` подряд вытеснили бы из списка всё остальное.
        if cmd in items:
            items.remove(cmd)
        items.insert(0, cmd)
        del items[self._limit:]

    def last(self, key: str, count: int = DEFAULT_LIMIT) -> list[str]:
        """Последние команды терминала, свежие первыми."""
        return self._by_key.get(key, [])[:count]

    def forget_session(self, name: str) -> None:
        """Забыть историю удалённой сессии.

        Сверяем полный префикс `s:<имя>:`, а не начало имени: удаление сессии
        `proj` не должно стирать историю у `project`.
        """
        prefix = f"s:{name}:"
        self._by_key = {
            k: v for k, v in self._by_key.items() if not k.startswith(prefix)
        }
