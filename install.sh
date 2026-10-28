#!/usr/bin/env bash
# Installer/updater for tmux-agent-indicator.

set -euo pipefail

SCRIPT_DIR=""
TMP_SOURCE_DIR=""
INSTALL_WARNINGS=""
cleanup() {
    [ -z "$TMP_SOURCE_DIR" ] || rm -rf "$TMP_SOURCE_DIR"
    [ -z "$INSTALL_WARNINGS" ] || rm -f "$INSTALL_WARNINGS"
    return 0
}
trap cleanup EXIT

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
    curl -fsSL "$ARCHIVE_URL" | tar -xz -C "$TMP_SOURCE_DIR"
    SCRIPT_DIR="$(find "$TMP_SOURCE_DIR" -mindepth 1 -maxdepth 1 -type d | head -n 1)"

    if [ -z "$SCRIPT_DIR" ] || [ ! -f "$SCRIPT_DIR/agent-indicator.tmux" ]; then
        echo "Failed to fetch installer sources from $ARCHIVE_URL" >&2
        exit 1
    fi
fi
TARGET_DIR="$HOME/.tmux/plugins/tmux-agent-indicator"
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
  --no-claude          Skip Claude hooks setup
  --no-codex           Skip Codex hooks setup
  --no-opencode        Skip OpenCode plugin setup
  --no-tmux-conf       Do not install ~/.tmux/agent-indicator.conf or touch ~/.tmux.conf
  --no-alias           Do not add the codex and ssh-reconnect aliases to the shell startup files
  --no-hammerspoon     Do not install the macOS menu-bar module (~/.hammerspoon/agent_dots.lua)
  --uninstall          Remove everything the installer put in place (plugin, config, hooks, alias, module)
  -h, --help           Show this help
EOF
}

while [ "$#" -gt 0 ]; do
    case "$1" in
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

if [ "${BASH_VERSINFO[0]}" -lt 4 ]; then
    echo "ERROR: Run this installer with Bash 4+; the current interpreter is $BASH_VERSION." >&2
    exit 1
fi
if ! command -v python3 >/dev/null 2>&1; then
    echo "ERROR: Python 3.8+ is required; install python3 and make it available on PATH." >&2
    exit 1
fi
python3 - <<'PYTHON'
import sys
if sys.version_info < (3, 8):
    sys.exit("ERROR: Python 3.8+ required; found " + sys.version.split()[0])
try:
    import fcntl, json, pathlib, ssl, urllib.request
except ImportError:
    sys.exit("ERROR: Python is missing required standard-library modules; install a complete Python 3 distribution.")
PYTHON
INSTALL_WARNINGS="$(mktemp)"

# Auto-detect agents: skip integration if agent not found on system
if [ "$INSTALL_CLAUDE" = true ] && [ "$UNINSTALL_CLAUDE" = false ]; then
    CLAUDE_DIR="${CLAUDE_CONFIG_DIR:-$HOME/.claude}"
    if ! command -v claude >/dev/null 2>&1 && [ ! -d "$CLAUDE_DIR" ]; then
        INSTALL_CLAUDE=false
        echo "Claude not detected, skipping hooks setup"
    fi
fi

if [ "$INSTALL_CODEX" = true ] && [ "$UNINSTALL_CODEX" = false ]; then
    CODEX_DIR="${CODEX_CONFIG_DIR:-${CODEX_HOME:-$HOME/.codex}}"
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
TAG="managed by tmux-agent-indicator install.sh"   # every line the installer adds to a shared file ends with it
OWNED="$HOME/.tmux/agent-indicator.installed"        # files created outside the plugin directory, and "line:<file>" for files that got a tagged line
ALIAS_LINE="alias codex='codex --no-daemon'"
RECONNECT_ALIAS="alias ssh-reconnect='$HOME/.tmux/plugins/tmux-agent-indicator/scripts/ssh-reconnect.sh'"
# Bash-only code behind a guard, because .profile and .bash_login are read by other shells too:
# interactive ($- has i) Bash that is neither in POSIX mode (SHELLOPTS) nor invoked as sh ($0).
RECONNECT_COMPLETION_BASH="if [ -n \"\${BASH_VERSION:-}\" ] && [ -r ~/.tmux/plugins/tmux-agent-indicator/scripts/ssh-reconnect-completion.sh ]; then case \"\$-\" in *i*) case \":\${SHELLOPTS:-}:/\${0##*/}\" in *:posix:*|*/sh|*/-sh) ;; *) . ~/.tmux/plugins/tmux-agent-indicator/scripts/ssh-reconnect-completion.sh ;; esac ;; esac; fi"
RECONNECT_COMPLETION_ZSH="_ssh_reconnect() { local -a h; h=(\${(f)\"\$(~/.tmux/plugins/tmux-agent-indicator/scripts/ssh-hosts.sh)\"}); _describe host h; }; if (( \$+functions[compdef] )); then compdef _ssh_reconnect ssh-reconnect; fi"
HS_DIR="$HOME/.hammerspoon"
HS_MODULE="$HS_DIR/agent_dots.lua"
HS_REQUIRE='require("agent_dots")'

