#!/usr/bin/env python3
"""Compare a captured baseline against the run's CURRENT scorecards.

Written because a full-product re-extraction overwrites the scorecards it is judged against:
without a snapshot there is nothing to answer "did it improve" with, and the fallback chain's
own "adopt only if better" rule operates per document, not per product.

    python scripts/compare_baseline.py baselines/mram-baseline-<stamp>.json --profile dev1
"""
from __future__ import annotations

import argparse
import json
import sys
from collections import Counter
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("baseline",
                    help="s3://… URI, or a bare filename resolved under "
                         "<run>/_baselines/. Baselines live in S3, never in the repo: a "
                         "66KB snapshot of 106 scorecards is run state, not source, and it "
                         "belongs beside the run it describes.")
    ap.add_argument("--profile", default=None)
    ap.add_argument("--bucket", default="aosphere-tenant-dev1-core-index")
    ap.add_argument("--region", default="eu-west-1")
    ap.add_argument("--prefix", default="corpus/2026-08-21-02",
                    help="run prefix holding _baselines/")
    a = ap.parse_args()
    import os
    if a.profile:
        os.environ.setdefault("AWS_PROFILE", a.profile)
    os.environ.setdefault("ACI_EXTRACTION_BUCKET", a.bucket)
    os.environ.setdefault("ACI_AWS_REGION", a.region)
    from aosphere_core_index.service import doc_gallery as G

    # Read from S3 by default; a local path still works for an ad-hoc file.
    if a.baseline.startswith("s3://") or "/" not in a.baseline:
        from aosphere_core_index.aws.s3_readonly import ReadOnlyS3
        if a.baseline.startswith("s3://"):
            rest = a.baseline[len("s3://"):]
            bucket, _, key = rest.partition("/")
        else:
            bucket, key = a.bucket, f"{a.prefix}/_baselines/{a.baseline}"
        base = json.loads(ReadOnlyS3(bucket=bucket, region=a.region).get_bytes(key))
    else:
        base = json.loads(Path(a.baseline).read_text())
    run, product = base["run"], base["product"]
    old = {d["label"]: d for d in base["documents"]}
    tree = G.gallery_tree("all", run)
    man = {d["slug"]: d for d in G.store(run).manifest()}
    new = {man[d["slug"]]["scorecard"].split("/")[1]: d
           for f in tree["folders"] if f["product"] == product
           for j in f["jurisdictions"] for d in j["documents"]}

    print(f"  {product}   baseline captured {base['captured_at']}")
    print(f"  {len(old)} baseline documents, {len(new)} now\n")
    better, worse, same, missing = [], [], [], []
    for label, o in sorted(old.items()):
        n = new.get(label)
        if n is None:
            missing.append(label); continue
        ow, nw = o.get("worst_score"), n.get("worst_score")
        if ow is None or nw is None:
            continue
        delta = round(nw - ow, 1)
        row = (delta, label, ow, nw, o.get("gate"), n.get("gate"),
               o.get("weakest"), n.get("weakest"))
        (better if delta > 0.05 else worse if delta < -0.05 else same).append(row)
    print(f"  improved {len(better)}   unchanged {len(same)}   REGRESSED {len(worse)}"
          + (f"   missing {len(missing)}" if missing else ""))
    print(f"\n  gate transitions: "
          f"{dict(Counter((o.get('gate'), new[l].get('gate')) for l, o in old.items() if l in new))}")
    if worse:
        print("\n  REGRESSIONS (the ones that matter — a re-extraction should not lose ground):")
        for d, l, ow, nw, og, ng, owk, nwk in sorted(worse):
            print(f"    {d:+6.1f}  {ow:5.1f} -> {nw:5.1f}  {og}->{ng}  {owk}->{nwk}  {l[:34]}")
    if better:
        print("\n  biggest improvements:")
        for d, l, ow, nw, og, ng, owk, nwk in sorted(better, reverse=True)[:10]:
            print(f"    {d:+6.1f}  {ow:5.1f} -> {nw:5.1f}  {og}->{ng}  {l[:34]}")
    if missing:
        print(f"\n  in the baseline but absent now: {missing}")


if __name__ == "__main__":
    main()
