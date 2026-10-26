"""Framed-by-pickle records with bounded memo lifetime and atomic publication."""
from datetime import datetime, timezone
import hashlib
import os
from pathlib import Path
import pickle
import tempfile
from typing import Callable, Iterator

from .models import ArchiveResult, CoverageMetadata, FileCoverage, ParseStats, PickleFooter, PickleHeader
from .parser import CoverageReader

MAGIC = b'LLVM-COV-PY\x00\x01'


class ArchiveError(ValueError):
    pass


class _HashIO:
    def __init__(self, raw):
        self.raw = raw
        self.digest = hashlib.sha256()
        self.bytes = 0

    def _update(self, data):
        self.digest.update(data)
        self.bytes += len(data)
        return data

    def write(self, data):
        size = self.raw.write(data)
        if size != len(data):
            raise OSError('incomplete pickle write')
        self._update(data)
        return size

    def read(self, size=-1):
        return self._update(self.raw.read(size))

    def readline(self, size=-1):
        return self._update(self.raw.readline(size))


def write_pickle(reader: CoverageReader, destination, *, source='', test_name=None,
                 command=(), overwrite=False, finalize: Callable[[], None] | None = None):
    """Serialize one FileCoverage at a time; publish only after full validation.

    A fresh Pickler per record prevents its memo retaining the whole dataset.
    finalize is called after parsing but before publication (e.g. exporter wait).
    """
    path = Path(destination).resolve()
    path.parent.mkdir(parents=True, exist_ok=True)
    if path.exists() and not overwrite:
        raise FileExistsError(path)
    header = PickleHeader(1, datetime.now(timezone.utc).isoformat(), source, test_name, tuple(command))
    descriptor, temporary_name = tempfile.mkstemp(prefix='.' + path.name + '-', suffix='.tmp', dir=path.parent)
    temporary = Path(temporary_name)
    try:
        with os.fdopen(descriptor, 'wb') as raw:
            stream = _HashIO(raw)
            stream.write(MAGIC)
            pickle.dump(header, stream, protocol=5)
            for file in reader:
                pickle.dump(file, stream, protocol=5)
            if not reader.completed or reader.metadata is None:
                raise ArchiveError('JSON reader did not complete validation')
            if finalize is not None:
                finalize()
            footer = PickleFooter(reader.metadata, reader.stats, stream.digest.hexdigest(), stream.bytes)
            pickle.dump(footer, stream, protocol=5)
            raw.write(stream.digest.digest())
            raw.flush()
            os.fsync(raw.fileno())
        if overwrite:
            os.replace(temporary, path)
        else:
            # Atomic no-clobber publication, even if another writer created
            # the destination after the initial existence check.
            os.link(temporary, path)
            temporary.unlink()
        return ArchiveResult(str(path), header, footer, path.stat().st_size)
    finally:
        temporary.unlink(missing_ok=True)


class PickleReader:
    """Read archives from trusted sources only: pickle can execute Python code.

    File records are yielded before the footer is validated. Exhaust the iterator
    and check completed before treating the archive as fully verified.
    """
    def __init__(self, path):
        self.path = Path(path)
        self.header: PickleHeader | None = None
        self.footer: PickleFooter | None = None
        self.completed = False
        self._used = False

    def __iter__(self) -> Iterator[FileCoverage]:
        if self._used:
            raise RuntimeError('PickleReader is a one-pass iterator')
        self._used = True
        return self._iterate()

    def _iterate(self):
        files = segments = 0
        try:
            with self.path.open('rb') as raw:
                stream = _HashIO(raw)
                if stream.read(len(MAGIC)) != MAGIC:
                    raise ArchiveError('not a supported llvm-cov.py pickle archive')
                self.header = pickle.load(stream)
                if not isinstance(self.header, PickleHeader) or self.header.format_version != 1:
                    raise ArchiveError('unsupported pickle header')
                while True:
                    before_hash, before_bytes = stream.digest.hexdigest(), stream.bytes
                    item = pickle.load(stream)
                    if isinstance(item, FileCoverage):
                        files += 1
                        segments += len(item.segments)
                        yield item
                    elif isinstance(item, PickleFooter):
                        if not isinstance(item.stats, ParseStats) or not isinstance(item.metadata, CoverageMetadata):
                            raise ArchiveError('invalid pickle footer metadata')
                        if item.payload_sha256 != before_hash or item.payload_bytes != before_bytes:
                            raise ArchiveError('pickle payload checksum mismatch')
                        if item.stats.files != files or item.stats.segments != segments:
                            raise ArchiveError('pickle record counts do not match footer')
                        if raw.read(32) != stream.digest.digest():
                            raise ArchiveError('archive checksum mismatch')
                        if raw.read(1):
                            raise ArchiveError('trailing bytes after pickle footer')
                        self.footer, self.completed = item, True
                        return
                    else:
                        raise ArchiveError('unexpected pickle record type')
        except (EOFError, pickle.UnpicklingError) as exc:
            raise ArchiveError('truncated or malformed pickle archive') from exc