startup_files=()
if [ "$INSTALL_ALIAS" = true ]; then
    # Every existing startup file gets the line, and so does the current shell's own file even when it
    # does not exist yet. Zsh: ${ZDOTDIR}/.zshrc. Bash on macOS opens login shells, which read the first
    # of .bash_profile, .bash_login, .profile that exists (only .bash_profile is created when none does);
    # elsewhere .bashrc. A codex alias the user wrote is left alone.
    for f in "$HOME/.bash_profile" "$HOME/.bash_login" "$HOME/.profile" "$HOME/.bashrc" "${ZDOTDIR:-$HOME}/.zshrc"; do
        [ -f "$f" ] && startup_files+=("$f")
    done
    case "${SHELL:-}" in
        */zsh) startup_files+=("${ZDOTDIR:-$HOME}/.zshrc") ;;
        */bash)
            if [ "$(uname -s)" = Darwin ]; then
                login_file="$HOME/.bash_profile"
                for f in "$HOME/.bash_profile" "$HOME/.bash_login" "$HOME/.profile"; do
                    if [ -f "$f" ]; then login_file="$f"; break; fi
                done
                startup_files+=("$login_file")
            else
                startup_files+=("$HOME/.bashrc")
            fi ;;
    esac
fi

SETUP_HAMMERSPOON=false
if [ "$INSTALL_HAMMERSPOON" = true ] && [ "$(uname -s)" = Darwin ]; then
    if [ -d "$HS_DIR" ] || [ -d /Applications/Hammerspoon.app ] || [ -d "$HOME/Applications/Hammerspoon.app" ]; then
        SETUP_HAMMERSPOON=true
    fi
    if [ ! -d /Applications/Hammerspoon.app ] && [ ! -d "$HOME/Applications/Hammerspoon.app" ]; then
        printf '%s\n' 'Hammerspoon not found in Applications. Install it (brew install --cask hammerspoon) and rerun the installer for menu-bar dots.' >> "$INSTALL_WARNINGS"
    fi
fi

check_args=(--warnings "$INSTALL_WARNINGS")
[ "$UNINSTALL_ALL" = false ] || check_args+=(--uninstall)
[ "$INSTALL_TMUX_CONF" = false ] || check_args+=(--tmux-conf)
[ "$SETUP_HAMMERSPOON" = false ] || check_args+=(--hammerspoon)
if [ "$INSTALL_CLAUDE" = true ] || [ "$UNINSTALL_CLAUDE" = true ]; then
    check_args+=(--claude "${CLAUDE_CONFIG_DIR:-$HOME/.claude}")
fi
if [ "$INSTALL_CODEX" = true ] || [ "$UNINSTALL_CODEX" = true ]; then
    check_args+=(--codex "${CODEX_CONFIG_DIR:-${CODEX_HOME:-$HOME/.codex}}")
fi
[ "$INSTALL_OPENCODE" = false ] || check_args+=(--opencode "${XDG_CONFIG_HOME:-$HOME/.config}/opencode")
for f in ${startup_files[@]+"${startup_files[@]}"}; do check_args+=(--startup-file "$f"); done
python3 "$SCRIPT_DIR/scripts/install-checks.py" "${check_args[@]}"

# Remove the tagged lines the installer added to a shared file (startup files, init.lua, .tmux.conf).
remove_tagged_lines() {
    local file="$1" tmp
    [ -f "$file" ] || return 0
    grep -qF "$TAG" "$file" || return 0
    tmp="$(mktemp)"
    grep -vF "$TAG" "$file" > "$tmp" || true
    cat "$tmp" > "$file"
    rm -f "$tmp"
}

