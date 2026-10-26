"""Incremental JSON event handling; only selected file fields are materialized."""
import hashlib
import math
import time
from typing import BinaryIO, Iterator

import ijson
from ijson.common import ObjectBuilder

from .models import (CoverageMetadata, FileCoverage, FileSummary, MetricSummary,
                     ParseStats, Segment, UnitSummary)


class CoverageParseError(ValueError):
    pass


class _MeteredReader:
    def __init__(self, stream: BinaryIO, started: float):
        self.stream, self.started = stream, started
        self.bytes_read = 0
        self.digest = hashlib.sha256()
        self.first_byte_seconds = None

    def read(self, size=-1):
        if size == 0:
            return b''
        method = getattr(self.stream, 'read1', self.stream.read) if size > 0 else self.stream.read
        data = method(size)
        if not isinstance(data, bytes):
            raise CoverageParseError('input must be a binary stream returning bytes')
        if data:
            if self.first_byte_seconds is None:
                self.first_byte_seconds = time.monotonic() - self.started
            self.bytes_read += len(data)
            self.digest.update(data)
        return data


def _integer(value, minimum=0):
    if type(value) is not int or value < minimum:
        raise CoverageParseError(f'expected integer >= {minimum}, got {value!r}')
    return value


def _summary(raw):
    if raw is None:
        return None
    if not isinstance(raw, dict):
        raise CoverageParseError('coverage summary must be an object')
    values = {}
    for name in ('lines', 'regions', 'functions', 'instantiations'):
        if name not in raw:
            continue
        item = raw[name]
        if not isinstance(item, dict):
            raise CoverageParseError('summary metric must be an object')
        count, covered = _integer(item['count']), _integer(item['covered'])
        percent = item['percent']
        if type(percent) not in (int, float) or not math.isfinite(percent) or not 0 <= percent <= 100:
            raise CoverageParseError('invalid coverage percentage')
        missing = _integer(item['notcovered']) if 'notcovered' in item else None
        if covered > count or (missing is not None and missing != count - covered):
            raise CoverageParseError('inconsistent summary counts')
        values[name] = MetricSummary(count, covered, float(percent), missing)
    return FileSummary(**values)


def _segment(values):
    if len(values) != 6:
        raise CoverageParseError('a coverage segment must have six fields')
    for index in (0, 1, 2):
        _integer(values[index], 0 if index == 2 else 1)
    if any(type(value) is not bool for value in values[3:]):
        raise CoverageParseError('segment flags must be booleans')
    return Segment(*values)


