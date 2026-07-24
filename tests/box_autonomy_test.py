"""Пакет `box/` автономен — доказательство, что launcher (Слой 2 редизайна
claude-box) не тянет оркестратор. Импортируем ВСЕ подмодули box в свежем
процессе и проверяем, что `orchestrator` не затянут в sys.modules.

По образцу tests/vault_domain_test.py::test_no_orchestrator_dependency: под
`pytest` все тест-модули делят один процесс, где orchestrator уже загружен
соседями (sessions/wallet-тесты), поэтому честная проверка автономности — только
в отдельном интерпретаторе.

Запуск: .venv/bin/python tests/box_autonomy_test.py
"""
import subprocess
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))


def test_no_orchestrator_dependency():
    """box импортируется в СВЕЖЕМ интерпретаторе, НЕ затягивая orchestrator."""
    root = str(Path(__file__).parent.parent)
    # Импортируем ВСЕ подмодули box (pkgutil.walk_packages) — автономность
    # обязана держаться для всего пакета, включая будущие модули, без правки теста.
    code = (
        f"import sys; sys.path.insert(0, {root!r});"
        "import importlib, pkgutil, box;"
        "[importlib.import_module(m.name) for m in pkgutil.walk_packages(box.__path__, 'box.')];"
        "leaked=[m for m in sys.modules if m=='orchestrator' or m.startswith('orchestrator.')];"
        "sys.exit(1 if leaked else 0)"
    )
    r = subprocess.run([sys.executable, "-c", code], capture_output=True, text=True)
    assert r.returncode == 0, (
        "box затянул orchestrator в свежем процессе:\n"
        f"stdout={r.stdout}\nstderr={r.stderr}"
    )
    print("OK box автономен: свежий процесс импортит box без orchestrator")


def main():
    test_no_orchestrator_dependency()
    print("ALL BOX-AUTONOMY OK")


if __name__ == "__main__":
    main()
