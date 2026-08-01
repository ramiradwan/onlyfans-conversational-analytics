"""Command-line entry point for offline contract integrity checks."""

from __future__ import annotations

import argparse

from .loader import load_trust_set, verify_snapshot_integrity


def main() -> int:
    parser = argparse.ArgumentParser(description="Verify the offline contract snapshot")
    parser.add_argument("--trust-set", help="manifest-relative trust-set.json to load")
    parser.add_argument("--environment", default="production", choices=("production", "development"))
    args = parser.parse_args()
    manifest = verify_snapshot_integrity()
    print(f"verified selected contract snapshot integrity for {len(manifest['files'])} vendored files")
    if args.trust_set:
        trust_set = load_trust_set(args.trust_set, environment=args.environment)
        print(f"loaded trust profile {trust_set['profile']}")
    return 0


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
