"""One-time recovery of a database kept outside the bot's persistent Compose directory."""
from __future__ import annotations

import fcntl
import json
import os
import posixpath
import sqlite3
import subprocess
import sys
from contextlib import closing
from itertools import zip_longest
from pathlib import Path, PurePosixPath
from tempfile import TemporaryDirectory


def import_database(source: Path, live: Path, work: Path, *, offline_copy: bool = False) -> None:
    if live.exists() and source.samefile(live):
        return
    snapshot = work / "snapshot.sqlite3"
    mode = "rw" if offline_copy else "ro"  # SQLite can recover a hot journal on the private copy.
    with closing(sqlite3.connect(source.as_uri() + "?mode=" + mode, uri=True)) as old, \
         closing(sqlite3.connect(snapshot)) as copy:
        if not old.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='video_counts'").fetchone():
            raise ValueError(f"В {source} отсутствует таблица результатов")
        old.backup(copy)
        copy.execute("PRAGMA journal_mode=DELETE")
        if copy.execute("PRAGMA quick_check").fetchall() != [("ok",)]:
            raise ValueError("Копия не прошла проверку SQLite")
        snapshot.chmod(0o600)
        if live.exists():
            with closing(sqlite3.connect(live.as_uri() + "?mode=ro", uri=True)) as existing:
                if all(a == b for a, b in zip_longest(copy.iterdump(), existing.iterdump())):
                    return
            rescue = live.parent / f"legacy-{work.name}.sqlite3"
            os.link(snapshot, rescue)
            raise ValueError(f"В {live} другая база. Копия старой базы сохранена в {rescue}.")
        if any(Path(str(live) + suffix).exists() for suffix in ("-wal", "-shm", "-journal")):
            raise ValueError(f"Рядом с {live} найден журнал без основного файла")
        os.link(snapshot, live)


def migrate(root: Path, native_source: Path | None = None) -> None:
    data = root / "data"
    live = data / "pushups.sqlite3"
    data.mkdir(mode=0o700, exist_ok=True)
    with (data / ".migration.lock").open("a") as lock, TemporaryDirectory(dir=data) as temp:
        fcntl.flock(lock, fcntl.LOCK_EX)
        work = Path(temp)
        ids = subprocess.check_output(["docker", "compose", "ps", "--all", "--quiet", "bot"],
                                      cwd=root, text=True).split()
        if len(ids) > 1:
            raise ValueError("Обнаружено несколько контейнеров bot. Уточните источник базы.")
        info = json.loads(subprocess.check_output(["docker", "inspect", ids[0]]))[0] if ids else None
        if info is None or info["State"]["Status"] == "created":
            source = native_source or (live if live.is_file() else root / "pushups.sqlite3")
            if source.is_file():
                import_database(source, live, work)
            elif native_source is not None:
                raise FileNotFoundError(source)
            elif any(data.glob("*.sqlite3-*")) or any((data / "backups").glob("*.sqlite3")):
                raise ValueError(f"В {data} есть журнал или резервная копия. Проверьте рабочую базу.")
            return
        if native_source is not None:
            raise ValueError("Для переноса нужен один старый контейнер или путь к базе остановленного бота")
        container = ids[0]
        config = info["Config"]
        raw = next((v.partition("=")[2] for v in config["Env"] if v.startswith("DB_PATH=")), "./data/pushups.sqlite3")
        if not raw:
            raise ValueError("Пустой DB_PATH у старого контейнера")
        path = PurePosixPath(posixpath.normpath(posixpath.join(config["WorkingDir"] or "/", raw)))
        for mount in sorted(info["Mounts"], key=lambda m: len(m["Destination"]), reverse=True):
            directory = PurePosixPath(mount["Destination"])
            if directory != path and directory not in path.parents:
                continue
            if mount["Type"] == "bind":
                host_path = Path(mount["Source"]) / path.relative_to(directory)
                if host_path.resolve() == live.resolve() and live.is_file():
                    return
            if mount["Type"] == "tmpfs":
                raise ValueError("База находится в tmpfs. Сохраните её из работающего контейнера до остановки.")
            break
        running = info["State"]["Running"]
        if running:
            subprocess.run(["docker", "stop", container], check=True)
        try:
            source = work / "old.sqlite3"
            for suffix in ("", "-wal", "-journal"):
                result = subprocess.run(["docker", "cp", "-L", f"{container}:{path}{suffix}", str(source) + suffix],
                                        capture_output=True, text=True)
                if result.returncode:
                    missing = f"could not find the file {path}{suffix} in container".lower()
                    if suffix and missing in result.stderr.lower():
                        continue
                    raise RuntimeError(result.stderr.strip())
            import_database(source, live, work, offline_copy=True)
        except BaseException:
            if running:
                subprocess.run(["docker", "start", container], check=True)
            raise


if __name__ == "__main__":
    import argparse

    parser = argparse.ArgumentParser(description="Перенести старую базу в data перед первым обновлением")
    parser.add_argument("--source-db", type=Path, help="файл остановленного бота при переходе с запуска без Docker")
    options = parser.parse_args()
    try:
        migrate(Path(__file__).resolve().parents[1],
                options.source_db.expanduser().resolve() if options.source_db is not None else None)
    except (OSError, sqlite3.Error, subprocess.CalledProcessError, ValueError, RuntimeError) as exc:
        sys.exit(f"Перенос остановлен. {exc}")
    print("Можно запускать docker compose up -d --build.")
