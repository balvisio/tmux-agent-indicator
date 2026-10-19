#!/usr/bin/env bash
# Installer/updater for tmux-agent-indicator.

set -euo pipefail

SCRIPT_DIR=""
TMP_SOURCE_DIR=""

SCRIPT_SOURCE=""
case "${0:-}" in
    bash|-bash|sh|-sh)
        ;;
    *)
        SCRIPT_SOURCE="${0}"
        ;;
esac

if [ -z "$SCRIPT_SOURCE" ] && [ -n "${BASH_SOURCE+set}" ] && [ "${#BASH_SOURCE[@]}" -gt 0 ]; then
    SCRIPT_SOURCE="${BASH_SOURCE[0]}"
fi

if [ -n "$SCRIPT_SOURCE" ] && [ -f "$SCRIPT_SOURCE" ]; then
    SCRIPT_DIR="$(cd "$(dirname "$SCRIPT_SOURCE")" && pwd)"
fi

if [ -z "$SCRIPT_DIR" ] || [ ! -f "$SCRIPT_DIR/agent-indicator.tmux" ]; then
    INSTALL_REPO="${TMUX_AGENT_INSTALL_REPO:-balvisio/tmux-agent-indicator}"
    INSTALL_REF="${TMUX_AGENT_INSTALL_REF:-main}"
    ARCHIVE_URL="https://codeload.github.com/${INSTALL_REPO}/tar.gz/refs/heads/${INSTALL_REF}"

    if ! command -v curl >/dev/null 2>&1; then
        echo "curl is required for stdin-based installation" >&2
        exit 1
    fi
    if ! command -v tar >/dev/null 2>&1; then
        echo "tar is required for stdin-based installation" >&2
        exit 1
    fi

    TMP_SOURCE_DIR="$(mktemp -d)"
    trap 'rm -rf "$TMP_SOURCE_DIR"' EXIT
    curl -fsSL "$ARCHIVE_URL" | tar -xz -C "$TMP_SOURCE_DIR"
    SCRIPT_DIR="$(find "$TMP_SOURCE_DIR" -mindepth 1 -maxdepth 1 -type d | head -n 1)"

    if [ -z "$SCRIPT_DIR" ] || [ ! -f "$SCRIPT_DIR/agent-indicator.tmux" ]; then
        echo "Failed to fetch installer sources from $ARCHIVE_URL" >&2
        exit 1
    fi
fi
TARGET_DIR="${TMUX_AGENT_INSTALL_DIR:-$HOME/.tmux/plugins/tmux-agent-indicator}"
INSTALL_CLAUDE=true
INSTALL_CODEX=true
INSTALL_OPENCODE=true
INSTALL_TMUX_CONF=true
INSTALL_ALIAS=true
INSTALL_HAMMERSPOON=true
UNINSTALL_ALL=false
UNINSTALL_CLAUDE=false
UNINSTALL_CODEX=false
UNINSTALL_OPENCODE=false

usage() {
    cat <<'EOF'
Usage: install.sh [OPTIONS]

Options:
  --target-dir <path>  Install path (default: ~/.tmux/plugins/tmux-agent-indicator)
  --no-claude          Skip Claude hooks setup
  --no-codex           Skip Codex hooks setup
  --no-opencode        Skip OpenCode plugin setup
  --no-tmux-conf       Do not install ~/.tmux/agent-indicator.conf or touch ~/.tmux.conf
  --no-alias           Do not add the `codex --no-daemon` alias to the shell startup files
  --no-hammerspoon     Do not install the macOS menu-bar module (~/.hammerspoon/agent_dots.lua)
  --uninstall          Remove everything the installer put in place (plugin, config, hooks, alias, module)
  --uninstall-claude   Remove Claude hooks/status-line wrapper from ~/.claude/settings.json
  --uninstall-codex    Remove tmux-agent-indicator Codex hooks from ~/.codex/hooks.json
  --uninstall-opencode Remove tmux-agent-indicator OpenCode plugin from ~/.config/opencode/plugins/
  -h, --help           Show this help
EOF
}

