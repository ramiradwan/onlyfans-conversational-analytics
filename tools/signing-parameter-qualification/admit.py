"""Admit one new isolated current-clock QA installation outside the consumer."""
import json
import argparse
from pathlib import Path
import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
from fixture_admission import seed_admission

if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("directory", type=Path)
    parser.add_argument("--grant-basis-expires-in-seconds", type=int)
    parser.add_argument("--companion-authority-expires-in-seconds", type=int)
    args = parser.parse_args()
    receipt = seed_admission(args.directory, grant_basis_expires_in_seconds=args.grant_basis_expires_in_seconds,
                             companion_authority_expires_in_seconds=args.companion_authority_expires_in_seconds).receipt
    print(json.dumps(receipt, sort_keys=True, separators=(",", ":")))
