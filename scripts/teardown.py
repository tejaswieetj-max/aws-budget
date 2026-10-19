"""Tag-scoped teardown planning. Intentionally no delete/terminate implementation yet."""

import argparse

p = argparse.ArgumentParser()
p.add_argument("--dry-run", action="store_true", default=True)
p.add_argument("--apply", action="store_true")
p.add_argument("--confirm", action="store_true")
a = p.parse_args()
print(
    "Plan only: discover and verify project=spendctl, env=dev, managed-by=spend-controller resources; inspect reservation IDs; preserve billing export bucket/data; verify EBS after termination."
)
if a.apply:
    if not a.confirm:
        raise SystemExit("Destructive teardown requires --confirm after explicit user approval.")
    raise SystemExit(
        "Deletion adapter is intentionally not implemented; no cloud resource was changed."
    )
print("No AWS API called. Teardown implementation is incomplete.")