while [ "$#" -gt 0 ]; do
    case "$1" in
        --target-dir)
            [ "$#" -lt 2 ] && usage && exit 1
            TARGET_DIR="$2"
            shift 2
            ;;
        --no-claude)
            INSTALL_CLAUDE=false
            shift
            ;;
        --no-codex)
            INSTALL_CODEX=false
            shift
            ;;
        --no-opencode)
            INSTALL_OPENCODE=false
            shift
            ;;
        --no-tmux-conf)
            INSTALL_TMUX_CONF=false
            shift
            ;;
        --no-alias)
            INSTALL_ALIAS=false
            shift
            ;;
        --no-hammerspoon)
            INSTALL_HAMMERSPOON=false
            shift
            ;;
        --uninstall)
            UNINSTALL_ALL=true
            UNINSTALL_CLAUDE=true
            UNINSTALL_CODEX=true
            UNINSTALL_OPENCODE=true
            INSTALL_CLAUDE=false
            INSTALL_CODEX=false
            INSTALL_OPENCODE=false
            INSTALL_TMUX_CONF=false
            INSTALL_ALIAS=false
            INSTALL_HAMMERSPOON=false
            shift
            ;;
        --uninstall-claude)
            UNINSTALL_CLAUDE=true
            INSTALL_CLAUDE=false
            shift
            ;;
        --uninstall-codex)
            UNINSTALL_CODEX=true
            INSTALL_CODEX=false
            shift
            ;;
        --uninstall-opencode)
            UNINSTALL_OPENCODE=true
            INSTALL_OPENCODE=false
            shift
            ;;
        -h|--help)
            usage
            exit 0
            ;;
        *)
            echo "Unknown option: $1" >&2
            usage
            exit 1
            ;;
    esac
done

# Auto-detect agents: skip integration if agent not found on system
if [ "$INSTALL_CLAUDE" = true ] && [ "$UNINSTALL_CLAUDE" = false ]; then
    CLAUDE_DIR="${CLAUDE_CONFIG_DIR:-$HOME/.claude}"
    if ! command -v claude >/dev/null 2>&1 && [ ! -d "$CLAUDE_DIR" ]; then
        INSTALL_CLAUDE=false
        echo "Claude not detected, skipping hooks setup"
    fi
fi

if [ "$INSTALL_CODEX" = true ] && [ "$UNINSTALL_CODEX" = false ]; then
    CODEX_DIR="${CODEX_CONFIG_DIR:-$HOME/.codex}"
    if ! command -v codex >/dev/null 2>&1 && [ ! -d "$CODEX_DIR" ]; then
        INSTALL_CODEX=false
        echo "Codex not detected, skipping hooks setup"
    fi
fi

if [ "$INSTALL_OPENCODE" = true ] && [ "$UNINSTALL_OPENCODE" = false ]; then
    OPENCODE_CFG_DIR="${XDG_CONFIG_HOME:-$HOME/.config}/opencode"
    if ! command -v opencode >/dev/null 2>&1 && [ ! -d "$OPENCODE_CFG_DIR" ]; then
        INSTALL_OPENCODE=false
        echo "OpenCode not detected, skipping plugin setup"
    fi
fi

TMUX_CONF="$HOME/.tmux.conf"
AGENT_CONF="$HOME/.tmux/agent-indicator.conf"
SOURCE_LINE="source-file ~/.tmux/agent-indicator.conf"
MARKER="# tmux-agent-indicator"
ALIAS_LINE="alias codex='codex --no-daemon'"
HS_DIR="$HOME/.hammerspoon"
HS_MODULE="$HS_DIR/agent_dots.lua"
HS_REQUIRE='require("agent_dots")'

# Remove the marker-tagged lines the installer added to a shared file (startup files, init.lua, .tmux.conf).
remove_marked_lines() {
    local file="$1" tmp
    [ -f "$file" ] || return 0
    grep -qF "$MARKER" "$file" || return 0
    tmp="$(mktemp)"
    grep -vF "$MARKER" "$file" > "$tmp" || true
    cat "$tmp" > "$file"
    rm -f "$tmp"
}

