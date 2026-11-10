"""Independent MMD diagnostic for already-reviewed, completed artifacts; not grading."""

from __future__ import annotations

import argparse
import hashlib
import json
import math
from pathlib import Path
import random
import sys


def kernel(left, right, gamma):
    return math.exp(-gamma * math.fsum((a - b) ** 2 for a, b in zip(left, right)))


def independent_estimate(reference, current, gamma):
    n, m = len(reference), len(current)
    # Enumerate individual pairs rather than building a kernel matrix.
    within_reference = math.fsum(kernel(reference[i], reference[j], gamma)
                                 for i in range(n) for j in range(n) if i != j)
    within_current = math.fsum(kernel(current[i], current[j], gamma)
                               for i in range(m) for j in range(m) if i != j)
    cross = math.fsum(kernel(a, b, gamma) for a in reference for b in current)
    return within_reference / (n * (n - 1)) + within_current / (m * (m - 1)) - 2 * cross / (n * m)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--module-dir", type=Path, required=True,
                        help="Parent of reviewed drift_monitor package; importing executes its code")
    args = parser.parse_args()
    module_dir = args.module_dir.resolve(strict=True)
    sys.path.insert(0, str(module_dir))
    import numpy as np
    from drift_monitor.statistical_tests import mmd

    seed, gamma = 38191, 0.6
    rng = random.Random(seed)
    reference = [[rng.gauss(0, 1) for _ in range(3)] for _ in range(7)]
    current = [[rng.gauss(0.4, 1.1) for _ in range(3)] for _ in range(11)]
    rows = []
    for name, left, right in (("unequal_samples", reference, current),
                              ("same_samples_negative_estimate", reference, reference)):
        expected = independent_estimate(left, right, gamma)
        actual = mmd(np.array(left), np.array(right), gamma=gamma)
        rows.append({"case": name, "reference_size": len(left), "current_size": len(right),
                     "expected_unbiased_mmd_squared": expected, "actual": actual,
                     "passed": math.isclose(actual, expected, rel_tol=1e-12, abs_tol=1e-12)})
    print(json.dumps({"scope": "Independent supplemental diagnostic; original verifier scores unchanged",
                      "seed": seed, "gamma": gamma, "dimensions": 3,
                      "input_sha256": hashlib.sha256(json.dumps([reference, current]).encode()).hexdigest(),
                      "cases": rows, "passed": all(row["passed"] for row in rows)}))


if __name__ == "__main__":
    main()
