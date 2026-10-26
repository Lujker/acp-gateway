"""Stdlib-only local inventory, also executed verbatim inside WSL.

No remote arguments, shell commands, login checks or agent processes here.
"""

import json
import os
import shutil
import sys
from pathlib import Path


def inventory():
    home = Path.home()
    directories = [home / ".local/bin", home / ".npm-global/bin", home / ".volta/bin"]
    for pattern in (".nvm/versions/node/*/bin", ".local/share/mise/installs/node/*/bin"):
        directories.extend(sorted(home.glob(pattern), reverse=True)[:8])
    directories += [Path("/opt/homebrew/bin"), Path("/usr/local/bin"), Path("/usr/bin")]
    windows = sys.platform == "win32"
    if windows:
        directories = [home / ".local/bin", home / "scoop/shims"]
        for key in ("APPDATA", "LOCALAPPDATA"):
            if os.environ.get(key):
                suffix = "npm" if key == "APPDATA" else "Microsoft/WinGet/Links"
                directories.append(Path(os.environ[key]) / suffix)
    agents = []
    for kind, name in (("codex", "codex"), ("claude_code", "claude")):
        found = shutil.which(name)
        choices = ([Path(found)] if found else []) + [
            folder / (name + ".exe" if windows else name) for folder in directories
        ]
        if windows and kind == "codex":
            # npm's .cmd shim cannot be launched with CreateProcess. Use its native binary.
            npm_roots = [p.parent for p in choices] + directories
            for root in npm_roots:
                for pattern in (
                    "node_modules/@openai/codex/vendor/*/codex/codex.exe",
                    "node_modules/@openai/codex/node_modules/@openai/codex-*/vendor/*/codex/codex.exe",
                    "node_modules/@openai/codex-*/vendor/*/codex/codex.exe",
                ):
                    choices.extend(sorted(root.glob(pattern))[:4])
        for path in choices:
            if windows and path.suffix.lower() != ".exe":
                continue
            if not windows and path.suffix.lower() in {".exe", ".cmd", ".bat"}:
                continue
            if path.is_file() and os.access(path, os.X_OK):
                executable = str(path.absolute())
                path_dirs = list(dict.fromkeys([str(path.parent), *os.get_exec_path()]))
                if not windows:
                    path_dirs = [
                        p
                        for p in path_dirs
                        if p.startswith("/")
                        and ":" not in p
                        and "\\" not in p
                        and ".." not in p.split("/")
                        and all(c.isprintable() for c in p)
                    ]
                agents.append({"kind": kind, "executable": executable, "path": path_dirs[:16]})
                break
    return {
        "home": str(home),
        "user": __import__("pwd").getpwuid(os.getuid()).pw_name if not windows else "",
        "python": sys.executable,
        "agents": agents,
    }


if __name__ == "__main__":
    print(json.dumps(inventory()))
