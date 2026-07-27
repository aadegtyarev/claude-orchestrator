#!/usr/bin/env bash
# Установка claude-orchestrator: venv, зависимости, systemd user-сервис.
# Повторный запуск безопасен (идемпотентен). Удаление: ./install.sh --uninstall
set -euo pipefail
cd "$(dirname "$0")"
DIR=$(pwd)
SERVICE=claude-orchestrator
UNIT_DIR=$HOME/.config/systemd/user

if [ "${1:-}" = "--uninstall" ]; then
    echo "==> Удаляю systemd-сервис (репозиторий, .venv и .env не трогаю)"
    systemctl --user disable --now "$SERVICE" 2>/dev/null || true
    rm -f "$UNIT_DIR/$SERVICE.service"
    systemctl --user daemon-reload
    echo "Готово."
    exit 0
fi

echo "==> Проверки"
command -v python3 >/dev/null || { echo "Нужен python3"; exit 1; }
command -v claude >/dev/null || echo "ВНИМАНИЕ: claude не найден в PATH — установи Claude Code и залогинься"
command -v bwrap >/dev/null || echo "ВНИМАНИЕ: bubblewrap не найден (apt install bubblewrap) — либо поставь его, либо SANDBOX=off в .env"

echo "==> venv и зависимости"
python3 -m venv .venv
.venv/bin/pip install -q --upgrade pip
.venv/bin/pip install -q -r requirements.txt

if [ ! -f .env ]; then
    cp .env.example .env
    echo "==> Создан .env — заполни TELEGRAM_BOT_TOKEN и ALLOWED_USER_IDS"
fi

echo "==> systemd user-сервис"
# PATH юнита: каталог с бинарём claude определяем по факту (npm-global,
# ~/.local/bin, nvm — у всех по-разному), не хардкодим раскладку.
CLAUDE_PATH=""
if command -v claude >/dev/null; then
    CLAUDE_PATH="$(dirname "$(command -v claude)"):"
fi
mkdir -p "$UNIT_DIR"
cat > "$UNIT_DIR/$SERVICE.service" <<EOF
[Unit]
Description=claude-orchestrator — Claude Code session orchestrator (Telegram/Web)
After=network.target
# Защита от рестарт-штопора: 5 падений за 5 минут — стоп до ручного
# systemctl --user reset-failed (иначе цикл crash-restart молотит вечно).
StartLimitIntervalSec=300
StartLimitBurst=5

[Service]
Type=simple
WorkingDirectory=$DIR
# Гарантия одного инстанса: перед стартом добить сбежавший из cgroup инстанс.
# Паттерн через [.]venv — регэксп-трюк, чтобы pkill НЕ сматчил собственную
# командную строку (иначе убивает свой control-process → старт падает).
ExecStartPre=/bin/sh -c 'pkill -TERM -f "[.]venv/bin/python -m orchestrator" || true; sleep 1'
ExecStart=$DIR/.venv/bin/python -m orchestrator
Restart=on-failure
RestartSec=5
KillMode=control-group
KillSignal=SIGTERM
TimeoutStopSec=15
Environment=PATH=$CLAUDE_PATH$HOME/.local/bin:/usr/local/bin:/usr/bin:/bin

[Install]
WantedBy=default.target
EOF
systemctl --user daemon-reload

# Linger: user-сервис продолжает работать после разлогина / без SSH-сессии.
echo "==> loginctl enable-linger (сервис переживает разлогин)"
if ! loginctl enable-linger "$USER" 2>/dev/null; then
    echo "Не удалось включить linger без прав root, выполни вручную:"
    echo "  sudo loginctl enable-linger $USER"
fi

cat <<EOF

Готово. Дальше:
  1. Отредактируй $DIR/.env (TELEGRAM_BOT_TOKEN, ALLOWED_USER_IDS, TELEGRAM_CHAT_ID;
     веб-интерфейс — ADAPTERS=telegram,web)
  2. Запусти:            systemctl --user enable --now $SERVICE
  3. Логи:               journalctl --user -u $SERVICE -f
  4. Перезапуск:         systemctl --user restart $SERVICE
  5. Удалить сервис:     ./install.sh --uninstall
EOF
