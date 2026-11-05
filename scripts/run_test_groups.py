"""Run non-browser tests in small, timed groups for local use and Windows CI."""
import subprocess
import sys


def main():
    collect = subprocess.run(
        [sys.executable, '-m', 'pytest', '--collect-only', '-q', '--ignore=tests/test_ui_browser.py', '--ignore=tests/test_v58_browser.py'],
        text=True, capture_output=True, timeout=90,
    )
    if collect.returncode:
        print(collect.stdout, collect.stderr, file=sys.stderr)
        return collect.returncode
    tests = [line for line in collect.stdout.splitlines() if line.startswith('tests/') and '::' in line]
    if not tests:
        print('No tests collected', file=sys.stderr)
        return 1
    failures = []
    for start in range(0, len(tests), 15):
        group = tests[start:start + 15]
        print(f'\nGroup {start // 15 + 1}: {len(group)} tests', flush=True)
        try:
            result = subprocess.run([sys.executable, '-m', 'pytest', '-q', *group], timeout=120)
            if result.returncode:
                failures.append((start + 1, f'exit {result.returncode}'))
        except subprocess.TimeoutExpired:
            failures.append((start + 1, 'timeout'))
    if failures:
        print(f'Failed groups: {failures}', file=sys.stderr)
        return 1
    print(f'All {len(tests)} collected non-browser tests completed successfully in timed groups (see output for skips).')
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
