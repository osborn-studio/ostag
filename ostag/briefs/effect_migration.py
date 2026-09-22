"""Explicit owned Brief schema4->5 migration with pre-migration snapshot.

This does not install/restore a live backup or enable provider execution. Clients
must be stopped for migration; the same-connection data_version check also denies
an intervening commit between the read snapshot and exclusive migration lock.
SQLite backup API: https://www.sqlite.org/backup.html
"""
from pathlib import Path
import hashlib
import os
import sqlite3
from time import monotonic

from ostag.briefs import store as schema


def enable_effects(store, backup_path):
    if type(store) is not schema.BriefStore or not isinstance(backup_path, (str, Path)):
        raise schema.BriefSchemaError('owned Brief store and new backup path required')
    backup = Path(backup_path)
    if not str(backup_path).strip() or str(backup_path) == ':memory:' or not backup.parent.is_dir():
        raise schema.BriefSchemaError('new on-disk backup path required')
    deadline = monotonic() + 5
    def progress(status, remaining, total):
        if status in (sqlite3.SQLITE_BUSY, sqlite3.SQLITE_LOCKED) or monotonic() >= deadline:
            raise schema.StoreBusy('effect migration backup unavailable')
    try:
        with store._connection() as source:
            source.execute('PRAGMA foreign_keys=ON')
            source.execute('PRAGMA synchronous=FULL')
            source.execute('BEGIN')
            store._inspect(source)
            if source.execute('PRAGMA user_version').fetchone()[0] != schema.DELIVERY_SCHEMA_VERSION:
                raise schema.BriefSchemaError('effect migration requires existing schema4')
            version = source.execute('PRAGMA data_version').fetchone()[0]
            # O_EXCL rejects symlinks/existing destinations and cannot overwrite
            # the source inode. Parent provisioning remains an operator duty.
            descriptor = os.open(backup, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
            os.close(descriptor)
            target = sqlite3.connect(backup, timeout=0, isolation_level=None)
            target.row_factory = sqlite3.Row
            try:
                target.execute('PRAGMA synchronous=FULL')
                source.backup(target, pages=256, progress=progress, sleep=0)
                store._inspect(target)
                if target.execute('PRAGMA integrity_check').fetchone()[0] != 'ok':
                    raise schema.BriefSchemaError('effect migration backup failed validation')
            finally:
                target.close()
            with backup.open('rb') as saved:
                os.fsync(saved.fileno())
            directory = os.open(backup.parent, os.O_RDONLY | os.O_DIRECTORY)
            try:
                os.fsync(directory)
            finally:
                os.close(directory)
            source.rollback()
            source.execute('BEGIN EXCLUSIVE')
            store._inspect(source)
            if (source.execute('PRAGMA user_version').fetchone()[0] != schema.DELIVERY_SCHEMA_VERSION
                    or source.execute('PRAGMA data_version').fetchone()[0] != version):
                raise schema.Conflict('Brief changed after its backup; migration refused')
            for statement in schema._EFFECT_SCHEMA:
                source.execute(statement)
            epoch = hashlib.sha256(os.urandom(32)).hexdigest()
            source.execute("INSERT INTO brief_effect_meta VALUES(1,?,'initial',0)", (epoch,))
            source.execute(f'PRAGMA user_version={schema.EFFECT_SCHEMA_VERSION}')
            store._inspect(source)
            source.commit()
            return epoch
    except (OSError, sqlite3.Error):
        # No path, provider body, SQL or raw exception escapes this new surface.
        raise schema.StoreError('effect migration or snapshot unavailable') from None