class CoverageReader:
    """One-pass file iterator. Metadata is certified only after full exhaustion.

    Supports file-level segment schemas from LLVM JSON v2 and v3. Branch,
    MC/DC, expansion and function-record payloads are consumed but not retained.
    """
    def __init__(self, stream: BinaryIO, *, buffer_size: int = 65536):
        if buffer_size < 1:
            raise ValueError('buffer_size must be positive')
        self.started = time.monotonic()
        self.stream = _MeteredReader(stream, self.started)
        self.buffer_size = buffer_size
        self.completed = False
        self.metadata: CoverageMetadata | None = None
        self.files = self.segments = 0
        self.first_file_seconds = None
        self._used = False

    @property
    def stats(self):
        return ParseStats(self.stream.bytes_read, self.files, self.segments,
                          self.stream.digest.hexdigest(), time.monotonic() - self.started,
                          self.stream.first_byte_seconds, self.first_file_seconds)

    def __iter__(self) -> Iterator[FileCoverage]:
        if self._used:
            raise RuntimeError('CoverageReader is a one-pass iterator')
        self._used = True
        return self._iterate()

    def _iterate(self):
        root_fields, metadata = set(), {}
        unit_index = -1
        units = []
        filename = None
        segments = None
        segment_values = None
        summary = None
        file_fields = set()
        builder = builder_prefix = builder_kind = None
        file_prefix = 'data.item.files.item'
        segment_prefix = file_prefix + '.segments.item'
        try:
            for prefix, event, value in ijson.parse(self.stream, buf_size=self.buffer_size, use_float=True):
                if prefix == '' and event == 'map_key':
                    if value in root_fields:
                        raise CoverageParseError('duplicate root field: ' + value)
                    root_fields.add(value)
                if prefix in ('type', 'version') and event == 'string':
                    metadata[prefix] = value
                if prefix == 'data' and event != 'map_key' and event not in ('start_array', 'end_array'):
                    raise CoverageParseError('data must be an array')
                if prefix == 'data.item' and event not in ('start_map', 'end_map', 'map_key'):
                    raise CoverageParseError('each data entry must be an object')
                if prefix == 'data.item.files' and event not in ('start_array', 'end_array'):
                    raise CoverageParseError('files must be an array')
                if prefix == file_prefix and event not in ('start_map', 'end_map', 'map_key'):
                    raise CoverageParseError('each file entry must be an object')
                if builder is not None:
                    builder.event(event, value)
                    if prefix == builder_prefix and event == 'end_map':
                        parsed = _summary(builder.value)
                        if builder_kind == 'file':
                            summary = parsed
                        else:
                            units[unit_index] = UnitSummary(unit_index, parsed)
                        builder = builder_prefix = builder_kind = None
                    continue
                if prefix == 'data.item' and event == 'start_map':
                    unit_index += 1
                    units.append(UnitSummary(unit_index, None))
                elif prefix in ('data.item.totals', file_prefix + '.summary'):
                    if event != 'start_map':
                        raise CoverageParseError('summary must be an object')
                    builder, builder_prefix = ObjectBuilder(), prefix
                    builder_kind = 'file' if prefix.startswith(file_prefix) else 'unit'
                    builder.event(event, value)
                elif prefix == file_prefix and event == 'start_map':
                    filename, segments, summary = None, None, None
                    file_fields = set()
                elif prefix == file_prefix and event == 'map_key':
                    if value in file_fields:
                        raise CoverageParseError('duplicate file field: ' + value)
                    file_fields.add(value)
                elif prefix == file_prefix + '.filename':
                    if event != 'string' or not value:
                        raise CoverageParseError('filename must be a nonempty string')
                    filename = value
                elif prefix == file_prefix + '.segments':
                    if event == 'start_array':
                        segments = []
                    elif event != 'end_array':
                        raise CoverageParseError('segments must be an array')
                elif prefix == segment_prefix:
                    if event == 'start_array':
                        segment_values = []
                    elif event == 'end_array':
                        segment = _segment(segment_values)
                        if segments and (segment.line, segment.column) < (segments[-1].line, segments[-1].column):
                            raise CoverageParseError('segments must be ordered by source position')
                        segments.append(segment)
                        segment_values = None
                    else:
                        raise CoverageParseError('each segment must be an array')
                elif prefix == segment_prefix + '.item':
                    if event not in ('number', 'boolean') or segment_values is None:
                        raise CoverageParseError('invalid segment field')
                    segment_values.append(value)
                elif prefix == file_prefix and event == 'end_map':
                    if filename is None or segments is None:
                        raise CoverageParseError('file requires filename and segments; summary-only export is unsupported')
                    self.files += 1
                    self.segments += len(segments)
                    if self.first_file_seconds is None:
                        self.first_file_seconds = time.monotonic() - self.started
                    yield FileCoverage(filename, tuple(segments), summary, unit_index)
                    segments = None
            if metadata.get('type') != 'llvm.coverage.json.export':
                raise CoverageParseError('not an LLVM coverage JSON export')
            if not isinstance(metadata.get('version'), str) or metadata['version'].split('.')[0] not in ('2', '3'):
                raise CoverageParseError('supported LLVM JSON major versions are 2 and 3')
            if 'data' not in root_fields:
                raise CoverageParseError('missing data array')
            self.metadata = CoverageMetadata(metadata['type'], metadata['version'], tuple(units))
            self.completed = True
        except CoverageParseError:
            raise
        except (ijson.JSONError, KeyError, TypeError, IndexError) as exc:
            raise CoverageParseError(str(exc)) from exc
