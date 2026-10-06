"""История страниц памяти: локальный репозиторий git в каталоге страниц.

Репозиторий создаётся при первой сборке, живёт только на сервере: внешних адресов у него нет,
`push` и `fetch` здесь не вызываются вообще. Каждое изменение файлов сервисом — один коммит;
правка владельца, найденная в каталоге, сохраняется отдельным коммитом до того, как сервис
что-либо запишет.

Меры осторожности (каталог страниц читают и правят не только мы — владелец, Obsidian, агент):
  * git запускается без оболочки, списком аргументов, с пределом времени и чистым окружением;
    общие и пользовательские настройки git не читаются;
  * каталог репозитория и рабочий каталог задаются явно — git не ищет репозиторий выше по дереву;
  * хуки выключены, имена файлов передаются буквально (`--literal-pathspecs`);
  * перед каждым запуском проверяется `.git/config`: в нём допустимы только те ключи, которые
    пишем мы сами. Любой другой ключ (а через настройки git можно запустить программу) —
    и история не ведётся, о чём сообщает сборка и проверка страниц;
  * если git не установлен, страницы собираются как обычно, только «без истории».

Функции синхронные: сборка вызывает их через `asyncio.to_thread`.
"""

from __future__ import annotations

import os
import re
import shutil
import subprocess
from pathlib import Path
from typing import Sequence

from . import pages

TIMEOUT = 30  # секунд на одну команду git
NO_HISTORY = "без истории"
NAME = "Штурман"
EMAIL = "shturman@localhost"
OWNER_NAME = "Владелец"
OWNER_EMAIL = "owner@localhost"

# Что может стоять в .git/config: только записанное `git init` и нашей настройкой имени.
_ALLOWED_CONFIG = {
    "core": {"repositoryformatversion", "filemode", "bare", "logallrefupdates", "ignorecase",
             "precomposeunicode", "symlinks"},
    "user": {"name", "email"},
}
_SECTION = re.compile(r"\[([A-Za-z0-9.-]+)\]")
_SETTING = re.compile(r"([A-Za-z][A-Za-z0-9-]*)\s*=.*")


class GitError(RuntimeError):
    """Команда git не выполнилась. В тексте — только название шага, без вывода git."""


def config_is_plain(text: str) -> bool:
    """Правда ли в настройках репозитория нет ничего, кроме записанного нами."""
    section = None
    for raw in text.splitlines():
        line = raw.strip()
        if not line or line[0] in "#;":
            continue
        head = _SECTION.fullmatch(line)
        if head:
            section = head.group(1).lower()
            if section not in _ALLOWED_CONFIG:
                return False
            continue
        setting = _SETTING.fullmatch(line)
        if not setting or section is None or setting.group(1).lower() not in _ALLOWED_CONFIG[section]:
            return False
    return True


