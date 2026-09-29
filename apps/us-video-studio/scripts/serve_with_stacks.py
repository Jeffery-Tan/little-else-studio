"""Start Studio with a periodic all-thread stack dump.

The service froze twice (2026-09-30) with every endpoint timing out while the
process kept its CPU near zero — the shape of a deadlock, not of slow work. This
launcher arms ``faulthandler`` so that whenever the freeze happens again the
stacks of *every* thread land in a file, which names the lock and the holder
instead of leaving it to guesswork.

Run: python scripts/serve_with_stacks.py <port> <dump-file>
"""
import faulthandler
import os
import sys
from pathlib import Path

port = int(sys.argv[1]) if len(sys.argv) > 1 else 4760
dump = Path(sys.argv[2]) if len(sys.argv) > 2 else Path(os.environ['TEMP']) / 'studio-stacks.txt'
dump.parent.mkdir(parents=True, exist_ok=True)
handle = dump.open('w', encoding='utf-8')
faulthandler.enable(file=handle, all_threads=True)
# Re-arm every 120s: each dump replaces the file content, so the last one before
# the freeze is what a reader finds.
faulthandler.dump_traceback_later(120, repeat=True, file=handle, exit=False)
print(f'stack dump armed -> {dump}', flush=True)

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import uvicorn  # noqa: E402
from studio.config import load_settings  # noqa: E402
from studio.server import build_app  # noqa: E402

uvicorn.run(build_app(load_settings(None)), host='127.0.0.1', port=port, log_level='info')
