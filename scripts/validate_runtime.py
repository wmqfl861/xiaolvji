#!/usr/bin/env python3
"""Run meaningful runtime, policy and static-contract checks without dependencies."""
from __future__ import annotations
import argparse
import hashlib
import json
import os
from pathlib import Path
import platform
import subprocess
import sys
import time
import unittest

ROOT = Path(__file__).resolve().parents[1]
sys.path[:0] = [str(ROOT / 'src'), str(ROOT), str(ROOT / 'reference')]
from xiaolvji import __version__


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--report', type=Path, required=True)
    args = parser.parse_args()
    loader = unittest.TestLoader()
    outcomes = []
    started = time.monotonic()
    for label, directory, pattern in [('runtime', 'tests', 'test_*.py'), ('policy_reference', 'reference', 'test_inject_agents.py')]:
        suite = loader.discover(str(ROOT / directory), pattern=pattern, top_level_dir=str(ROOT) if directory == 'tests' else str(ROOT / 'reference'))
        before = time.monotonic()
        result = unittest.TextTestRunner(verbosity=2).run(suite)
        outcomes.append({'group': label, 'tests': result.testsRun, 'failures': len(result.failures),
                         'errors': len(result.errors), 'skipped': len(result.skipped),
                         'passed': result.wasSuccessful(), 'seconds': round(time.monotonic()-before, 3),
                         'failed_tests': [str(test) for test, _ in result.failures + result.errors]})
    static = subprocess.run([sys.executable, str(ROOT/'reference/check_design.py')], cwd=ROOT,
                            stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True, timeout=30)
    print(static.stdout)
    inputs = sorted(p for base in ('src', 'tests', 'reference', 'schemas', 'spec', 'scripts') for p in (ROOT/base).rglob('*')
                    if p.is_file() and '__pycache__' not in p.parts and p.suffix in ('.py', '.json', '.md'))
    digest = hashlib.sha256()
    for path in inputs:
        digest.update(path.relative_to(ROOT).as_posix().encode() + b'\0')
        digest.update(hashlib.sha256(path.read_bytes()).digest())
    result = {'schema_version': 1, 'runtime_version': __version__,
              'status': 'passed' if all(r['passed'] for r in outcomes) and static.returncode == 0 else 'failed',
              'environment': {'platform': platform.platform(), 'python': platform.python_version(),
                              'git': subprocess.run(['git','--version'],capture_output=True,text=True,check=True).stdout.strip()},
              'groups': outcomes, 'total_unit_tests': sum(r['tests'] for r in outcomes),
              'design_static_check': {'passed': static.returncode == 0, 'output': static.stdout.strip()},
              'source_inputs_sha256': digest.hexdigest(), 'source_input_count': len(inputs),
              'elapsed_seconds': round(time.monotonic()-started, 3),
              'optional_integration_inputs': {
                  'codex_cli_loading': bool(os.environ.get('XIAOLVJI_CODEX_CLI_SMOKE')),
                  'codex_native_protocol_fixture': bool(os.environ.get('XIAOLVJI_CODEX_NATIVE_SMOKE')),
                  'zcode_native_source_mapping': bool(os.environ.get('XIAOLVJI_ZCODE_SOURCE'))},
              'not_run': ['Authenticated external-model acceptance for Codex', 'Complete ZCode CLI/model integration',
                          'Real external provider service acceptance (synthetic IPC/HTTP transport is tested)', 'Windows execution', 'macOS execution',
                          'Production load and long-duration recovery', 'Independent formal release review']}
    args.report.parent.mkdir(parents=True,exist_ok=True)
    args.report.write_text(json.dumps(result,ensure_ascii=False,indent=2)+'\n',encoding='utf-8')
    print(json.dumps({'status': result['status'], 'tests': result['total_unit_tests'], 'report': str(args.report)},ensure_ascii=False))
    return 0 if result['status'] == 'passed' else 1

if __name__ == '__main__':
    raise SystemExit(main())
