#!/usr/bin/env python3
"""refresh_pricing: fetch the latest maintained pricing table for token-rent.

Downloads data/pricing.json from the project's GitHub repository (one public file, plain GET,
nothing sent), checks it, and saves it to a cache folder if it is newer than what you have.
Every script then uses it automatically. Exit 0 = up to date, 1 = could not refresh.

  python3 refresh_pricing.py            # use the URL inside the bundled pricing.json
  python3 refresh_pricing.py --url URL  # a fork or a private table
"""
from __future__ import annotations

import argparse
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import rentlib as rl  # noqa: E402


def main(argv=None):
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--url", default=None)
    p.add_argument("--timeout", type=float, default=10.0)
    a = p.parse_args(argv)
    ok, msg = rl.refresh_pricing(a.url, a.timeout)
    print(f"[token-rent] {msg}")
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