# Record a file as installer-owned; a pre-existing file that is not ours is kept as a dated backup.
own_file() {
    local file="$1"
    if [ -f "$file" ] && ! grep -qxF "$file" "$OWNED" 2>/dev/null; then
        cp -p "$file" "$file.bak-$(date +%Y%m%d%H%M%S)"
        echo "Kept your previous $file as a .bak copy"
    fi
    grep -qxF "$file" "$OWNED" 2>/dev/null || echo "$file" >> "$OWNED"
}

# Installer-owned line in a shared file, remembered for uninstall. Added once; when the installer
# ships a newer version of the same entry, the old one (found by the tag plus a needle only that
# entry contains) is replaced instead of a second line being added.
put_tagged_line() {
    local file="$1" needle="$2" line="$3" comment="$4" full tmp
    full="$line   $comment $TAG"
    [ -f "$file" ] && grep -qxF "$full" "$file" && return 0
    if [ -f "$file" ] && grep -F "$TAG" "$file" | grep -qF "$needle"; then
        tmp="$(mktemp)"
        awk -v tag="$TAG" -v needle="$needle" '!(index($0, tag) && index($0, needle))' "$file" > "$tmp"
        cat "$tmp" > "$file"
        rm -f "$tmp"
    fi
    if [ -s "$file" ] && [ -n "$(tail -n 1 "$file")" ] && ! tail -n 1 "$file" | grep -qF "$TAG"; then
        printf '\n' >> "$file"
    fi
    printf '%s\n' "$full" >> "$file"
    grep -qxF "line:$file" "$OWNED" 2>/dev/null || echo "line:$file" >> "$OWNED"
}

if [ "$UNINSTALL_ALL" = true ]; then
    python3 "$SCRIPT_DIR/scripts/agent-recovery.py" disable "$TARGET_DIR"
    python3 "$SCRIPT_DIR/scripts/recovery-dependencies.py" --uninstall
    for f in "$TMUX_CONF" "$HOME/.bash_profile" "$HOME/.bash_login" "$HOME/.profile" "$HOME/.bashrc" "$HOME/.zshrc" "$HS_DIR/init.lua"; do
        remove_tagged_lines "$f"
    done
    if [ -f "$OWNED" ]; then
        while IFS= read -r f; do
            case "$f" in
                "") ;;
                line:*) remove_tagged_lines "${f#line:}" ;;
                *) rm -f "$f" && echo "Removed $f" ;;
            esac
        done < "$OWNED"
        rm -f "$OWNED"
    fi
fi

# Fetch dependencies before replacing any live plugin files.
if [ "$INSTALL_TMUX_CONF" = true ]; then
    python3 "$SCRIPT_DIR/scripts/recovery-dependencies.py"
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
    cp "$SCRIPT_DIR/scripts/slack-badge.js" "$TARGET_DIR/scripts/"
    cp "$SCRIPT_DIR/hooks/"*.json "$TARGET_DIR/hooks/"
    cp "$SCRIPT_DIR/plugins/"*.js "$TARGET_DIR/plugins/"
    cp "$SCRIPT_DIR/config/agent-indicator.conf" "$TARGET_DIR/config/"
    cp "$SCRIPT_DIR/install.sh" "$TARGET_DIR/"
    cp "$SCRIPT_DIR/integrations/hammerspoon/"*.lua "$TARGET_DIR/integrations/hammerspoon/"

    chmod +x "$TARGET_DIR/agent-indicator.tmux" "$TARGET_DIR/install.sh" "$TARGET_DIR/scripts/"*.sh "$TARGET_DIR/scripts/"*.py
fi

if [ "$INSTALL_TMUX_CONF" = true ]; then
    mkdir -p "$HOME/.tmux"
    own_file "$AGENT_CONF"
    cp "$TARGET_DIR/config/agent-indicator.conf" "$AGENT_CONF"
    if [ ! -f "$TMUX_CONF" ] || ! grep -qF "$SOURCE_LINE" "$TMUX_CONF"; then
        put_tagged_line "$TMUX_CONF" "agent-indicator.conf" "$SOURCE_LINE" "#"
    fi
    echo "tmux config -> $AGENT_CONF (sourced from $TMUX_CONF)"
fi

