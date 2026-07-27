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
    for cli in claude-box vault wallet; do   # только свои симлинки
        link="$HOME/.local/bin/$cli"
        [ -L "$link" ] && [ "$(readlink "$link")" = "$DIR/bin/$cli" ] && rm -f "$link"
    done
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

echo "==> CLI в PATH (~/.local/bin): claude-box, vault, wallet"
# claude-box — базовый слой, им пользуются и без оркестратора; без симлинка
# человек получает «команда не найдена» и не понимает, что она вообще есть.
mkdir -p "$HOME/.local/bin"
for cli in claude-box vault wallet; do
    link="$HOME/.local/bin/$cli"
    if [ -e "$link" ] && [ ! -L "$link" ]; then
        echo "    пропускаю $link — это обычный файл, не трогаю"
        continue
    fi
    ln -sfn "$DIR/bin/$cli" "$link"
done
case ":$PATH:" in
    *":$HOME/.local/bin:"*) ;;
    *) echo "    ВНИМАНИЕ: $HOME/.local/bin не в PATH — добавь его в ~/.profile" ;;
esac

# Базовая конфигурация claude-box: как выглядит «просто claude-box» на этой
# машине. Спрашиваем ОДИН раз и только у живого терминала — в CI и при повторной
# установке молча ничего не трогаем (готовый конфиг оператора не перезаписываем).
BOX_CONFIG="${CLAUDE_BOX_CONFIG:-$HOME/.config/claude-box/config.toml}"
if [ -t 0 ] && [ ! -f "$BOX_CONFIG" ] && [ "${1:-}" != "--no-config" ]; then
    echo "==> Базовая конфигурация claude-box (Enter — значение по умолчанию)"
    printf '    Изоляция по умолчанию [bwrap/off/vm] (bwrap): '
    read -r box_engine || box_engine=""
    case "${box_engine:-bwrap}" in
        bwrap|off) ;;
        vm|agent-vm) box_engine=agent-vm ;;
        *) echo "    не понял «$box_engine» — беру bwrap"; box_engine=bwrap ;;
    esac
    printf '    Кошелёк секретов, когда изоляция есть? [Y/n]: '
    read -r box_wallet || box_wallet=""
    case "$box_wallet" in [nNнН]*) box_wallet=false ;; *) box_wallet=true ;; esac
    printf '    Профиль claude по умолчанию (пусто — общий ~/.claude): '
    read -r box_profile || box_profile=""
    # Имя профиля идёт и в TOML, и в путь каталога. Кавычка внутри имени
    # сломала бы файл настроек так, что claude-box перестал бы запускаться
    # ВООБЩЕ (умолчания читаются раньше разбора аргументов, даже для --help),
    # поэтому валидируем тем же allowlist, что и сам claude-box.
    if [ -n "$box_profile" ] && ! printf '%s' "$box_profile" | grep -qE '^[A-Za-z0-9._-]{1,64}$'; then
        echo "    имя «$box_profile» не годится (допустимо: латиница, цифры, . _ -) — пропускаю"
        box_profile=""
    fi

    # Выбрали microVM — проверим, чем её запускать: нужен НАШ форк agent-vm
    # (у апстрима нет флагов egress, и кошелёк в госте работать не будет).
    # Ничего не качаем молча: печатаем, чего не хватает, и куда смотреть.
    if [ "$box_engine" = "agent-vm" ]; then
        if ! command -v agent-vm >/dev/null 2>&1; then
            echo "    ВНИМАНИЕ: agent-vm не найден — microVM не запустится."
            echo "    Установка форка: docs/BOX.md, раздел «Установка microVM»"
        elif ! agent-vm claude --help 2>/dev/null | grep -q -- "--egress-proxy"; then
            echo "    ВНИМАНИЕ: установлен АПСТРИМНЫЙ agent-vm (нет --egress-proxy):"
            echo "    кошелёк в microVM не заработает. Поставь форк"
            echo "    github.com/aadegtyarev/agent-vm — см. docs/BOX.md"
        else
            echo "    agent-vm: форк на месте ($(agent-vm --version 2>/dev/null))"
        fi
        [ -e /dev/kvm ] || echo "    ВНИМАНИЕ: /dev/kvm нет — microVM не поднимется"
    fi

    mkdir -p "$(dirname "$BOX_CONFIG")"
    {
        echo "# Умолчания claude-box (флаг в командной строке всегда сильнее)."
        echo "# Справочник: docs/BOX.md"
        echo "engine = \"${box_engine:-bwrap}\""
        echo "wallet = $box_wallet"
        [ -n "$box_profile" ] && echo "profile = \"$box_profile\""
    } > "$BOX_CONFIG"
    echo "    записано в $BOX_CONFIG"
    if [ -n "$box_profile" ]; then
        "$DIR/bin/claude-box" init "$box_profile" >/dev/null 2>&1 \
            && echo "    профиль «$box_profile» создан" \
            || echo "    профиль «$box_profile» создать не удалось — сделай позже: claude-box init $box_profile"
    fi
fi

if [ -f "$BOX_CONFIG" ]; then
    echo "==> Умолчания claude-box: $BOX_CONFIG (перенастроить — claude-box config)"
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
