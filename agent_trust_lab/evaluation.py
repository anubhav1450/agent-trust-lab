"""Ground-truth causal evaluation harness.

The README documents, in prose, that leave-one-out causal analysis is
blind to redundant/OR-type causes: if two context items are each
independently sufficient to cause an outcome, removing either one alone
leaves the other in place, so the outcome doesn't change and neither gets
flagged as relevant. This module measures that instead of just asserting
it — it runs the causal engine against scenarios with a hand-authored,
KNOWN ground truth (the engine is never told the answer), and scores
precision/recall for both the plain leave-one-out method and the
leave-one-out + pairwise method (`sandbox.redundant_pair_report`), so the
before/after of that upgrade is a real number, not a claim.
"""
from typing import Set

from . import scenarios
from .sandbox import causal_report, redundant_pair_report

ROOT_KEY = b"eval-root-key"

# (name, scenario_fn, ground-truth relevant context ids, what this case tests)
BENCHMARK = [
    (
        "single_sufficient_cause",
        scenarios.prompt_injection_attack,
        {"c2"},
        "One malicious item is the sole cause. Leave-one-out is expected "
        "to find it exactly, and pairwise shouldn't need to add anything.",
    ),
    (
        "redundant_sufficient_causes",
        scenarios.redundant_injection_attack,
        {"c2", "c3"},
        "Two independently-sufficient malicious items. Leave-one-out is "
        "expected to miss both; the pairwise pass is expected to catch "
        "them as a redundant pair.",
    ),
]


def score(detected: Set[str], ground_truth: Set[str]) -> dict:
    tp = detected & ground_truth
    fp = detected - ground_truth
    fn = ground_truth - detected
    precision = len(tp) / len(detected) if detected else 0.0
    recall = len(tp) / len(ground_truth) if ground_truth else 0.0
    return {
        "true_positives": sorted(tp),
        "false_positives": sorted(fp),
        "false_negatives": sorted(fn),
        "precision": precision,
        "recall": recall,
    }


def run_benchmark() -> list:
    results = []
    for name, scenario_fn, ground_truth, note in BENCHMARK:
        store, ids = scenario_fn()

        single = causal_report(store, ids, ROOT_KEY)
        detected_single = {cid for cid, info in single["per_context"].items() if info["causally_relevant"]}

        pairwise = redundant_pair_report(store, ids, ROOT_KEY, single=single)
        detected_combined = set(detected_single)
        for p in pairwise["redundant_pairs"]:
            detected_combined.update(p["pair"])

        results.append({
            "scenario": name,
            "note": note,
            "ground_truth": sorted(ground_truth),
            "leave_one_out": {"detected": sorted(detected_single), **score(detected_single, ground_truth)},
            "leave_one_out_plus_pairwise": {
                "detected": sorted(detected_combined),
                "redundant_pairs": [p["pair"] for p in pairwise["redundant_pairs"]],
                **score(detected_combined, ground_truth),
            },
        })
    return results


def print_report():
    print("=" * 72)
    print("CAUSAL ANALYSIS EVALUATION — leave-one-out vs. known ground truth")
    print("=" * 72)
    for r in run_benchmark():
        lo, lop = r["leave_one_out"], r["leave_one_out_plus_pairwise"]
        print(f"\n{r['scenario']}")
        print(f"  {r['note']}")
        print(f"  ground truth: {r['ground_truth']}")
        print(f"  leave-one-out alone:      detected {str(lo['detected']):<14} "
              f"precision {lo['precision']:.2f}  recall {lo['recall']:.2f}")
        print(f"  + pairwise intervention:  detected {str(lop['detected']):<14} "
              f"precision {lop['precision']:.2f}  recall {lop['recall']:.2f}"
              f"  (redundant pairs found: {lop['redundant_pairs']})")


if __name__ == "__main__":
    print_report()
