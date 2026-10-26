import argparse
from dataclasses import asdict
import json
from pathlib import Path
import sys

from . import CoverageReader, PickleReader, write_pickle, export_pickle


def main():
    parser = argparse.ArgumentParser(prog='llvm-cov-py')
    commands = parser.add_subparsers(dest='command', required=True)
    parse = commands.add_parser('parse', help='incrementally parse LLVM JSON or stdin into typed pickle records')
    parse.add_argument('input', help='JSON path, or - for stdin')
    export = commands.add_parser('export', help='run llvm-cov export and pickle its stdout directly')
    export.add_argument('mapping', help='executable, object or supported archive')
    export.add_argument('--profile', required=True)
    export.add_argument('--tool', default='llvm-cov')
    export.add_argument('--stderr', help='export log; defaults to OUTPUT.export.log')
    export.add_argument('--timeout', type=float, default=120)
    for sub in (parse, export):
        sub.add_argument('-o', '--output', required=True)
        sub.add_argument('--buffer-size', type=int, default=65536)
        sub.add_argument('--test-name')
        sub.add_argument('--overwrite', action='store_true')
    inspect = commands.add_parser('inspect', help='verify a trusted pickle archive and print its metadata')
    inspect.add_argument('input')
    inspect.add_argument('--files', action='store_true')
    args = parser.parse_args()
    try:
        if args.command == 'parse':
            if args.input != '-' and Path(args.input).resolve() == Path(args.output).resolve():
                raise ValueError('input and pickle output must differ')
            stream = sys.stdin.buffer if args.input == '-' else open(args.input, 'rb')
            try:
                result = write_pickle(CoverageReader(stream, buffer_size=args.buffer_size), args.output,
                                      source=args.input, test_name=args.test_name, overwrite=args.overwrite)
            finally:
                if args.input != '-':
                    stream.close()
            print(json.dumps(asdict(result), indent=2))
        elif args.command == 'export':
            if Path(args.mapping).resolve() == Path(args.output).resolve() or Path(args.profile).resolve() == Path(args.output).resolve():
                raise ValueError('pickle output must differ from mapping and profile')
            log = args.stderr or args.output + '.export.log'
            if Path(log).resolve() in (Path(args.mapping).resolve(), Path(args.profile).resolve()):
                raise ValueError('stderr log must differ from mapping and profile')
            command = [args.tool, 'export', args.mapping, '-instr-profile=' + args.profile,
                       '-skip-expansions', '-skip-functions']
            result = export_pickle(command, args.output, stderr_path=log,
                                   timeout=args.timeout, buffer_size=args.buffer_size,
                                   test_name=args.test_name, overwrite=args.overwrite)
            print(json.dumps(asdict(result), indent=2))
        else:
            archive = PickleReader(args.input)
            count = segments = 0
            for file in archive:
                count += 1
                segments += len(file.segments)
                if args.files:
                    print(json.dumps({'filename': file.filename, 'data_index': file.data_index,
                                      'segments': len(file.segments)}, ensure_ascii=False))
            print(json.dumps({'verified': archive.completed, 'files': count, 'segments': segments,
                              'header': asdict(archive.header), 'footer': asdict(archive.footer)}, indent=2))
    except (OSError, ValueError, RuntimeError) as exc:
        parser.exit(2, f'llvm-cov-py: {exc}\n')
