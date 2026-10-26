"""Incremental LLVM coverage parsing and typed, streaming pickle storage."""
from .models import (ArchiveResult, CoverageMetadata, FileCoverage, FileSummary,
                     LineCoverage, MetricSummary, ParseStats, PickleFooter,
                     PickleHeader, Segment, UnitSummary)
from .parser import CoverageParseError, CoverageReader
from .archive import ArchiveError, PickleReader, write_pickle
from .process import ExportError, export_pickle

__version__ = '0.1.0'
__all__ = ['ArchiveResult', 'CoverageMetadata', 'FileCoverage', 'FileSummary',
           'LineCoverage', 'MetricSummary', 'ParseStats', 'PickleFooter',
           'PickleHeader', 'Segment', 'UnitSummary', 'CoverageReader',
           'CoverageParseError', 'ArchiveError', 'PickleReader', 'write_pickle',
           'ExportError', 'export_pickle']