if [ "$INSTALL_ALIAS" = true ]; then
    seen_files=" "
    for f in ${startup_files[@]+"${startup_files[@]}"}; do
        case "$seen_files" in *" $f "*) continue ;; esac
        seen_files="$seen_files$f "
        if ! { [ -f "$f" ] && grep -qE "^[[:space:]]*alias ssh-reconnect=" "$f"; }; then
            put_tagged_line "$f" "alias ssh-reconnect=" "$RECONNECT_ALIAS" "#"
            echo "ssh-reconnect alias -> $f"
        fi
        case "$f" in
            *zshrc) put_tagged_line "$f" "compdef" "$RECONNECT_COMPLETION_ZSH" "#" ;;
            *) put_tagged_line "$f" "ssh-reconnect-completion.sh" "$RECONNECT_COMPLETION_BASH" "#" ;;
        esac
        command -v codex >/dev/null 2>&1 || continue
        [ -f "$f" ] && grep -qE "^[[:space:]]*alias codex=" "$f" && continue
        put_tagged_line "$f" "alias codex=" "$ALIAS_LINE" "#"
        echo "codex alias -> $f"
    done
fi

if [ "$SETUP_HAMMERSPOON" = true ]; then
    mkdir -p "$HS_DIR"
    own_file "$HS_MODULE"
    cp "$TARGET_DIR/integrations/hammerspoon/agent_dots.lua" "$HS_MODULE"
    touch "$HS_DIR/init.lua"
    if ! grep -qF "$HS_REQUIRE" "$HS_DIR/init.lua"; then
        put_tagged_line "$HS_DIR/init.lua" "agent_dots" "$HS_REQUIRE" "--"
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

        python3 - "$CLAUDE_SETTINGS" "$CLAUDE_TEMPLATE" "$TARGET_DIR" "$CLAUDE_MODE" "$SCRIPT_DIR/scripts/install-checks.py" <<'PY'
import base64
import json
import pathlib
import shlex
import sys
import runpy

settings_path = pathlib.Path(sys.argv[1])
template_path = pathlib.Path(sys.argv[2])
target_dir = sys.argv[3]
mode = sys.argv[4]
default_install_dir = "$HOME/.tmux/plugins/tmux-agent-indicator"

checks = runpy.run_path(sys.argv[5])
settings = checks["read_hooks"](settings_path)
hooks = settings.setdefault("hooks", {})

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
    return ("scripts/agent-state.sh" in command and "--agent claude --state" in command
            or "scripts/agent-recovery.py" in command and "hook claude" in command)

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
    CODEX_DIR="${CODEX_CONFIG_DIR:-${CODEX_HOME:-$HOME/.codex}}"
    CODEX_HOOKS="$CODEX_DIR/hooks.json"
    CODEX_TEMPLATE="$TARGET_DIR/hooks/codex-hooks.json"
    CODEX_CONFIG="$CODEX_DIR/config.toml"
    mkdir -p "$CODEX_DIR"
    if [ ! -f "$CODEX_HOOKS" ] && [ "$INSTALL_CODEX" = true ]; then
        printf '{"hooks":{}}\n' > "$CODEX_HOOKS"
    fi

    if [ "$INSTALL_CODEX" = true ]; then
        echo "Codex detected"
        echo "  Hooks -> $CODEX_HOOKS (SessionStart, UserPromptSubmit, PermissionRequest, PostToolUse, Interrupt, SessionEnd, Stop)"
        echo "  Next: run /hooks in Codex and trust the tmux-agent-indicator hooks"
    fi

    if [ -f "$CODEX_HOOKS" ]; then
        if [ "$UNINSTALL_CODEX" = true ]; then
            CODEX_MODE="uninstall"
        else
            CODEX_MODE="install"
        fi

        python3 - "$CODEX_HOOKS" "$CODEX_TEMPLATE" "$TARGET_DIR" "$CODEX_MODE" "$CODEX_CONFIG" "$SCRIPT_DIR/scripts/install-checks.py" "$INSTALL_WARNINGS" <<'PY'
import json
import pathlib
import re
import runpy
import sys

hooks_path = pathlib.Path(sys.argv[1])
template_path = pathlib.Path(sys.argv[2])
target_dir = sys.argv[3]
mode = sys.argv[4]
default_install_dir = "$HOME/.tmux/plugins/tmux-agent-indicator"

checks = runpy.run_path(sys.argv[6])
settings = checks["read_hooks"](hooks_path)
hooks = settings.setdefault("hooks", {})

def is_plugin_command(command):
    return ("scripts/agent-state.sh" in command and "--agent codex --state" in command
            or "scripts/agent-recovery.py" in command and "hook codex" in command)

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

