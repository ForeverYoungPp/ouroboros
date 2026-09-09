#!/usr/bin/env python3
"""Bulk repository inventory tool (read-only, pure stdlib).

One invocation walks a file tree and reports, per labeled pattern, how
many files contain at least one matching line and how many lines match
(grep -c line semantics: a line with one or more matches counts once).
Built for repository inventories such as "how many of the 818 skills
contain a Verification section" in a single call, replacing one
per-file search_code call per inventory question.

Read-only by construction: pure Python os.walk, no subprocess, no shell,
and no writes to any path. Every skip (binary, oversize, symlink,
unreadable, glob filter, walk error) is counted and disclosed in the
JSON result — never silent.

Exit codes: 0 success (ok:true) / 1 runtime error (ok:false) /
2 usage error (ok:false). Scan results and error envelopes print as
JSON on stdout; argparse usage errors and --help print on stderr
(exempt from the JSON-only stdout invariant, per plan review finding
slot_2:f4).

Devtools isolation: runtime modules must not import this file
(devtools/README.md); it is invoked directly, e.g. via run_command with
the repository as cwd.
"""

import argparse
import fnmatch
import json
import os
import re
import sys
import time

LABEL_RE = re.compile(r'^[A-Za-z0-9_.-]{1,64}$')
BINARY_PROBE_BYTES = 8192
ERROR_SAMPLE_LIMIT = 10
DEFAULT_MAX_FILE_BYTES = 1048576
DEFAULT_MAX_ENTRIES = 1000


class UsageError(ValueError):
    """Invalid CLI input (bad pattern syntax or limits) — exit code 2."""


def build_parser():
    parser = argparse.ArgumentParser(
        prog='bulk_inventory',
        description='Read-only bulk repository inventory: per-pattern file/line '
                    'match coverage over a file tree in one call (JSON output).')
    parser.add_argument('--root', default='.', metavar='DIR',
                        help='directory to scan (default: invocation cwd)')
    parser.add_argument('--match', action='append', required=True,
                        metavar='LABEL:PATTERN',
                        help='labeled pattern; repeat to answer several inventory '
                             'questions in one walk')
    parser.add_argument('--regex', action='store_true',
                        help='treat every --match pattern as a regular expression '
                             '(default: literal substring)')
    parser.add_argument('--ignore-case', action='store_true',
                        help='case-insensitive pattern matching')
    parser.add_argument('--include', action='append', default=[], metavar='GLOB',
                        help='scan only files whose repo-relative path matches this '
                             'fnmatch glob (repeatable, case-sensitive)')
    parser.add_argument('--exclude', action='append', default=[], metavar='GLOB',
                        help='skip files whose repo-relative path matches this '
                             'fnmatch glob (repeatable, case-sensitive)')
    parser.add_argument('--max-file-bytes', type=int, default=DEFAULT_MAX_FILE_BYTES,
                        metavar='N',
                        help='skip files larger than N bytes (default: %(default)s)')
    parser.add_argument('--max-entries', type=int, default=DEFAULT_MAX_ENTRIES,
                        metavar='N',
                        help='bound the per-file detail list; overflow is disclosed '
                             'via truncated/files_omitted (default: %(default)s)')
    return parser


def parse_match_specs(specs):
    """Split LABEL:PATTERN specs; reject empty/duplicate/invalid labels."""
    parsed = []
    seen = set()
    for spec in specs:
        label, sep, pattern = spec.partition(':')
        if not sep or not label or not pattern:
            raise UsageError(f"--match expects LABEL:PATTERN, got: {spec!r}")
        if not LABEL_RE.match(label):
            raise UsageError(
                f'pattern label must match {LABEL_RE.pattern}: {label!r}')
        if label in seen:
            raise UsageError(f'duplicate pattern label: {label!r}')
        seen.add(label)
        parsed.append((label, pattern))
    return parsed


def compile_patterns(specs, use_regex, ignore_case):
    """Compile literal or regex patterns; invalid regex is a usage error."""
    flags = re.IGNORECASE if ignore_case else 0
    compiled = []
    for label, pattern in specs:
        try:
            rx = re.compile(pattern if use_regex else re.escape(pattern), flags)
        except re.error as exc:
            raise UsageError(f'invalid regex for label {label!r}: {exc}')
        compiled.append((label, pattern, rx))
    return compiled


def validate_limits(max_file_bytes, max_entries):
    if max_file_bytes < 1:
        raise UsageError('--max-file-bytes must be >= 1')
    if max_entries < 0:
        raise UsageError('--max-entries must be >= 0')


def iter_tree(root, on_walk_error):
    """Deterministic read-only walk; `.git` directories are always pruned."""
    for dirpath, dirnames, filenames in os.walk(
            root, followlinks=False, onerror=on_walk_error):
        dirnames[:] = sorted(name for name in dirnames if name != '.git')
        for name in sorted(filenames):
            yield os.path.join(dirpath, name)


def classify_file(path, max_file_bytes):
    """Return (status, size, error_message).

    status is one of: ok | binary | oversize | symlink | unreadable.
    """
    if os.path.islink(path):
        return 'symlink', 0, None
    try:
        size = os.path.getsize(path)
    except OSError as exc:
        return 'unreadable', 0, str(exc)
    if size > max_file_bytes:
        return 'oversize', size, None
    if not os.path.isfile(path):
        return 'unreadable', 0, 'not a regular file'
    try:
        with open(path, 'rb') as handle:
            head = handle.read(BINARY_PROBE_BYTES)
    except OSError as exc:
        return 'unreadable', 0, str(exc)
    if b'\0' in head:
        return 'binary', size, None
    return 'ok', size, None


