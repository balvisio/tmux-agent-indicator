#!/usr/bin/env python3
"""Install pinned recovery plugins without changing user-managed plugin directories."""
import io
from pathlib import Path
import shutil
import sys
import tarfile
import tempfile
import urllib.request

REVISIONS = {
    "tmux-resurrect": "cff343cf9e81983d3da0c8562b01616f12e8d548",
    "tmux-continuum": "0698e8f4b17d6454c71bf5212895ec055c578da0",
}
MARKER = ".tmux-agent-indicator-managed"


def main():
    root = Path.home() / ".tmux/plugins"
    uninstall = sys.argv[1:] == ["--uninstall"]
    if not uninstall:
        root.mkdir(parents=True, exist_ok=True)
    for name, revision in REVISIONS.items():
        target = root / name
        owned = target.is_dir() and not target.is_symlink() and (target / MARKER).is_file()
        if uninstall:
            if owned:
                shutil.rmtree(target)
                print(f"Removed {target}")
            continue
        entry = name[len("tmux-"):] + ".tmux"
        if target.exists() or target.is_symlink():
            if not owned:
                if not (target / entry).is_file():
                    raise RuntimeError(f"{target} exists but is not a usable {name} installation")
                print(f"Using your existing {target}")
                continue
            if (target / MARKER).read_text().strip() == revision and (target / entry).is_file():
                continue
        url = f"https://codeload.github.com/tmux-plugins/{name}/tar.gz/{revision}"
        data = urllib.request.urlopen(url, timeout=30).read()
        with tempfile.TemporaryDirectory(prefix=".recovery-install-", dir=root) as work:
            stage = Path(work) / name
            stage.mkdir()
            links = []
            with tarfile.open(fileobj=io.BytesIO(data)) as archive:
                for member in archive.getmembers():
                    parts = Path(member.name).parts[1:]
                    if not parts:
                        continue
                    if ".." in parts or member.islnk():
                        raise RuntimeError("unsafe recovery plugin archive")
                    dest = stage.joinpath(*parts)
                    if member.issym():
                        (dest.parent / member.linkname).resolve().relative_to(stage.resolve())
                        links.append((dest, member.linkname))
                    elif member.isdir():
                        dest.mkdir(parents=True, exist_ok=True)
                    elif member.isfile():
                        dest.parent.mkdir(parents=True, exist_ok=True)
                        with archive.extractfile(member) as source, dest.open("wb") as output:
                            shutil.copyfileobj(source, output)
                        dest.chmod(member.mode & 0o755)
            for dest, link in links:
                dest.parent.mkdir(parents=True, exist_ok=True)
                dest.symlink_to(link)
            if not (stage / entry).is_file():
                raise RuntimeError(f"missing {entry} in downloaded archive")
            (stage / MARKER).write_text(revision + "\n")
            if target.exists():
                target.rename(Path(work) / "previous")
            stage.rename(target)
        print(f"Installed {name} at {revision[:12]}")


if __name__ == "__main__":
    main()
