from pathlib import Path

path = Path("tests/test_contract_snapshot.py")
source = path.read_text(encoding="utf-8")
replacements = {
    'assert pin["aggregate_bundle_sha256"] == "3c4b0a774e2fe6f3bf878c6e8434bc0e2c712630e7c89951933788d70cf1ed6c"': 'assert pin["aggregate_bundle_sha256"] == "5ced81122666923d82eb212ffd0d4643e56952fe6027a1308c12cc06c50315a1"',
    'assert pin["contract_manifest_sha256"] == "6ba604cfa5b85cd75354fe6dd0ab55c89a0adfa005d6d161c659485cbd1d0878"': 'assert pin["contract_manifest_sha256"] == "827eda31ec62e156fcc16cf7b7dbf09ddddb1019ed920e322be28d21c5bf30f4"',
}
for old, new in replacements.items():
    if old not in source and new not in source:
        raise SystemExit(f"missing expected assertion: {old}")
    source = source.replace(old, new, 1)
path.write_text(source, encoding="utf-8")
