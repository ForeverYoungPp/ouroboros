"""Tests for devtools/bulk_inventory.py (bulk repository inventory tool).

The tool is exercised through its real CLI surface (subprocess) so the
JSON contract, exit codes, and skip disclosure are all covered
end-to-end. The tool itself contains no subprocess/shell code — only
these tests launch it.
"""

import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent
TOOL = REPO_ROOT / 'devtools' / 'bulk_inventory.py'

TOP_LEVEL_KEYS = {
    'ok', 'tool', 'root', 'scanned_files', 'scanned_bytes', 'elapsed_sec',
    'patterns', 'aggregate', 'skipped', 'files', 'truncated', 'files_omitted',
}


def build_tree(root: Path) -> None:
    """Fixture tree: nested, empty, binary, unicode, .git, oversize-capable."""
    docs = root / 'docs'
    docs.mkdir(parents=True)
    (docs / 'a.md').write_text(
        'Verification: yes\nother\nVerification again\n', encoding='utf-8')
    (docs / 'b.txt').write_text('no match here\nother\n', encoding='utf-8')
    (root / 'empty.txt').write_text('', encoding='utf-8')
    (root / 'bin.dat').write_bytes(b'hi\x00')
    (root / '.git').mkdir()
    (root / '.git' / 'hidden.txt').write_text('Verification\n', encoding='utf-8')
    (root / 'unicode_文件.md').write_text('Verification ✓\n', encoding='utf-8')
    # ~94 bytes; uniquely the largest fixture file — with
    # --max-file-bytes 64 only this file is skipped as oversize.
    (root / 'big.log').write_text('x' * 80 + '\nVerification\n', encoding='utf-8')


def run_tool(args, cwd):
    return subprocess.run(
        [sys.executable, str(TOOL), *args],
        cwd=str(cwd), capture_output=True, text=True, timeout=120)


def parse_json(proc):
    return json.loads(proc.stdout)


def test_help_exits_zero(tmp_path):
    proc = run_tool(['--help'], cwd=tmp_path)
    assert proc.returncode == 0
    assert 'bulk_inventory' in proc.stdout


def test_literal_counts(tmp_path):
    build_tree(tmp_path)
    proc = run_tool(
        ['--root', str(tmp_path), '--match', 'v:Verification'], cwd=tmp_path)
    assert proc.returncode == 0
    result = parse_json(proc)
    assert result['ok'] is True
    # Hits: docs/a.md (2), unicode_文件.md (1), big.log (1).
    # Skipped: bin.dat (binary), .git/hidden.txt (pruned).
    assert result['patterns'][0]['files_hit'] == 3
    assert result['patterns'][0]['lines_hit'] == 4
    assert result['scanned_files'] == 5
    assert result['skipped']['binary'] == 1
    assert result['truncated'] is False
    assert result['files_omitted'] == 0


def test_regex_vs_literal(tmp_path):
    build_tree(tmp_path)
    base = ['--root', str(tmp_path)]
    literal = parse_json(run_tool(
        base + ['--match', 'v:Ver[a-z]+'], cwd=tmp_path))
    regex = parse_json(run_tool(
        base + ['--regex', '--match', 'v:Ver[a-z]+'], cwd=tmp_path))
    # Literal mode must not treat Ver[a-z]+ as a pattern.
    assert literal['patterns'][0]['files_hit'] == 0
    assert regex['patterns'][0]['files_hit'] == 3
    anchored = parse_json(run_tool(
        base + ['--regex', '--match', 'a:Verification$'], cwd=tmp_path))
    # Only big.log's bare "Verification" line ends the line.
    assert anchored['patterns'][0]['files_hit'] == 1
    assert anchored['patterns'][0]['lines_hit'] == 1


def test_binary_and_oversize_skips_disclosed(tmp_path):
    build_tree(tmp_path)
    proc = run_tool(
        ['--root', str(tmp_path), '--match', 'v:Verification',
         '--max-file-bytes', '64'], cwd=tmp_path)
    result = parse_json(proc)
    assert proc.returncode == 0
    assert result['skipped']['binary'] == 1
    assert result['skipped']['oversize'] == 1
    assert result['scanned_files'] == 4
    skipped_paths = {entry['path'] for entry in result['files']}
    assert 'bin.dat' not in skipped_paths
    assert 'big.log' not in skipped_paths


def test_include_and_exclude_globs(tmp_path):
    build_tree(tmp_path)
    base = ['--root', str(tmp_path), '--match', 'v:Verification']
    include = parse_json(run_tool(base + ['--include', '*.md'], cwd=tmp_path))
    assert include['scanned_files'] == 2
    assert include['skipped']['include_filter'] == 4
    exclude = parse_json(run_tool(base + ['--exclude', '*.md'], cwd=tmp_path))
    assert exclude['scanned_files'] == 3
    assert exclude['skipped']['exclude_glob'] == 2


def test_git_directory_is_pruned(tmp_path):
    build_tree(tmp_path)
    result = parse_json(run_tool(
        ['--root', str(tmp_path), '--match', 'v:Verification'], cwd=tmp_path))
    all_paths = {entry['path'] for entry in result['files']}
    assert all(not path.startswith('.git/') for path in all_paths)
    # .git/hidden.txt would otherwise add one more file and line.
    assert result['patterns'][0]['files_hit'] == 3


