"""Start CreditOptimiser from anywhere: `python /path/to/run_creditopt.py <command>`.

Claude Code's hook and status line call this file directly, so they work no
matter which folder Claude Code is running in, on macOS, Linux and Windows.
"""

import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from creditopt.__main__ import main  # noqa: E402

if __name__ == "__main__":
    sys.exit(main())
