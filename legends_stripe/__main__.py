"""CLI entrypoint for python -m legends_stripe."""
import sys
from .cli import main

if __name__ == "__main__":
    sys.exit(main())
