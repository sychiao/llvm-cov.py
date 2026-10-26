import io
import json
from pathlib import Path
import sys
import tempfile
import unittest

from llvm_cov import (ArchiveError, CoverageParseError, CoverageReader, ExportError,
                      FileCoverage, LineCoverage, MetricSummary, PickleReader,
                      Segment, export_pickle, write_pickle)


def file_record(name='資料.cpp', count=3):
    return {'filename': name,
            'segments': [[1, 1, count, True, True, False], [3, 1, 0, False, True, False]],
            'summary': {'lines': {'count': 2, 'covered': 2 if count else 0, 'percent': 100 if count else 0}},
            'branches': [[1, 1, 2, 1, 3, 0, 0, 0, 4]],
            'expansions': [{'ignored': ['not stored']}]}


def document(files=None, version='3.0.1'):
    return {'data': [{'files': [file_record()] if files is None else files,
                     'totals': {'lines': {'count': 2, 'covered': 2, 'percent': 100}}}],
            'type': 'llvm.coverage.json.export', 'version': version}


class Chunked:
    def __init__(self, content, size=1):
        self.stream = io.BytesIO(content)
        self.size = size

    def read(self, size=-1):
        return self.stream.read(min(size, self.size) if size >= 0 else self.size)


class ParserTests(unittest.TestCase):
    def reader(self, doc, **kwargs):
        return CoverageReader(io.BytesIO(json.dumps(doc).encode()), **kwargs)

    def test_incremental_utf8_and_typed_records(self):
        raw = json.dumps(document(), ensure_ascii=False).encode()
        reader = CoverageReader(Chunked(raw), buffer_size=7)
        files = list(reader)
        self.assertTrue(reader.completed)
        self.assertEqual(files[0].filename, '資料.cpp')
        self.assertIsInstance(files[0], FileCoverage)
        self.assertIsInstance(files[0].segments[0], Segment)
        self.assertEqual(files[0].summary.lines, MetricSummary(2, 2, 100.0))
        self.assertEqual(list(files[0].iter_lines()), [LineCoverage(1, 3), LineCoverage(2, 3)])
        self.assertEqual(reader.stats.bytes_read, len(raw))
        self.assertEqual(reader.metadata.units[0].totals.lines.count, 2)
        self.assertFalse(hasattr(files[0], 'branches'))

    def test_v2_and_v3_and_multiple_units(self):
        for version in ['2.0.1', '3.0.1']:
            doc = document(version=version)
            doc['data'].append({'files': [file_record('other.rs')]})
            reader = self.reader(doc)
            self.assertEqual([f.data_index for f in reader], [0, 1])
            self.assertEqual(len(reader.metadata.units), 2)

    def test_line_projection_gaps_and_wraps(self):
        raw = [[1, 2, 4, True, True, False], [3, 2, 0, True, True, False],
               [4, 1, 0, False, True, False], [5, 1, 9, True, True, True],
               [6, 1, 0, False, False, False]]
        file = FileCoverage('file.py', tuple(Segment(*s) for s in raw))
        self.assertEqual([(x.line, x.count) for x in file.iter_lines()], [(1, 4), (2, 4), (3, 4), (5, 0), (6, 9)])

    def test_invalid_segments_and_summary_are_rejected(self):
        for segments in [[[0, 1, 0, True, True, False]],
                         [[1, 1, 0, 1, True, False]],
                         [[2, 1, 0, True, True, False], [1, 1, 0, True, True, False]],
                         [[1, 1, 0]]]:
            doc = document()
            doc['data'][0]['files'][0]['segments'] = segments
            with self.assertRaises(CoverageParseError):
                list(self.reader(doc))
        doc = document()
        doc['data'][0]['files'][0]['summary']['lines']['covered'] = 3
        with self.assertRaises(CoverageParseError):
            list(self.reader(doc))

    def test_invalid_shapes_and_versions(self):
        for doc in [{'data': [123]}, {'data': [{'files': {}}]},
                    document(version='4.0.0'), document(files=[{'filename': 'summary-only'}])]:
            with self.assertRaises(CoverageParseError):
                list(self.reader(doc))
        raw = b'{"data":[],"type":"llvm.coverage.json.export","type":"llvm.coverage.json.export","version":"3.0.1"}'
        with self.assertRaises(CoverageParseError):
            list(CoverageReader(io.BytesIO(raw)))

    def test_trailer_validation_is_late_and_reader_is_one_pass(self):
        doc = document()
        doc['type'] = 'wrong'
        reader = self.reader(doc)
        iterator = iter(reader)
        self.assertIsInstance(next(iterator), FileCoverage)
        with self.assertRaises(CoverageParseError):
            list(iterator)
        self.assertFalse(reader.completed)
        with self.assertRaises(RuntimeError):
            iter(reader)


class ArchiveTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.path = Path(self.tmp.name) / 'coverage.pkl'

    def write(self, doc=None, **kwargs):
        return write_pickle(CoverageReader(io.BytesIO(json.dumps(doc or document()).encode())),
                            self.path, source='fixture', test_name='test.ll', **kwargs)

    def test_round_trip_and_verified_footer(self):
        result = self.write(document(files=[file_record(), file_record('second.c')]))
        reader = PickleReader(self.path)
        files = list(reader)
        self.assertTrue(reader.completed)
        self.assertEqual(len(files), 2)
        self.assertEqual(reader.footer, result.footer)
        self.assertEqual(reader.header.test_name, 'test.ll')
        self.assertEqual(list(files[0].iter_lines())[0].count, 3)

    def test_late_invalid_json_does_not_publish(self):
        doc = document()
        doc['type'] = 'wrong'
        with self.assertRaises(CoverageParseError):
            self.write(doc)
        self.assertFalse(self.path.exists())
        self.assertEqual(list(self.path.parent.glob('*.tmp')), [])

    def test_finalizer_failure_preserves_existing_archive(self):
        self.write()
        original = self.path.read_bytes()
        def fail():
            raise ValueError('export failed')
        with self.assertRaises(ValueError):
            self.write(overwrite=True, finalize=fail)
        self.assertEqual(self.path.read_bytes(), original)
        with self.assertRaises(FileExistsError):
            self.write()

    def test_checksum_truncation_and_trailing_bytes(self):
        self.write()
        original = self.path.read_bytes()
        for changed in [original[:-8], original + b'garbage',
                        original.replace(b'fixture', b'fixturf', 1)]:
            self.path.write_bytes(changed)
            with self.assertRaises(ArchiveError):
                list(PickleReader(self.path))

    def test_empty_export_round_trip(self):
        self.write(document(files=[]))
        reader = PickleReader(self.path)
        self.assertEqual(list(reader), [])
        self.assertTrue(reader.completed)


class ProcessTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)

    def run_export(self, script, **kwargs):
        return export_pickle([sys.executable, '-u', '-c', script], self.root / 'output.pkl',
                             stderr_path=self.root / 'export.log', **kwargs)

    def test_subprocess_stdout_round_trip(self):
        result = self.run_export('import sys;print(' + repr(json.dumps(document())) + ');print("diagnostic",file=sys.stderr)')
        self.assertEqual(result.footer.stats.files, 1)
        self.assertIn('diagnostic', (self.root / 'export.log').read_text())
        self.assertTrue(list(PickleReader(result.path)))

    def test_nonzero_exit_and_timeout_leave_no_archive(self):
        with self.assertRaises(ExportError):
            self.run_export('import sys;print(' + repr(json.dumps(document())) + ');sys.exit(2)')
        self.assertFalse((self.root / 'output.pkl').exists())
        with self.assertRaises(ExportError):
            self.run_export('import time;time.sleep(10)', timeout=0.1)
        self.assertFalse((self.root / 'output.pkl').exists())

    def test_incremental_writer_consumes_first_file_before_producer_finishes(self):
        from llvm_cov import CoverageReader
        import subprocess
        a = json.dumps(file_record())
        first = '{"data":[{"files":[' + a + ','
        rest = a + ']}],"type":"llvm.coverage.json.export","version":"3.0.1"}'
        script = 'import sys,time;print(' + repr(first) + ',end="",flush=True);time.sleep(0.3);print(' + repr(rest) + ',flush=True)'
        with subprocess.Popen([sys.executable, '-u', '-c', script], stdout=subprocess.PIPE) as process:
            reader = CoverageReader(process.stdout, buffer_size=32)
            files = iter(reader)
            self.assertIsInstance(next(files), FileCoverage)
            self.assertIsNone(process.poll())
            self.assertEqual(len(list(files)), 1)
            self.assertTrue(reader.completed)
