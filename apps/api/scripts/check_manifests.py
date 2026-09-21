"""Assert the committed reviewer manifests still hash to their approved values.

WHY THIS IS A SEPARATE SCRIPT AND NOT A TEST
============================================
The tests run against a database. This needs nothing but the three files, and it
answers a deployment question rather than a behavioural one: *are the bytes in this
commit the bytes the code was approved against?*

`console.APPROVED_MANIFESTS` pins a sha256 per manifest, and `decisions.preview`
refuses any responsibility decision or promotion whose manifest does not match. So a
commit that regenerates a manifest without updating the pinned constant produces a
console where every decision is blocked by `MANIFEST_MISMATCH`. That is a failure that
should arrive in CI, in the diff that caused it -- not in front of a reviewer.

Exits 1 with the mismatch named, which is the only output worth reading.
"""

from __future__ import annotations

import sys

from app.domains.verification import console


def main() -> int:
    rows = console.manifest_status()
    if not rows:
        print("no manifests found -- expected three under .reports/step-5c5/")
        return 1

    ok = True
    for row in rows:
        # `actual_sha256` is None for a manifest that is not on disk, so it is only
        # ever sliced on the branch where the file was read.
        if row.present and row.matches:
            digest = row.actual_sha256 or ""
            print(f"OK       {row.name:18s} {row.rows} rows  {digest[:12]}...")
            continue
        ok = False
        if not row.present:
            print(f"MISSING  {row.name:18s} expected {row.approved_sha256[:12]}...")
        else:
            print(
                f"MISMATCH {row.name:18s}\n"
                f"           approved : {row.approved_sha256}\n"
                f"           on disk  : {row.actual_sha256}"
            )
    if not ok:
        print(
            "\nThe package under review is not the one the code was approved against. "
            "Either restore the approved bytes or update APPROVED_MANIFESTS in "
            "app/domains/verification/console.py -- deliberately, in a reviewed commit."
        )
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