if [ "$UNINSTALL_ALL" = true ]; then
    remove_marked_lines "$TMUX_CONF"
    rm -f "$AGENT_CONF"
    for f in "$HOME/.bash_profile" "$HOME/.bashrc" "$HOME/.zshrc"; do remove_marked_lines "$f"; done
    remove_marked_lines "$HS_DIR/init.lua"
    rm -f "$HS_MODULE"
    echo "Removed ~/.tmux/agent-indicator.conf, the source-file line, the codex alias and the Hammerspoon module"
fi

# A symlink here points at a development checkout: replace the link, never write through it.
if [ -L "$TARGET_DIR" ]; then
    rm "$TARGET_DIR"
fi
if [ "$UNINSTALL_ALL" = false ]; then
    mkdir -p "$TARGET_DIR/scripts" "$TARGET_DIR/hooks" "$TARGET_DIR/plugins" "$TARGET_DIR/licenses" "$TARGET_DIR/config" "$TARGET_DIR/integrations/hammerspoon"
fi

if [ "$UNINSTALL_ALL" = false ]; then
    cp "$SCRIPT_DIR/agent-indicator.tmux" "$TARGET_DIR/"
    cp "$SCRIPT_DIR/README.md" "$TARGET_DIR/"
    cp "$SCRIPT_DIR/LICENSE" "$TARGET_DIR/"
    cp "$SCRIPT_DIR/licenses/"*.txt "$TARGET_DIR/licenses/"
    cp "$SCRIPT_DIR/scripts/"*.sh "$TARGET_DIR/scripts/"
    cp "$SCRIPT_DIR/scripts/"*.py "$TARGET_DIR/scripts/"
    cp "$SCRIPT_DIR/hooks/"*.json "$TARGET_DIR/hooks/"
    cp "$SCRIPT_DIR/plugins/"*.js "$TARGET_DIR/plugins/"
    cp "$SCRIPT_DIR/config/agent-indicator.conf" "$TARGET_DIR/config/"
    cp "$SCRIPT_DIR/integrations/hammerspoon/agent_dots.lua" "$TARGET_DIR/integrations/hammerspoon/"

    chmod +x "$TARGET_DIR/agent-indicator.tmux" "$TARGET_DIR/scripts/"*.sh "$TARGET_DIR/scripts/"*.py
fi

if [ "$INSTALL_TMUX_CONF" = true ]; then
    mkdir -p "$HOME/.tmux"
    cp "$TARGET_DIR/config/agent-indicator.conf" "$AGENT_CONF"
    if [ ! -f "$TMUX_CONF" ] || ! grep -qF "$SOURCE_LINE" "$TMUX_CONF"; then
        printf '\n%s   %s\n' "$SOURCE_LINE" "$MARKER" >> "$TMUX_CONF"
    fi
    echo "tmux config -> $AGENT_CONF (sourced from $TMUX_CONF)"
fi

if [ "$INSTALL_ALIAS" = true ] && command -v codex >/dev/null 2>&1; then
    # Bash login shells (macOS terminals) read .bash_profile, other Bash shells .bashrc, Zsh .zshrc.
    # Only files that already exist are touched, and a user's own codex alias is left alone.
    for f in "$HOME/.bash_profile" "$HOME/.bashrc" "$HOME/.zshrc"; do
        [ -f "$f" ] || continue
        grep -qE "^[[:space:]]*alias codex=" "$f" && continue
        printf '\n%s   %s\n' "$ALIAS_LINE" "$MARKER" >> "$f"
        echo "codex alias -> $f"
    done
fi

if [ "$INSTALL_HAMMERSPOON" = true ] && [ "$(uname -s)" = Darwin ] && [ -d "$HS_DIR" ]; then
    cp "$TARGET_DIR/integrations/hammerspoon/agent_dots.lua" "$HS_MODULE"
    touch "$HS_DIR/init.lua"
    if ! grep -qF "$HS_REQUIRE" "$HS_DIR/init.lua"; then
        printf '\n%s   -- %s\n' "$HS_REQUIRE" "${MARKER#\# }" >> "$HS_DIR/init.lua"
    fi
    echo "Hammerspoon module -> $HS_MODULE (reload Hammerspoon: menu bar icon -> Reload Config)"