# Check the installed hook positions: other tools may precede our hooks in each event.
# Disabled settings and trust decisions remain owned by Codex.
config_path = pathlib.Path(sys.argv[5])
if mode == "install" and config_path.is_file():
    def warn(message):
        with pathlib.Path(sys.argv[7]).open("a", encoding="utf-8") as report:
            print(message, file=report)

    config = checks["read_codex_config"](config_path)
    features = config.get("features", {})
    feature_key = "hooks" if "hooks" in features else "codex_hooks"
    if features.get(feature_key) is False:
        warn(f"Codex hooks are disabled in {config_path} ([features].{feature_key} = false). "
             f"Set {feature_key} = true under [features], then restart Codex for state updates.")

    states = config.get("hooks", {}).get("state", {})
    disabled = []
    for event, entries in hooks.items():
        if not isinstance(entries, list):
            continue
        event_key = re.sub(r"(?<!^)(?=[A-Z])", "_", event).lower()
        for group_index, entry in enumerate(entries):
            if not isinstance(entry, dict) or not isinstance(entry.get("hooks"), list):
                continue
            for hook_index, hook in enumerate(entry["hooks"]):
                command = hook.get("command", "") if isinstance(hook, dict) else ""
                if not isinstance(command, str) or not is_plugin_command(command):
                    continue
                hook_id = f"{hooks_path.resolve()}:{event_key}:{group_index}:{hook_index}"
                if states.get(hook_id, {}).get("enabled") is False and event not in disabled:
                    disabled.append(event)
    if disabled:
        warn("Disabled tmux-agent-indicator Codex hooks: " + ", ".join(disabled) + ". "
             "Enable them in /hooks, then restart Codex. State updates or conversation recovery may be incomplete.")
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
        # Installing into a running server must not replay an old layout. -o preserves any
        # startup restore already pending on this server.
        tmux set-option -goq @agent_recovery_startup ready
        tmux set-option -goq @agent_recovery_startup_reason 'installer reload'
        # The defaults file ends by sourcing ~/.tmux/agent-indicator.local.conf, so the user's
        # customizations are reloaded intact. The whole ~/.tmux.conf is not sourced: it may hold
        # commands such as new-session that must not run twice.
        if tmux source-file "$AGENT_CONF"; then
            echo "Running tmux server reloaded"
        else
            printf '%s\n' "Files installed, but the running tmux server could not reload the configuration. Fix the reported error, then run: tmux source-file $AGENT_CONF" >> "$INSTALL_WARNINGS"
        fi
    fi
    if [ "$INSTALL_CODEX" = true ]; then
        echo "Codex: start new sessions from a new shell (alias) and run /hooks to enable and trust the hooks."
    fi
    if [ "$SETUP_HAMMERSPOON" = true ]; then
        echo "Hammerspoon: grant Accessibility, enable Launch at login, and Reload Config from its menu."
    fi
    if [ -s "$INSTALL_WARNINGS" ]; then
        echo "Installation completed with warnings:" >&2
        while IFS= read -r warning; do
            if [ -t 2 ] && [ "${TERM:-}" != dumb ] && [ -z "${NO_COLOR+x}" ]; then
                printf '\033[33mWARNING: %s\033[0m\n' "$warning" >&2
            else
                printf 'WARNING: %s\n' "$warning" >&2
            fi
        done < "$INSTALL_WARNINGS"
    else
        echo "Installation checks passed."
    fi
fi

if [ "$UNINSTALL_CLAUDE" = true ]; then
    echo "Removed tmux-agent-indicator Claude hooks/status-line wrapper from: ${CLAUDE_CONFIG_DIR:-$HOME/.claude}/settings.json"
fi

if [ "$UNINSTALL_CODEX" = true ]; then
    echo "Removed tmux-agent-indicator Codex hooks from: ${CODEX_CONFIG_DIR:-${CODEX_HOME:-$HOME/.codex}}/hooks.json"
fi

if [ "$UNINSTALL_OPENCODE" = true ]; then
    echo "Removed tmux-agent-indicator OpenCode plugin from: ${XDG_CONFIG_HOME:-$HOME/.config}/opencode/plugins/"
fi

# Last: the hook uninstallers above still need the templates inside the plugin directory.
if [ "$UNINSTALL_ALL" = true ]; then
    rm -rf "$TARGET_DIR"
    echo "Removed $TARGET_DIR"
fi

