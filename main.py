import os
import sys

# `python main.py --channel <name> <command> ...` runs <command> for another channel on this machine (see `channels`).
# It must be set before the package is imported: paths are fixed at import time.
if "--channel" in sys.argv:
    i = sys.argv.index("--channel")
    if i + 1 < len(sys.argv):
        os.environ["SHORTS_CHANNEL"] = sys.argv[i + 1]
        del sys.argv[i:i + 2]

from shorts_pipeline.cli import main  # noqa: E402

if __name__ == "__main__":
    main()
