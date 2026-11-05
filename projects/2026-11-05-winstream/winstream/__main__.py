import os
import sys

from .cli import main

try:
    code = main()
    sys.stdout.flush()
except BrokenPipeError:  # e.g. `winstream show ... | head`
    os.dup2(os.open(os.devnull, os.O_WRONLY), sys.stdout.fileno())
    code = 0
raise SystemExit(code)