fi

if [ "$INSTALL_CLAUDE" = true ] || [ "$UNINSTALL_CLAUDE" = true ]; then
    CLAUDE_DIR="${CLAUDE_CONFIG_DIR:-$HOME/.claude}"
    CLAUDE_SETTINGS="$CLAUDE_DIR/settings.json"
    CLAUDE_TEMPLATE="$TARGET_DIR/hooks/claude-hooks.json"
    mkdir -p "$CLAUDE_DIR"
    if [ ! -f "$CLAUDE_SETTINGS" ] && [ "$INSTALL_CLAUDE" = true ]; then
        printf '{}\n' > "$CLAUDE_SETTINGS"
    fi

    if [ "$INSTALL_CLAUDE" = true ]; then
        echo "Claude detected"
        echo "  Hooks/status line -> $CLAUDE_SETTINGS"
    fi

    if [ -f "$CLAUDE_SETTINGS" ]; then
        if [ "$UNINSTALL_CLAUDE" = true ]; then
            CLAUDE_MODE="uninstall"
        else
            CLAUDE_MODE="install"
        fi

        python3 - "$CLAUDE_SETTINGS" "$CLAUDE_TEMPLATE" "$TARGET_DIR" "$CLAUDE_MODE" <<'PY'
import base64
import json
import pathlib
import shlex
import sys

settings_path = pathlib.Path(sys.argv[1])
template_path = pathlib.Path(sys.argv[2])
target_dir = sys.argv[3]
mode = sys.argv[4]
default_install_dir = "$HOME/.tmux/plugins/tmux-agent-indicator"

try:
    settings = json.loads(settings_path.read_text(encoding="utf-8"))
except Exception:
    settings = {}

if not isinstance(settings, dict):
    settings = {}

hooks = settings.setdefault("hooks", {})
if not isinstance(hooks, dict):
    hooks = {}

def unwrap_statusline(command):
    try:
        parts = shlex.split(command)
    except ValueError:
        return False, ""
    for index, part in enumerate(parts):
        if not part.endswith("/scripts/agent-limits.py"):
            continue
        if parts[index + 1:index + 2] != ["claude-statusline"]:
            continue
        if index + 2 == len(parts):
            return True, ""
        if parts[index + 2:index + 3] != ["--previous-command-base64"]:
            continue
        if index + 3 >= len(parts):
            return False, ""
        try:
            previous = base64.b64decode(parts[index + 3]).decode("utf-8")
        except Exception:
            return False, ""
        return True, previous
    return False, ""

def is_plugin_command(command):
    return "scripts/agent-state.sh" in command and "--agent claude --state" in command

for event in list(hooks.keys()):
    entries = hooks.get(event, [])
    if not isinstance(entries, list):
        continue

    cleaned_entries = []
    for entry in entries:
        if not isinstance(entry, dict):
            cleaned_entries.append(entry)
            continue

        hook_items = entry.get("hooks", [])
        if not isinstance(hook_items, list):
            cleaned_entries.append(entry)
            continue

        cleaned_hook_items = []
        for hook_item in hook_items:
            if not isinstance(hook_item, dict):
                cleaned_hook_items.append(hook_item)
                continue

            cmd = hook_item.get("command", "")
            if isinstance(cmd, str) and is_plugin_command(cmd):
                continue
            cleaned_hook_items.append(hook_item)

        if hook_items and not cleaned_hook_items:
            continue

        if cleaned_hook_items != hook_items:
            updated = dict(entry)
            updated["hooks"] = cleaned_hook_items
            cleaned_entries.append(updated)
        else:
            cleaned_entries.append(entry)

    if cleaned_entries:
        hooks[event] = cleaned_entries
    else:
        hooks.pop(event, None)

template_text = template_path.read_text(encoding="utf-8").replace(default_install_dir, target_dir)
template = json.loads(template_text)
template_hooks = template.get("hooks", {}) if isinstance(template, dict) else {}

