"""Хранилище состояния плагина: небольшие JSON-файлы в каталоге данных плагина.

Дашборд и шлюз Hermes — разные процессы, общего у них только диск. Поэтому каждая запись
атомарна (временный файл и переименование), а изменение «прочитал — поправил — записал»
идёт под файловой блокировкой.
"""

from __future__ import annotations

import fcntl
import json
import os
import secrets
import tempfile
from contextlib import contextmanager
from pathlib import Path
from typing import Any, Iterator

PLUGIN_NAME = "shturman"
# Отметка «привязку владельца сбросил сам плагин» (вход по ссылке восстановления).
OWNER_UNBOUND = "owner_unbound"


def default_state_dir() -> Path:
    """Каталог состояния: `SHTURMAN_STATE_DIR` либо `<HERMES_HOME>/plugin-data/shturman`."""
    explicit = os.environ.get("SHTURMAN_STATE_DIR", "").strip()
    if explicit:
        return Path(explicit)
    try:  # внутри Hermes — та же функция, что у штатного plugin_data_dir
        from hermes_constants import get_hermes_home  # type: ignore

        home = Path(get_hermes_home())
    except Exception:
        home = Path(os.environ.get("HERMES_HOME") or Path.home() / ".hermes")
    return home / "plugin-data" / PLUGIN_NAME


class StoreReadError(Exception):
    """Файл состояния есть (или каталог недоступен), но прочитать его сейчас не удалось."""


class Store:
    def __init__(self, root: Path | None = None) -> None:
        self.root = Path(root) if root is not None else default_state_dir()

    # --- служебное ---

    def _ensure_root(self) -> None:
        if self.root.is_dir():
            return
        # Владельца берём у ближайшего уже существующего каталога: сами созданные каталоги
        # при запуске от root принадлежали бы root.
        anchor = self.root
        while not anchor.exists() and anchor != anchor.parent:
            anchor = anchor.parent
        missing = [p for p in [self.root, *self.root.parents] if not p.exists()]
        self.root.mkdir(parents=True, exist_ok=True)
        try:
            os.chmod(self.root, 0o700)
        except OSError:
            pass
        for created in reversed(missing):
            self._fix_owner(created, anchor)

    def _fix_owner(self, path: Path, anchor: Path | None = None) -> None:
        """Запущено от root (например, через docker exec) — отдать файл владельцу данных Hermes.

        Иначе процесс Hermes, работающий под обычным пользователем, не прочитает файл с правами 600.
        """
        if not hasattr(os, "geteuid") or os.geteuid() != 0:
            return
        try:
            st = (anchor or self.root).stat()
            os.chown(path, st.st_uid, st.st_gid)
        except OSError:
            pass

    def _path(self, name: str) -> Path:
        if not name.replace("_", "").replace("-", "").isalnum():
            raise ValueError(f"недопустимое имя файла состояния: {name!r}")
        return self.root / f"{name}.json"

    # --- чтение и запись ---

    def read(self, name: str) -> dict[str, Any]:
        path = self._path(name)
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return {}
        return data if isinstance(data, dict) else {}

    def read_strict(self, name: str) -> dict[str, Any] | None:
        """Как `read`, но различает «файла нет» и «не удалось прочитать».

        None — файла точно нет: каталог состояния на месте, а файла в нём нет. `StoreReadError` —
        прочитать не удалось (ошибка диска, каталог пропал, содержимое повреждено): по такому
        ответу нельзя заключать, что записи нет. Нужен там, где отсутствие записи ведёт к
        необратимому действию — например, к сообщению сервису «владелец отвязан».
        """
        path = self._path(name)
        try:
            text = path.read_text(encoding="utf-8")
        except FileNotFoundError:
            if not self.root.is_dir():
                raise StoreReadError("каталог состояния недоступен") from None
            return None
        except OSError as exc:
            raise StoreReadError(type(exc).__name__) from None
        try:
            data = json.loads(text)
        except ValueError:
            raise StoreReadError("файл состояния повреждён") from None
        if not isinstance(data, dict):
            raise StoreReadError("файл состояния повреждён")
        return data

    def write(self, name: str, data: dict[str, Any]) -> None:
        self._ensure_root()
        path = self._path(name)
        fd, tmp = tempfile.mkstemp(dir=self.root, prefix=f".{name}.", suffix=".tmp")
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as fh:
                json.dump(data, fh, ensure_ascii=False, separators=(",", ":"))
                fh.flush()
                os.fsync(fh.fileno())
            os.chmod(tmp, 0o600)
            self._fix_owner(Path(tmp))
            os.replace(tmp, path)
        except BaseException:
            try:
                os.unlink(tmp)
            except OSError:
                pass
            raise

    def delete(self, name: str) -> None:
        try:
            self._path(name).unlink()
        except OSError:
            pass

    def mtime(self, name: str) -> float:
        try:
            return self._path(name).stat().st_mtime
        except OSError:
            return 0.0

    @contextmanager
    def locked(self, name: str) -> Iterator[dict[str, Any]]:
        """Читает файл под блокировкой, отдаёт словарь на правку и записывает его при выходе."""
        self._ensure_root()
        lock_path = self.root / f".{name}.lock"
        with open(lock_path, "a+") as lock:
            self._fix_owner(lock_path)
            fcntl.flock(lock, fcntl.LOCK_EX)
            try:
                data = self.read(name)
                before = json.dumps(data, sort_keys=True)
                yield data
                if json.dumps(data, sort_keys=True) != before:
                    self.write(name, data)
            finally:
                fcntl.flock(lock, fcntl.LOCK_UN)

    # --- ключ подписи ---

    def secret(self) -> bytes:
        """Ключ подписи сессий. Создаётся один раз; оба процесса читают один и тот же файл."""
        self._ensure_root()
        path = self.root / "secret.key"
        try:
            data = path.read_bytes()
            if len(data) >= 32:
                return data
        except OSError:
            pass
        lock_path = self.root / ".secret.lock"
        with open(lock_path, "a+") as lock:
            self._fix_owner(lock_path)
            fcntl.flock(lock, fcntl.LOCK_EX)
            try:
                try:
                    data = path.read_bytes()
                    if len(data) >= 32:
                        return data
                except OSError:
                    pass
                data = secrets.token_bytes(32)
                fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
                with os.fdopen(fd, "wb") as fh:
                    fh.write(data)
                    fh.flush()
                    os.fsync(fh.fileno())
                self._fix_owner(path)
                return data
            finally:
                fcntl.flock(lock, fcntl.LOCK_UN)
