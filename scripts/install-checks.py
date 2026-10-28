#!/usr/bin/env python3
"""Check installation inputs and destinations before changing user files."""
import argparse
import json
import os
from pathlib import Path
import re
import shutil
import subprocess
import sys


def read_hooks(path):
    if not path.exists() and not path.is_symlink():
        return {}
    try:
        settings = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as error:
        raise ValueError(f"Cannot read valid JSON from {path}; fix the file before installing.") from error
    if not isinstance(settings, dict) or not isinstance(settings.get("hooks", {}), dict):
        raise ValueError(f"Invalid settings in {path}: settings and hooks must be objects.")
    for event, groups in settings.get("hooks", {}).items():
        if not isinstance(groups, list) or any(
            not isinstance(group, dict) or not isinstance(group.get("hooks"), list)
            or any(not isinstance(hook, dict) for hook in group["hooks"]) for group in groups
        ):
            raise ValueError(f"Invalid hook groups for {event} in {path}; fix the file before installing.")
    return settings


def read_codex_config(path):
    if not path.exists() and not path.is_symlink():
        return {}
    try:
        import tomllib
    except ImportError:
        try:
            import tomli as tomllib
        except ImportError as error:
            raise ValueError(f"Checking {path} requires Python 3.11+ or tomli for Python 3.8–3.10.") from error
    try:
        config = tomllib.loads(path.read_text(encoding="utf-8"))
        features = config.get("features", {})
        hooks = config.get("hooks", {})
        if not isinstance(features, dict) or not isinstance(hooks, dict):
            raise ValueError("features/hooks must be tables")
        states = hooks.get("state", {})
        if not isinstance(states, dict) or any(not isinstance(value, dict) for value in states.values()):
            raise ValueError("hook states must be tables")
        return config
    except (OSError, ValueError) as error:
        raise ValueError(f"Cannot read valid Codex configuration from {path}; fix the file before installing.") from error


def writable(path, directory=False):
    if path.exists() or path.is_symlink():
        if not (path.is_dir() if directory else path.is_file()):
            raise ValueError(f"Expected a {'directory' if directory else 'file'} at {path}.")
        if not os.access(path, os.W_OK | (os.X_OK if directory else 0)):
            raise ValueError(f"Not writable: {path}. Correct its permissions or ownership before installing.")
    parent = path.parent
    while not parent.exists() and not parent.is_symlink():
        parent = parent.parent
    if not parent.is_dir() or not os.access(parent, os.W_OK | os.X_OK):
        raise ValueError(f"Cannot write {path}: parent {parent} must be a writable directory.")


def output(command):
    return subprocess.check_output(command, text=True, stderr=subprocess.DEVNULL,
                                   stdin=subprocess.DEVNULL, timeout=5).strip()


def version(command, pattern, minimum, name):
    try:
        detected = output(command)
    except (OSError, subprocess.SubprocessError) as error:
        raise ValueError(f"Cannot run {name}; install it and make it available on PATH.") from error
    match = re.search(pattern, detected)
    if not match or tuple(map(int, match.groups())) < minimum:
        required = ".".join(map(str, minimum))
        raise ValueError(f"{name} {required}+ required; found {detected or 'an unknown version'}.")


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--warnings", type=Path, required=True)
    parser.add_argument("--claude", type=Path)
    parser.add_argument("--codex", type=Path)
    parser.add_argument("--opencode", type=Path)
    parser.add_argument("--tmux-conf", action="store_true")
    parser.add_argument("--hammerspoon", action="store_true")
    parser.add_argument("--startup-file", type=Path, action="append", default=[])
    parser.add_argument("--uninstall", action="store_true")
    args = parser.parse_args()
    errors, warnings = [], []

    def check(function, *values):
        try:
            return function(*values)
        except (OSError, ValueError) as error:
            errors.append(str(error))
            return None

    if not args.uninstall:
        check(version, ["bash", "-c", 'printf "%s" "$BASH_VERSION"'], r"^(\d+)\.(\d+)", (4, 0), "Bash")
        check(version, ["tmux", "-V"], r"^tmux (\d+)\.(\d+)", (3, 6), "tmux")

    if args.claude:
        settings = check(read_hooks, args.claude / "settings.json")
        if not args.uninstall and settings and settings.get("disableAllHooks") is True:
            warnings.append(f"Claude hooks are disabled in {args.claude / 'settings.json'} "
                            "(disableAllHooks = true). Enable them there for state updates and recovery.")
    if args.codex:
        check(read_hooks, args.codex / "hooks.json")
        check(read_codex_config, args.codex / "config.toml")
        if not args.uninstall:
            try:
                help_text = output(["codex", "--help"])
                if not re.search(r"(?<![\w-])--no-daemon(?![\w-])", help_text):
                    warnings.append("Codex does not advertise --no-daemon. Update Codex before using the indicator alias or recovery.")
            except (OSError, subprocess.SubprocessError):
                warnings.append("Cannot check Codex --no-daemon support. Ensure codex is on PATH and check codex --help.")

    if not args.uninstall:
        home = Path.home()
        target = home / ".tmux/plugins/tmux-agent-indicator"
        # A development symlink is replaced, so only its parent needs write access.
        if target.is_symlink():
            check(writable, target.parent, True)
        else:
            check(writable, target, True)
            source = Path(__file__).resolve().parent.parent
            patterns = ("agent-indicator.tmux", "install.sh", "README.md", "LICENSE", "scripts/*.sh",
                        "scripts/*.py", "scripts/slack-badge.js", "hooks/*.json", "plugins/*.js",
                        "licenses/*.txt", "config/agent-indicator.conf", "integrations/hammerspoon/*.lua")
            for pattern in patterns:
                for path in source.glob(pattern):
                    check(writable, target / path.relative_to(source))
        files = list(args.startup_file)
        if args.tmux_conf:
            files.extend([home / ".tmux.conf", home / ".tmux/agent-indicator.conf"])
            for name in ("tmux-resurrect", "tmux-continuum"):
                dependency = target.parent / name
                if (dependency / ".tmux-agent-indicator-managed").is_file():
                    check(writable, dependency, True)
        if args.claude:
            files.append(args.claude / "settings.json")
        if args.codex:
            files.extend([args.codex / "hooks.json", args.codex / "config.toml"])
        if args.opencode:
            files.append(args.opencode / "plugins/opencode-tmux-agent-indicator.js")
        if args.hammerspoon:
            files.extend([home / ".hammerspoon/agent_dots.lua", home / ".hammerspoon/init.lua"])
        if args.tmux_conf or args.hammerspoon or args.startup_file:
            files.append(home / ".tmux/agent-indicator.installed")
        for path in set(files):
            check(writable, path)
        if (args.tmux_conf or args.startup_file) and not shutil.which("autossh"):
            warnings.append("ssh-reconnect needs autossh. Install it with brew install autossh or your package manager.")

    if errors:
        for message in dict.fromkeys(errors):
            print("ERROR: " + message, file=sys.stderr)
        print("Installation stopped before changing installed files or configuration.", file=sys.stderr)
        return 1
    with args.warnings.open("a", encoding="utf-8") as report:
        for message in warnings:
            print(message, file=report)
    return 0


if __name__ == "__main__":
    sys.exit(main())