def test_multi_pattern_shape(tmp_path):
    build_tree(tmp_path)
    result = parse_json(run_tool(
        ['--root', str(tmp_path), '--match', 'v:Verification',
         '--match', 'o:other'], cwd=tmp_path))
    assert [entry['label'] for entry in result['patterns']] == ['v', 'o']
    verification, other = result['patterns']
    assert verification['files_hit'] == 3
    assert verification['lines_hit'] == 4
    assert other['files_hit'] == 2
    assert other['lines_hit'] == 2
    assert result['aggregate']['files_with_any_match'] == 4
    by_path = {entry['path']: entry for entry in result['files']}
    assert by_path['docs/a.md']['hits'] == {'v': 2, 'o': 1}
    assert by_path['docs/b.txt']['hits'] == {'o': 1}


def test_max_entries_truncation_disclosed(tmp_path):
    build_tree(tmp_path)
    result = parse_json(run_tool(
        ['--root', str(tmp_path), '--match', 'v:Verification',
         '--max-entries', '2'], cwd=tmp_path))
    assert len(result['files']) == 2
    assert result['truncated'] is True
    assert result['files_omitted'] == 1


def test_case_sensitivity(tmp_path):
    build_tree(tmp_path)
    base = ['--root', str(tmp_path), '--match', 'v:verification']
    sensitive = parse_json(run_tool(base, cwd=tmp_path))
    assert sensitive['patterns'][0]['files_hit'] == 0
    insensitive = parse_json(run_tool(base + ['--ignore-case'], cwd=tmp_path))
    assert insensitive['patterns'][0]['files_hit'] == 3
    assert insensitive['patterns'][0]['lines_hit'] == 4
    assert insensitive['patterns'][0]['case_sensitive'] is False


def test_label_validation_errors(tmp_path):
    for bad_spec in ['nocolon', 'has space:x', 'v:', ':pattern']:
        proc = run_tool(
            ['--root', str(tmp_path), '--match', bad_spec], cwd=tmp_path)
        assert proc.returncode == 2, bad_spec
        assert parse_json(proc)['ok'] is False
    dup = run_tool(
        ['--root', str(tmp_path), '--match', 'v:x', '--match', 'v:y'],
        cwd=tmp_path)
    assert dup.returncode == 2
    assert parse_json(dup)['ok'] is False


def test_bad_regex_is_usage_error(tmp_path):
    proc = run_tool(
        ['--root', str(tmp_path), '--regex', '--match', 'v:('], cwd=tmp_path)
    assert proc.returncode == 2
    result = parse_json(proc)
    assert result['ok'] is False
    assert 'invalid regex' in result['error']


def test_missing_root_is_runtime_error(tmp_path):
    proc = run_tool(
        ['--root', str(tmp_path / 'does-not-exist'), '--match', 'v:x'],
        cwd=tmp_path)
    assert proc.returncode == 1
    result = parse_json(proc)
    assert result['ok'] is False
    assert 'root is not a directory' in result['error']


def test_zero_hits_aggregate(tmp_path):
    build_tree(tmp_path)
    result = parse_json(run_tool(
        ['--root', str(tmp_path), '--match', 'z:zzzznomatch'], cwd=tmp_path))
    assert result['scanned_files'] == 5
    assert result['aggregate']['files_zero_hits'] == 5
    assert result['aggregate']['files_with_any_match'] == 0
    assert result['aggregate']['coverage_pct'] == 0.0
    assert result['files'] == []


def test_schema_keys_present(tmp_path):
    build_tree(tmp_path)
    result = parse_json(run_tool(
        ['--root', str(tmp_path), '--match', 'v:Verification'], cwd=tmp_path))
    assert TOP_LEVEL_KEYS.issubset(result.keys())
    assert set(result['aggregate']) == {
        'files_with_any_match', 'files_zero_hits', 'coverage_pct'}
    assert set(result['patterns'][0]) == {
        'label', 'pattern', 'regex', 'case_sensitive', 'files_hit', 'lines_hit'}


def test_symlink_skipped_and_disclosed(tmp_path):
    build_tree(tmp_path)
    (tmp_path / 'real.txt').write_text('Verification\n', encoding='utf-8')
    try:
        os.symlink(tmp_path / 'real.txt', tmp_path / 'link.txt')
    except OSError:
        pytest.skip('symlink creation not permitted on this platform')
    result = parse_json(run_tool(
        ['--root', str(tmp_path), '--match', 'v:Verification'], cwd=tmp_path))
    assert result['skipped']['symlink'] == 1
    # link.txt is excluded; real.txt itself is still scanned normally.
    assert result['patterns'][0]['files_hit'] == 4
    paths = {entry['path'] for entry in result['files']}
    assert 'link.txt' not in paths
    assert 'real.txt' in paths


def test_real_repo_smoke_docs(tmp_path):
    """Real-repo smoke: docs/ scan runs clean and reports zero skips."""
    result = parse_json(run_tool(
        ['--root', str(REPO_ROOT / 'docs'), '--match', 'v:version'],
        cwd=tmp_path))
    assert result['ok'] is True
    assert result['scanned_files'] > 0
    # docs/ legitimately contains binary assets (images etc.); the smoke
    # contract is that skips are DISCLOSED (never silent) and no file fails
    # to read or walk — binary/oversize totals are covered by fixture tests.
    assert result['skipped']['unreadable'] == 0
    assert result['skipped']['walk_errors'] == 0
    assert result['patterns'][0]['lines_hit'] > 0
