"""Липкий режим терминала: топик временно работает как шелл, а не как чат.

Зачем: писать `/bash` перед каждой командой — это не работа в терминале, а
диктовка команд по одной. `/term on` делает так, что обычные сообщения топика
уходят в его bash-оболочку, пока режим не выключат.

Чтобы режим не запирал топик, строка, начинающаяся с `>`, всё равно уходит
claude. Экранирование `>>` оставляет возможность отправить в шелл строку,
которая сама начинается с `>` (перенаправление вывода — `> out.txt`).

Состояние — только в памяти и только про UI: перезапуск оркестратора сбрасывает
режим, и это правильно. Липкий топик, переживший рестарт, был бы ловушкой —
оператор написал бы сообщение claude, а оно ушло бы в шелл.

Ключи те же, что у оболочек (OrchestratorCore.bash_key): "s:<сессия>:<scope>"
и "main:<scope>". В главном чате режим не включается (см. app.term_command):
там шелл идёт по хосту с правами оператора, и случайно отправленное сообщение
выполнилось бы на нём.
"""

from __future__ import annotations


class TerminalMode:
    """Какие топики сейчас работают в режиме терминала."""

    def __init__(self) -> None:
        self._on: set[str] = set()

    def is_on(self, key: str) -> bool:
        return key in self._on

    def turn_on(self, key: str) -> None:
        self._on.add(key)

    def turn_off(self, key: str) -> None:
        self._on.discard(key)

    def forget_session(self, name: str) -> None:
        """Снять режимы удалённой сессии — её оболочки уже закрыты.

        Сверяем с полным префиксом `s:<имя>:`, а не с началом имени: иначе
        удаление сессии `proj` погасило бы режим у `project`.
        """
        prefix = f"s:{name}:"
        self._on = {k for k in self._on if not k.startswith(prefix)}

    @staticmethod
    def split_escape(text: str) -> tuple[str, str]:
        """Куда адресовано сообщение в липком режиме: ("claude"|"shell", текст).

        `>` — уводит строку claude (и снимается), `>>` — экранирует сам `>`
        и оставляет строку шеллу.
        """
        if text.startswith(">>"):
            return "shell", text[1:]
        if text.startswith(">"):
            return "claude", text[1:].strip()
        return "shell", text
