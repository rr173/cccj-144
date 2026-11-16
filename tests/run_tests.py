"""独立测试运行器：不依赖 pytest，逐个运行 test_cases 中的 test_* 函数。"""

from __future__ import annotations

import os
import sys
import tempfile
import traceback

sys.path.insert(0, os.path.dirname(__file__))

import test_cases  # noqa: E402


def main() -> int:
    tests = sorted(name for name in dir(test_cases) if name.startswith("test_"))
    failures = 0
    for name in tests:
        fn = getattr(test_cases, name)
        with tempfile.TemporaryDirectory(prefix="pbtest-") as tmp:
            try:
                fn(tmp)
            except Exception:
                failures += 1
                print(f"FAIL {name}")
                traceback.print_exc()
            else:
                print(f"PASS {name}")
    print(f"\n{len(tests) - failures}/{len(tests)} 通过")
    return 1 if failures else 0


if __name__ == "__main__":
    sys.exit(main())
