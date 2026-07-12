"""RAG evaluation harness + CI regression gate.

Measures RETRIEVAL quality against the real pipeline (pgvector + tsvector + RRF +
rerank) using a golden Q&A set. Retrieval metrics need no LLM, so this runs even
without a valid GOOGLE_API_KEY. Answer-quality (faithfulness) is optional and
only runs with --with-answers when the LLM is reachable.

Exit code is non-zero if hit-rate@k falls below --min-hit-rate, so CI can gate on
retrieval regressions.

Usage:
    python -m eval.run_eval [--k 3] [--min-hit-rate 0.8] [--with-answers]
"""

import argparse
import json
import os
import sys
import warnings
from pathlib import Path

warnings.filterwarnings("ignore")

from dotenv import load_dotenv

load_dotenv()

# Ensure the project root is importable when run as a script.
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import pg_store  # noqa: E402
import rag_backend as rb  # noqa: E402

DATASET = Path(__file__).resolve().parent / "dataset.json"


def _section_of(chunk):
    return (chunk.get("metadata") or {}).get("Section")


def _first_relevant_rank(ranked, expected_sections):
    """1-based rank of the first retrieved chunk in an expected section, else None."""
    expected = set(expected_sections)
    for i, chunk in enumerate(ranked, start=1):
        if _section_of(chunk) in expected:
            return i
    return None


def evaluate(k, with_answers):
    with open(DATASET, "r", encoding="utf-8") as f:
        data = json.load(f)
    cases = data["cases"]

    if not rb.rag_system._ensure_ready():
        print("ERROR: RAG service failed to initialize (check DATABASE_URL / logs).")
        return None
    active = pg_store.get_active_document()
    if active is None:
        print("ERROR: no active document to evaluate against.")
        return None

    depth = max(k, rb.HYBRID_CANDIDATES)
    hits_at_1 = hits_at_k = rr_sum = 0
    answer_checks = answer_passes = 0
    rows = []

    for case in cases:
        ranked = rb.rag_system.rank_candidates(active["id"], case["question"], top_k=depth)
        rank = _first_relevant_rank(ranked, case["expected_sections"])
        hit1 = rank == 1
        hitk = rank is not None and rank <= k
        hits_at_1 += int(hit1)
        hits_at_k += int(hitk)
        rr_sum += (1.0 / rank) if rank else 0.0

        answer_note = ""
        if with_answers:
            result = rb.get_answer(case["question"])
            ans = (result.get("answer") or "").lower()
            wanted = [s.lower() for s in case.get("expected_answer_contains", [])]
            ok = bool(wanted) and any(w in ans for w in wanted)
            answer_checks += 1
            answer_passes += int(ok)
            answer_note = "  ans:" + ("PASS" if ok else "FAIL")

        rows.append((hitk, rank, case["question"], answer_note))

    n = len(cases)
    metrics = {
        "n": n,
        "hit@1": hits_at_1 / n,
        f"hit@{k}": hits_at_k / n,
        "mrr": rr_sum / n,
    }
    if with_answers and answer_checks:
        metrics["answer_pass_rate"] = answer_passes / answer_checks

    print(f"\nRetrieval eval over {n} cases (k={k}, rerank={rb.RERANK_ENABLED}):\n")
    for hitk, rank, question, answer_note in rows:
        mark = "PASS" if hitk else "FAIL"
        rank_s = f"rank {rank}" if rank else "not found"
        print(f"  [{mark}] {rank_s:>10} | {question[:52]:52}{answer_note}")
    print()
    print(f"  hit@1 = {metrics['hit@1']:.2f}   hit@{k} = {metrics[f'hit@{k}']:.2f}   MRR = {metrics['mrr']:.3f}")
    if "answer_pass_rate" in metrics:
        print(f"  answer_pass_rate = {metrics['answer_pass_rate']:.2f}")
    return metrics


def main():
    parser = argparse.ArgumentParser(description="Traffic RAG evaluation + CI gate")
    parser.add_argument("--k", type=int, default=int(os.environ.get("EVAL_K", "3")))
    parser.add_argument(
        "--min-hit-rate", type=float,
        default=float(os.environ.get("EVAL_MIN_HIT_RATE", "0.8")),
    )
    parser.add_argument("--with-answers", action="store_true",
                        help="Also score answer faithfulness (needs a working LLM key).")
    args = parser.parse_args()

    metrics = evaluate(args.k, args.with_answers)
    if metrics is None:
        sys.exit(2)

    hit_key = f"hit@{args.k}"
    passed = metrics[hit_key] >= args.min_hit_rate
    print(f"\nGate: {hit_key} {metrics[hit_key]:.2f} >= {args.min_hit_rate:.2f} -> "
          f"{'PASS' if passed else 'FAIL'}\n")
    sys.exit(0 if passed else 1)


if __name__ == "__main__":
    main()