if mode == "install":
    for event, entries in template_hooks.items():
        if isinstance(entries, list):
            hooks.setdefault(event, []).extend(entries)

settings["hooks"] = hooks

status_line = settings.get("statusLine")
status_command = status_line.get("command", "") if isinstance(status_line, dict) else ""
wrapped, previous_command = unwrap_statusline(status_command)

if mode == "install" and (status_line is None or isinstance(status_line, dict)):
    if not wrapped:
        previous_command = status_command
    encoded = base64.b64encode(previous_command.encode("utf-8")).decode("ascii")
    script = shlex.quote(str(pathlib.Path(target_dir) / "scripts" / "agent-limits.py"))
    command = f"{script} claude-statusline"
    if encoded:
        command += f" --previous-command-base64 {encoded}"
    updated = dict(status_line) if isinstance(status_line, dict) else {}
    updated["type"] = "command"
    updated["command"] = command
    settings["statusLine"] = updated
elif mode == "uninstall" and wrapped:
    if previous_command:
        updated = dict(status_line)
        updated["command"] = previous_command
        settings["statusLine"] = updated
    else:
        settings.pop("statusLine", None)

settings_path.write_text(json.dumps(settings, indent=2) + "\n", encoding="utf-8")
PY
    fi
fi

if [ "$INSTALL_CODEX" = true ] || [ "$UNINSTALL_CODEX" = true ]; then
    CODEX_DIR="${CODEX_CONFIG_DIR:-$HOME/.codex}"
    CODEX_HOOKS="$CODEX_DIR/hooks.json"
    CODEX_TEMPLATE="$TARGET_DIR/hooks/codex-hooks.json"
    CODEX_CONFIG="$CODEX_DIR/config.toml"
    mkdir -p "$CODEX_DIR"
    if [ ! -f "$CODEX_HOOKS" ] && [ "$INSTALL_CODEX" = true ]; then
        printf '{"hooks":{}}\n' > "$CODEX_HOOKS"
    fi

    if [ "$INSTALL_CODEX" = true ]; then
        echo "Codex detected"
        echo "  Hooks -> $CODEX_HOOKS (UserPromptSubmit, PermissionRequest, PostToolUse, Interrupt, SessionEnd, Stop)"
        echo "  Next: run /hooks in Codex and trust the tmux-agent-indicator hooks"
    fi

    if [ -f "$CODEX_HOOKS" ]; then
        if [ "$UNINSTALL_CODEX" = true ]; then
            CODEX_MODE="uninstall"
        else
            CODEX_MODE="install"
        fi

        python3 - "$CODEX_HOOKS" "$CODEX_TEMPLATE" "$TARGET_DIR" "$CODEX_MODE" <<'PY'
import json
import pathlib
import sys

hooks_path = pathlib.Path(sys.argv[1])
template_path = pathlib.Path(sys.argv[2])
target_dir = sys.argv[3]
mode = sys.argv[4]
default_install_dir = "$HOME/.tmux/plugins/tmux-agent-indicator"

try:
    settings = json.loads(hooks_path.read_text(encoding="utf-8"))
except Exception:
    settings = {}

if not isinstance(settings, dict):
    settings = {}

hooks = settings.setdefault("hooks", {})
if not isinstance(hooks, dict):
    hooks = {}

def is_plugin_command(command):
    return "scripts/agent-state.sh" in command and "--agent codex --state" in command

for event in list(hooks.keys()):
    entries = hooks.get(event, [])
    if not isinstance(entries, list):
        continue

    cleaned_entries = []
    for entry in entries:
        if not isinstance(entry, dict):
            cleaned_entries.append(entry)
            continue

        hook_items = entry.get("hooks", [])
        if not isinstance(hook_items, list):
            cleaned_entries.append(entry)
            continue

        cleaned_hook_items = []
        for hook_item in hook_items:
            if not isinstance(hook_item, dict):
                cleaned_hook_items.append(hook_item)
                continue

            command = hook_item.get("command", "")
            if isinstance(command, str) and is_plugin_command(command):
                continue
            cleaned_hook_items.append(hook_item)

        if hook_items and not cleaned_hook_items:
            continue

        if cleaned_hook_items != hook_items:
            updated = dict(entry)
            updated["hooks"] = cleaned_hook_items
            cleaned_entries.append(updated)
        else:
            cleaned_entries.append(entry)

    if cleaned_entries:
        hooks[event] = cleaned_entries
    else:
        hooks.pop(event, None)

