"""After switching to manifest mode, inspect before pinning a version (issue #95). Run: python test_workload_sets.py"""
import os

import yaml

EXPECTED = [
    "dotnet workload config --update-mode manifests",
    "dotnet workload update",
    "dotnet workload --info",
    "dotnet workload list",
]

path = os.path.join(os.path.dirname(__file__), "test_definitions", "10_workload_sets.yaml")
with open(path, encoding="utf-8") as f:
    tests = {t["id"]: t for t in yaml.safe_load(f)["tests"]}

for test_id in ("workload-sets-update-specific-exe", "workload-sets-update-different-band-exe"):
    cmds = [s.get("command") for s in tests[test_id]["steps"]]
    i = cmds.index(EXPECTED[0])
    assert cmds[i:i + len(EXPECTED)] == EXPECTED, f"{test_id}: {cmds[i:i + len(EXPECTED)]}"

print("ok")
