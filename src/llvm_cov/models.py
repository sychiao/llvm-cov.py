"""Stable, pickleable file-level coverage models; no database or Git types."""
from dataclasses import dataclass
from itertools import groupby
from typing import Iterator


@dataclass(frozen=True, slots=True)
class Segment:
    line: int
    column: int
    count: int
    has_count: bool
    is_region_entry: bool
    is_gap_region: bool


@dataclass(frozen=True, slots=True)
class LineCoverage:
    line: int
    count: int

    @property
    def hit(self) -> bool:
        return self.count > 0


@dataclass(frozen=True, slots=True)
class MetricSummary:
    count: int
    covered: int
    percent: float
    notcovered: int | None = None


@dataclass(frozen=True, slots=True)
class FileSummary:
    lines: MetricSummary | None = None
    regions: MetricSummary | None = None
    functions: MetricSummary | None = None
    instantiations: MetricSummary | None = None


@dataclass(frozen=True, slots=True)
class FileCoverage:
    filename: str
    segments: tuple[Segment, ...]
    summary: FileSummary | None = None
    data_index: int = 0

    def iter_lines(self) -> Iterator[LineCoverage]:
        """LLVM 18/21 LineCoverageStats semantics, including gaps and EOF.

        Unmapped lines are absent; absence must not be interpreted as uncovered.
        Counts are derived lazily and are not duplicated in the pickle payload.
        """
        wrapped = None
        previous_line = None
        for line, values in groupby(self.segments, key=lambda s: s.line):
            if previous_line is not None and wrapped is not None and wrapped.has_count:
                for missing in range(previous_line + 1, line):
                    yield LineCoverage(missing, wrapped.count)
            current = list(values)
            entries = [s for s in current if s.has_count and s.is_region_entry and not s.is_gap_region]
            skipped = not current[0].has_count and current[0].is_region_entry
            mapped = ((not skipped and ((wrapped is not None and wrapped.has_count) or bool(entries)))
                      or any(s.has_count and s.is_region_entry for s in current))
            if mapped:
                count = max([wrapped.count if wrapped is not None else 0] + [s.count for s in entries])
                yield LineCoverage(line, count)
            wrapped = current[-1]
            previous_line = line


@dataclass(frozen=True, slots=True)
class UnitSummary:
    data_index: int
    totals: FileSummary | None


@dataclass(frozen=True, slots=True)
class CoverageMetadata:
    json_type: str
    json_version: str
    units: tuple[UnitSummary, ...]


@dataclass(frozen=True, slots=True)
class ParseStats:
    bytes_read: int
    files: int
    segments: int
    source_sha256: str
    elapsed_seconds: float
    first_byte_seconds: float | None
    first_file_seconds: float | None


@dataclass(frozen=True, slots=True)
class PickleHeader:
    format_version: int
    created_at: str
    source: str
    test_name: str | None = None
    command: tuple[str, ...] = ()


@dataclass(frozen=True, slots=True)
class PickleFooter:
    metadata: CoverageMetadata
    stats: ParseStats
    payload_sha256: str
    payload_bytes: int


@dataclass(frozen=True, slots=True)
class ArchiveResult:
    path: str
    header: PickleHeader
    footer: PickleFooter
    size_bytes: int
