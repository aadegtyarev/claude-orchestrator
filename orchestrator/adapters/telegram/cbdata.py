"""Callback-данные inline-кнопок: сборка и разбор в одном месте.

Раньше форматы «префикс:поля» были размазаны по хендлерам ad-hoc `split(":")`
с разным числом аргументов (REVIEW.md D4); колоны внутри request_id — известная
мина (Claude Code генерирует id с ':'). Здесь каждый формат собирается и
разбирается парой функций, разбор терпим к мусору (None вместо исключения).

Лимит Telegram на callback_data — 64 байта; сборщики не проверяют его
(request_id короткий на практике), но формат держим компактным.
"""

from __future__ import annotations

import hashlib


def _parse_thread(data: str) -> int | None:
    """Разбор компактного формата `<префикс>:<thread_id>` → thread_id или None.

    Общий разбор для stop/esc/bg — все три однополевые и байт-в-байт совпадали.
    Правка формата (напр. второе поле) — теперь в одном месте."""
    try:
        return int(data.split(":", 1)[1])
    except (IndexError, ValueError):
        return None


def stop_cb(thread_id: int) -> str:
    return f"stop:{thread_id}"


def parse_stop(data: str) -> int | None:
    return _parse_thread(data)


def esc_cb(thread_id: int) -> str:
    return f"esc:{thread_id}"


def parse_esc(data: str) -> int | None:
    return _parse_thread(data)


def bg_cb(thread_id: int) -> str:
    return f"bg:{thread_id}"


def parse_bg(data: str) -> int | None:
    return _parse_thread(data)


def model_cb(thread_id: int, alias: str) -> str:
    return f"model:{thread_id}:{alias}"


def parse_model(data: str) -> tuple[int, str] | None:
    """(thread_id, alias) либо None."""
    try:
        _, thread_raw, alias = data.split(":", 2)
        return int(thread_raw), alias
    except ValueError:
        return None


def sess_cb(action: str, thread_id: int) -> str:
    return f"sess:{action}:{thread_id}"


def parse_sess(data: str) -> tuple[str, int] | None:
    """(action, thread_id) либо None."""
    try:
        _, action, thread_raw = data.split(":", 2)
        return action, int(thread_raw)
    except ValueError:
        return None


def delete_cb(thread_id: int, verdict: str) -> str:
    return f"del:{thread_id}:{verdict}"


def parse_delete(data: str) -> tuple[int, str] | None:
    """(thread_id, verdict) либо None."""
    try:
        _, thread_raw, verdict = data.split(":", 2)
        return int(thread_raw), verdict
    except ValueError:
        return None


def perm_cb(thread_id: int, request_id: str, behavior: str) -> str:
    return f"perm:{thread_id}:{request_id}:{behavior}"


def parse_perm(data: str) -> tuple[int, str, str] | None:
    """(thread_id, request_id, behavior) либо None.

    request_id может содержать ':' — behavior отрезаем с хвоста (rsplit),
    thread_id с головы, всё между ними — request_id как есть.
    """
    try:
        prefix, behavior = data.rsplit(":", 1)
        _, thread_raw, request_id = prefix.split(":", 2)
        return int(thread_raw), request_id, behavior
    except ValueError:
        return None


# ── кнопки под выводом команды терминала ─────────────────────────


def termint_cb(thread_id: int) -> str:
    """⏹ Прервать — послать Ctrl-C в bash-терминал топика."""
    return f"termint:{thread_id}"


def parse_termint(data: str) -> int | None:
    return _parse_thread(data)


def termrepeat_cb(thread_id: int) -> str:
    """↻ Повторить — выполнить последнюю команду снова."""
    return f"termrepeat:{thread_id}"


def parse_termrepeat(data: str) -> int | None:
    return _parse_thread(data)


def termhist_cb(thread_id: int) -> str:
    """🕘 История — показать меню последних команд (0 — страница)."""
    return f"termhist:{thread_id}"


def parse_termhist(data: str) -> int | None:
    return _parse_thread(data)


def termclose_cb(thread_id: int) -> str:
    """✖ Закрыть липкий терминал (/term off + открепить статус)."""
    return f"termclose:{thread_id}"


def parse_termclose(data: str) -> int | None:
    return _parse_thread(data)


def cmd_digest(cmd: str) -> str:
    """Короткий отпечаток команды для callback_data.

    Адресоваться по ПОЗИЦИИ в истории нельзя: повтор поднимает команду
    наверх, список перенумеровывается, а кнопки в уже отправленном меню
    остаются старыми — и кнопка с подписью «rm -rf build» запускала бы
    совсем другое. Отпечаток привязывает кнопку к самой команде.
    """
    return hashlib.sha256(cmd.encode()).hexdigest()[:16]


def termhistrun_cb(thread_id: int, cmd: str) -> str:
    """Выполнить команду из истории — адресуемся отпечатком, не позицией."""
    return f"termhistrun:{thread_id}:{cmd_digest(cmd)}"


def parse_termhistrun(data: str) -> tuple[int, str] | None:
    """(thread_id, отпечаток команды) либо None."""
    try:
        _, thread_raw, digest = data.split(":", 2)
        return int(thread_raw), digest
    except (IndexError, ValueError):
        return None


def profhist_cb(thread_id: int, keep: bool) -> str:
    """Ответ на вопрос «перенести историю при смене учётки».

    Имя профиля в callback_data НЕ кладём: лимит Telegram — 64 байта, а имя
    профиля бывает до 64 символов само по себе. Выбранное имя ждёт в памяти
    адаптера (см. _pending_profile), сюда попадает только да/нет.
    """
    return f"profhist:{thread_id}:{'1' if keep else '0'}"


def parse_profhist(data: str) -> tuple[int, bool] | None:
    """(thread_id, переносить ли историю) либо None."""
    try:
        _, thread_raw, keep = data.split(":", 2)
        return int(thread_raw), keep == "1"
    except (IndexError, ValueError):
        return None