class History:
    """История одного каталога страниц. `problem` — почему история не ведётся (или None)."""

    def __init__(self, root: Path, *, binary: str | None = None, timeout: int = TIMEOUT) -> None:
        self.root = Path(os.path.realpath(root))
        self.binary = binary if binary is not None else shutil.which("git")
        self.timeout = timeout
        self.problem: str | None = None if self.binary else "git не установлен"

    @property
    def git_dir(self) -> Path:
        return self.root / ".git"

    # --- запуск ---

    def _env(self, author: tuple[str, str]) -> dict[str, str]:
        return {
            "PATH": os.environ.get("PATH", "/usr/bin:/bin"), "LC_ALL": "C", "HOME": str(self.root),
            "GIT_CONFIG_NOSYSTEM": "1", "GIT_CONFIG_GLOBAL": os.devnull, "GIT_TERMINAL_PROMPT": "0",
            "GIT_OPTIONAL_LOCKS": "0",
            "GIT_AUTHOR_NAME": author[0], "GIT_AUTHOR_EMAIL": author[1],
            "GIT_COMMITTER_NAME": NAME, "GIT_COMMITTER_EMAIL": EMAIL,
        }

    def _run(self, step: str, *args: str, author: tuple[str, str] = (NAME, EMAIL),
             ok: Sequence[int] = (0,), scoped: bool = True) -> subprocess.CompletedProcess:
        command = [
            str(self.binary), "-c", "core.hooksPath=" + os.devnull, "-c", f"safe.directory={self.root}",
            "-c", "core.fsmonitor=false", "-c", "commit.gpgsign=false", "-c", "gc.auto=0",
            "-c", "core.quotepath=false", "--literal-pathspecs", "--no-pager",
        ]
        if scoped:
            command += ["--git-dir", str(self.git_dir), "--work-tree", str(self.root)]
        try:
            done = subprocess.run(
                [*command, *args], cwd=self.root, env=self._env(author), stdin=subprocess.DEVNULL,
                capture_output=True, timeout=self.timeout, check=False)
        except subprocess.TimeoutExpired:
            raise GitError(f"{step}: git не ответил за {self.timeout} с") from None
        except OSError as exc:
            raise GitError(f"{step}: git не запустился ({type(exc).__name__})") from None
        if done.returncode not in ok:
            raise GitError(f"{step}: git завершился с кодом {done.returncode}")
        return done

    # --- готовность ---

    def ready(self, create: bool = True) -> bool:
        """Готовит репозиторий (создаёт при первом обращении) и проверяет его настройки.
        False — история сейчас не ведётся, причина в `problem`. С create=False ничего не
        создаёт: ещё не созданный репозиторий считается исправным."""
        if not self.binary:
            return False
        self.problem = None
        try:
            if self.git_dir.is_symlink() or (self.git_dir.exists() and not self.git_dir.is_dir()):
                self.problem = "каталог .git подменён: история не ведётся"
                return False
            if not self.git_dir.exists() and not create:
                return True
            if not self.git_dir.exists():
                self.root.mkdir(mode=0o700, parents=True, exist_ok=True)
                self._run("создание репозитория", "init", "-q", "--initial-branch=main", str(self.root),
                          scoped=False)
                self._run("настройка имени", "config", "--local", "user.name", NAME)
                self._run("настройка имени", "config", "--local", "user.email", EMAIL)
                os.chmod(self.git_dir, 0o700)
            config = (self.git_dir / "config").read_text(encoding="utf-8", errors="replace")
        except (GitError, OSError) as exc:
            self.problem = str(exc) if isinstance(exc, GitError) else "репозиторий страниц недоступен"
            return False
        if not config_is_plain(config):
            self.problem = "настройки git в каталоге страниц изменены вручную: история не ведётся"
            return False
        return True

    # --- чтение ---

    def changed(self) -> list[str]:
        """Файлы страниц, которые отличаются от последнего коммита: изменённые, новые, удалённые."""
        done = self._run("сверка каталога", "status", "--porcelain=v1", "-z", "--untracked-files=all",
                         "--no-renames")
        out = []
        for item in done.stdout.decode("utf-8", errors="replace").split("\0"):
            path = item[3:]
            if len(item) > 3 and pages.is_page_path(path):
                out.append(path)
        return sorted(set(out))

    def head(self) -> str | None:
        done = self._run("чтение истории", "rev-parse", "--short", "HEAD", ok=(0, 128))
        return done.stdout.decode().strip() or None if done.returncode == 0 else None

    def log(self, limit: int = 20) -> list[dict[str, str]]:
        """Последние коммиты: [{"sha", "author", "subject"}] — для кабинета и тестов."""
        done = self._run("чтение истории", "log", f"-{int(limit)}", "--format=%h%x1f%an%x1f%s", ok=(0, 128))
        if done.returncode != 0:
            return []
        rows = [line.split("\x1f") for line in done.stdout.decode("utf-8", errors="replace").splitlines()]
        return [{"sha": r[0], "author": r[1], "subject": r[2]} for r in rows if len(r) == 3]

    # --- запись ---

    def commit(self, paths: Sequence[str], message: str, *, by_owner: bool = False) -> str | None:
        """Коммит перечисленных файлов. Возвращает короткий идентификатор или None, если
        сохранять нечего. Другие файлы каталога в коммит не попадают."""
        paths = sorted({p for p in paths if pages.is_page_path(p)})
        if not paths:
            return None
        self._run("подготовка коммита", "add", "-A", "--", *paths)
        staged = self._run("подготовка коммита", "diff", "--cached", "--quiet", "--", *paths, ok=(0, 1))
        if staged.returncode == 0:
            return None
        author = (OWNER_NAME, OWNER_EMAIL) if by_owner else (NAME, EMAIL)
        self._run("коммит", "commit", "-q", "--no-verify", "--no-gpg-sign", "-m", message, "--", *paths,
                  author=author)
        return self.head()