def count_file_matches(path, compiled):
    """Count matching lines per pattern (grep -c semantics).

    Returns ({label: matching_line_count}, None) on success or
    (None, error_message) if the file could not be read.
    """
    try:
        with open(path, 'rb') as handle:
            data = handle.read()
    except OSError as exc:
        return None, str(exc)
    text = data.decode('utf-8', errors='replace')
    hits = {}
    for label, _pattern, rx in compiled:
        lines = 0
        for line in text.splitlines():
            if rx.search(line):
                lines += 1
        hits[label] = lines
    return hits, None


def matches_any(rel_path, globs):
    return any(fnmatch.fnmatchcase(rel_path, glob) for glob in globs)


def append_sample(skipped, list_key, omitted_key, sample):
    samples = skipped[list_key]
    if len(samples) < ERROR_SAMPLE_LIMIT:
        samples.append(sample)
    else:
        skipped[omitted_key] += 1


def scan_tree(root, compiled, args):
    """Walk the tree once and aggregate per-pattern / per-file statistics."""
    started = time.monotonic()
    scanned = 0
    scanned_bytes = 0
    per_pattern = {
        label: {'files_hit': 0, 'lines_hit': 0} for label, _p, _rx in compiled
    }
    files_with_any_match = 0
    files_zero_hits = 0
    skipped = {
        'binary': 0,
        'oversize': 0,
        'symlink': 0,
        'unreadable': 0,
        'exclude_glob': 0,
        'include_filter': 0,
        'walk_errors': 0,
        'error_samples': [],
        'error_samples_omitted': 0,
        'walk_error_samples': [],
        'walk_error_samples_omitted': 0,
    }
    hit_files = []
    walk_errors = []

    def note_walk_error(exc):
        walk_errors.append(exc)

    for path in iter_tree(root, note_walk_error):
        rel_path = os.path.relpath(path, root).replace(os.sep, '/')
        if args.exclude and matches_any(rel_path, args.exclude):
            skipped['exclude_glob'] += 1
            continue
        if args.include and not matches_any(rel_path, args.include):
            skipped['include_filter'] += 1
            continue
        status, size, error = classify_file(path, args.max_file_bytes)
        if status != 'ok':
            skipped[status] += 1
            if status == 'unreadable':
                append_sample(skipped, 'error_samples', 'error_samples_omitted',
                              {'path': rel_path, 'error': error or 'unreadable'})
            continue
        hits, error = count_file_matches(path, compiled)
        if hits is None:
            skipped['unreadable'] += 1
            append_sample(skipped, 'error_samples', 'error_samples_omitted',
                          {'path': rel_path, 'error': error or 'unreadable'})
            continue
        scanned += 1
        scanned_bytes += size
        total = sum(hits.values())
        if total == 0:
            files_zero_hits += 1
            continue
        files_with_any_match += 1
        for label, lines in hits.items():
            if lines:
                per_pattern[label]['files_hit'] += 1
                per_pattern[label]['lines_hit'] += lines
        hit_files.append({
            'path': rel_path,
            'hits': {name: count for name, count in hits.items() if count},
            'total': total,
        })

    for exc in walk_errors:
        skipped['walk_errors'] += 1
        append_sample(skipped, 'walk_error_samples', 'walk_error_samples_omitted',
                      {'path': str(getattr(exc, 'filename', '') or ''),
                       'error': str(exc)})

    hit_files.sort(key=lambda entry: entry['path'])
    total_hit_files = len(hit_files)
    bounded_files = hit_files[:args.max_entries]
    return {
        'ok': True,
        'tool': 'bulk_inventory',
        'root': os.path.abspath(root),
        'scanned_files': scanned,
        'scanned_bytes': scanned_bytes,
        'elapsed_sec': round(time.monotonic() - started, 3),
        'patterns': [
            {
                'label': label,
                'pattern': pattern,
                'regex': args.regex,
                'case_sensitive': not args.ignore_case,
                'files_hit': per_pattern[label]['files_hit'],
                'lines_hit': per_pattern[label]['lines_hit'],
            }
            for label, pattern, _rx in compiled
        ],
        'aggregate': {
            'files_with_any_match': files_with_any_match,
            'files_zero_hits': files_zero_hits,
            'coverage_pct': (
                round(files_with_any_match * 100.0 / scanned, 2) if scanned
                else 0.0),
        },
        'skipped': skipped,
        'files': bounded_files,
        'truncated': total_hit_files > len(bounded_files),
        'files_omitted': max(0, total_hit_files - len(bounded_files)),
    }


def run(args):
    validate_limits(args.max_file_bytes, args.max_entries)
    compiled = compile_patterns(
        parse_match_specs(args.match), args.regex, args.ignore_case)
    root = args.root
    if not os.path.isdir(root):
        return ({'ok': False, 'tool': 'bulk_inventory',
                 'error': f'root is not a directory: {root}'}, 1)
    return scan_tree(root, compiled, args), 0


def main(argv=None):
    try:
        sys.stdout.reconfigure(encoding='utf-8', errors='replace')
    except (AttributeError, ValueError):
        pass
    args = build_parser().parse_args(argv)
    try:
        result, exit_code = run(args)
    except UsageError as exc:
        result = {'ok': False, 'tool': 'bulk_inventory', 'error': str(exc)}
        exit_code = 2
    sys.stdout.write(json.dumps(result, indent=2, ensure_ascii=False) + '\n')
    return exit_code


if __name__ == '__main__':
    sys.exit(main(sys.argv[1:]))