template_text = template_path.read_text(encoding="utf-8").replace(default_install_dir, target_dir)
template = json.loads(template_text)
template_hooks = template.get("hooks", {}) if isinstance(template, dict) else {}

if mode == "install":
    for event, entries in template_hooks.items():
        if isinstance(entries, list):
            hooks.setdefault(event, []).extend(entries)

settings["hooks"] = hooks

hooks_path.write_text(json.dumps(settings, indent=2) + "\n", encoding="utf-8")
PY
    fi

    if [ -f "$CODEX_CONFIG" ]; then
        python3 - "$CODEX_CONFIG" <<'PY'
import pathlib
import re
import sys

config_path = pathlib.Path(sys.argv[1])
text = config_path.read_text(encoding="utf-8")
text = re.sub(
    r'(?m)^[ \t]*notify[ \t]*=[ \t]*\[\s*"bash"\s*,\s*".*/adapters/codex-notify\.sh"\s*\][ \t]*\n?',
    "",
    text,
)
config_path.write_text(text, encoding="utf-8")
PY
    fi
fi

OPENCODE_PLUGIN_NAME="opencode-tmux-agent-indicator.js"

if [ "$INSTALL_OPENCODE" = true ]; then
    OPENCODE_PLUGINS_DIR="${XDG_CONFIG_HOME:-$HOME/.config}/opencode/plugins"
    echo "OpenCode detected"
    echo "  Plugin -> $OPENCODE_PLUGINS_DIR/$OPENCODE_PLUGIN_NAME"
    mkdir -p "$OPENCODE_PLUGINS_DIR"
    cp "$TARGET_DIR/plugins/$OPENCODE_PLUGIN_NAME" "$OPENCODE_PLUGINS_DIR/$OPENCODE_PLUGIN_NAME"
fi

if [ "$UNINSTALL_OPENCODE" = true ]; then
    OPENCODE_PLUGINS_DIR="${XDG_CONFIG_HOME:-$HOME/.config}/opencode/plugins"
    rm -f "$OPENCODE_PLUGINS_DIR/$OPENCODE_PLUGIN_NAME"
fi

if [ "$UNINSTALL_ALL" = false ]; then
    echo "Installed/updated tmux-agent-indicator in $TARGET_DIR"
    if [ "$INSTALL_TMUX_CONF" = true ] && tmux info >/dev/null 2>&1; then
        # Reload only the plugin config: sourcing a whole ~/.tmux.conf can re-run new-session lines.
        if tmux source-file "$AGENT_CONF" 2>/dev/null; then
            echo "Running tmux server reloaded ($AGENT_CONF)"
        else
            echo "Reload the running tmux server with: tmux source-file $AGENT_CONF"
        fi
    fi
    echo "Codex: start new sessions from a new shell (alias) and run /hooks once to trust the hooks."
fi

if [ "$UNINSTALL_CLAUDE" = true ]; then
    echo "Removed tmux-agent-indicator Claude hooks/status-line wrapper from: ${CLAUDE_CONFIG_DIR:-$HOME/.claude}/settings.json"
fi

if [ "$UNINSTALL_CODEX" = true ]; then
    echo "Removed tmux-agent-indicator Codex hooks from: ${CODEX_CONFIG_DIR:-$HOME/.codex}/hooks.json"
fi

if [ "$UNINSTALL_OPENCODE" = true ]; then
    echo "Removed tmux-agent-indicator OpenCode plugin from: ${XDG_CONFIG_HOME:-$HOME/.config}/opencode/plugins/"
fi

# Last: the hook uninstallers above still need the templates inside the plugin directory.
if [ "$UNINSTALL_ALL" = true ]; then
    rm -rf "$TARGET_DIR"
    echo "Removed $TARGET_DIR"
fi

