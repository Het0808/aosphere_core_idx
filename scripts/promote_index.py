#!/usr/bin/env python3
"""The index half of a promotion: a staged gallery version -> searchable.

Driven by `promote_run.py --with-index`, which owns the run selection and the progress
object; this module owns the stages that turn the promoted documents into an index version
and cut over to it. Every stage is a thin orchestration of code that already exists, run as
a subprocess, so there is ONE implementation of each step and it is the one a human runs by
hand:

    seed          aws-free S3 download of index/<current>/products/  -> the data dir
    content       scripts/tree_to_content.py + scripts/merge_guidance_alerts.py
    embed         aci reembed          (see "the stale npz" below)
    flat          aci reindex          -> products/_multi/{multi.npz,manifest.json}
    publish       aci publish <bucket> <version> --no-set-latest
    index_verify  the gate
    cutover       index/latest = <version>
    vectors       scripts/load_vectors.py   (delete-then-refill of aci-vectors)

WHY SEEDING IS NOT OPTIONAL
`aci reindex` builds multi.npz from whatever is in the data dir. A dir holding only the
promoted regions produces an index containing only those regions, and flipping index/latest
to it would delete every other jurisdiction from search. So the incumbent version's
artifacts are downloaded first and the new version is a SUPERSET of the live one — which
index_verify then asserts, because "additive" is a property worth checking rather than
assuming.

THE STALE NPZ
`aci reembed` skips a region whose .npz already reports the target model. That is the right
rule for a re-embed and exactly the wrong one here: a promoted region gets a NEW
content.json while its seeded .npz still says "titan", so reembed would skip it and the
region would keep vectors describing the PREVIOUS extraction. Silent, and the kind of thing
found months later. So the content stage DELETES the .npz of every region it rewrites, and
one `aci reembed` then rebuilds exactly those and skips everything else. No library change,
no flag, and the invariant is local to the two stages that share it.

(The general fix is a content fingerprint on the .npz — see docs/PROMOTION_PIPELINE.md. It
is not needed while the deletion is right here next to the rewrite.)
"""
from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
import time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

HERE = Path(__file__).resolve().parent
REPO = HERE.parent
sys.path.insert(0, str(HERE))

from aosphere_core_index.regions.region_map import (
    canonical_jurisdiction, qualified, split_region)  # noqa: E402

TREE_TO_CONTENT = HERE / "tree_to_content.py"
MERGE_GUIDANCE = HERE / "merge_guidance_alerts.py"
LOAD_VECTORS = HERE / "load_vectors.py"

# Only the markdown is read by tree_to_content. A real Marketing Restrictions tree measured
# 22 MB, of which 552 KB is .md and 21 MB is page-snapshot PNGs — so restricting the download
# to *.md is a 40x saving and changes nothing about the result.
TREE_SUFFIX = ".md"
STAGE3 = "03_stage3_final"

DOWNLOAD_FANOUT = int(os.getenv("ACI_PROMOTE_FANOUT", "32"))


def log(msg: str) -> None:
    print(f"[{time.strftime('%H:%M:%S')}] {msg}", flush=True)



def region_of(row: dict) -> str:
    """The region id for a promoted document — the ONE place a plan row becomes a region.

    Via canonical_jurisdiction, because the corpus folder name is not the index's spelling:
    see that function for what promoting the raw name does to a live index.
    """
    return qualified(row["product"], canonical_jurisdiction(row["jurisdiction"]))


def artifacts_dir(data_dir: Path, qreg: str) -> Path:
    """<data_dir>/products/<product>/<jurisdiction>/artifacts — mirrors
    settings.region_artifacts, which is how the service resolves it. Duplicated from
    build_hybrid_index.artifacts_dir rather than imported, because importing that module
    pulls extract_to_s3 and the PDF stack."""
    product, jur = split_region(qreg)
    return data_dir / "products" / product / jur / "artifacts"


def _run(argv: list[str], env: dict | None = None, cwd: Path | None = None,
         on_line=None) -> tuple[int, str]:
    """Run a subprocess, streaming stdout so a long stage reports progress as it goes.

    Streamed rather than captured because `aci reembed` is minutes of work that prints one
    line per region: capturing it means a stage that looks frozen and then finishes.
    `entity_reindex.py` does the same thing for the same reason.
    """
    e = {**os.environ, **(env or {})}
    proc = subprocess.Popen(argv, cwd=str(cwd or REPO), env=e, stdout=subprocess.PIPE,
                            stderr=subprocess.STDOUT, text=True, bufsize=1)
    tail: list[str] = []
    for line in proc.stdout:                                 # type: ignore[union-attr]
        line = line.rstrip()
        tail.append(line)
        del tail[:-40]
        if on_line:
            on_line(line)
    proc.wait()
    return proc.returncode, "\n".join(tail)


def _aci(*args: str) -> list[str]:
    """The CLI, as a module so it runs under whichever interpreter is driving this."""
    return [sys.executable, "-m", "aosphere_core_index.cli", *args]


# ---------------------------------------------------------------- seed
def stage_seed(prog, s3, bucket: str, current: str | None, data_dir: Path,
               seed_from: str | None) -> dict:
    """Fill the data dir with the incumbent index version's artifacts.

    From S3 by default, because `index/<current>/` IS what is live — seeding from a local
    directory instead means the new version is a superset of whatever that directory happens
    to hold, which is not the same guarantee. `--seed-from <dir>` copies a local dir when you
    already have the right one and want to skip a ~1GB download.
    """
    data_dir.mkdir(parents=True, exist_ok=True)
    if seed_from:
        src = Path(seed_from).resolve() / "products"
        if not src.is_dir():
            raise RuntimeError(f"--seed-from {seed_from}: no products/ directory there")
        dst = data_dir / "products"
        log(f"seeding from {src} (local copy)")
        n = 0
        for p in src.rglob("*"):
            if not p.is_file() or p.name == "multi.npz":
                continue
            rel = p.relative_to(src)
            out = dst / rel
            out.parent.mkdir(parents=True, exist_ok=True)
            if not out.exists():
                shutil.copy2(p, out)
            n += 1
            if n % 100 == 0:
                prog.bump("seed", n=100)
                prog.publish()
        return {"seeded": n, "from": str(src)}

    if not current:
        # No pointer to read: the first index version in an empty bucket. Legitimate, but it
        # means there is no incumbent to be a superset OF, so say so rather than implying
        # the seed succeeded.
        log("⚠ no index/latest to seed from — this version will contain ONLY the promoted "
            "regions. That is correct only for a first-ever index.")
        return {"seeded": 0, "from": None, "no_incumbent": True}

    prefix = f"index/{current}/products/"
    keys = []
    for page in s3.get_paginator("list_objects_v2").paginate(Bucket=bucket, Prefix=prefix):
        for obj in page.get("Contents", []):
            k = obj["Key"]
            if k.endswith("/multi.npz"):
                continue                                     # rebuilt by `flat`
            keys.append((k, obj.get("Size", 0)))
    total_mb = sum(sz for _, sz in keys) / 1e6
    log(f"seeding from s3://{bucket}/{prefix} — {len(keys)} objects, {total_mb:.0f} MB")
    prog.d["stages"].setdefault("seed", {})["total"] = len(keys) or 1

    def fetch(item):
        key, _ = item
        rel = key[len(prefix):]
        out = data_dir / "products" / rel
        out.parent.mkdir(parents=True, exist_ok=True)
        if out.exists():
            return 0                                         # resumable
        s3.download_file(bucket, key, str(out))
        return 1

    got = 0
    # The S3 publish every 25 objects feeds the Promotion panel; the log line feeds the
    # terminal, which otherwise shows one "seeding from ..." and then nothing at all for a
    # ~1GB download and reads as a hang. Every 100 objects or 20s, whichever comes first.
    t0 = last = time.time()
    with ThreadPoolExecutor(max_workers=DOWNLOAD_FANOUT) as ex:
        for i, _ in enumerate(ex.map(fetch, keys), 1):
            got = i
            if i % 25 == 0:
                prog.d["stages"]["seed"]["done"] = i
                prog.publish()
            now = time.time()
            if i % 100 == 0 or now - last >= 20 or i == len(keys):
                last = now
                el = now - t0
                rate = i / el if el > 0 else 0.0
                eta = (len(keys) - i) / rate / 60 if rate > 0 else 0.0
                log(f"  seed {i}/{len(keys)} ({i / len(keys) * 100:.0f}%)  "
                    f"{rate:.1f}/s  eta {eta:.1f}m")
    prog.d["stages"]["seed"]["done"] = got
    return {"seeded": got, "from": f"index/{current}", "megabytes": round(total_mb, 1)}


# ---------------------------------------------------------------- content
def resolve_collisions(rows: list[dict], src_root: Path | None) -> tuple[list[dict], list[dict]]:
    """One document per (product, jurisdiction). -> (kept, collisions).

    The index keys content by product+jurisdiction, so two documents mapping to one region
    means one silently overwriting the other — and this is NORMAL, not exceptional:
    build_hybrid_index's own comment records "on Marketing Restrictions - Asset Management
    that is 17 of 88 jurisdictions", because a jurisdiction routinely holds a superseded
    opinion beside the current one.

    Resolved by the validated rule first — `extract_to_s3.keeper_stems`, which reads the
    jurisdiction's Doc_metadata.json and keeps the Survey (DP) or Memorandum (SD) — then by
    gate, then by score, then by doc id so it is deterministic. Every collision and its
    winner is RETURNED, not swallowed: silently picking one of two legal opinions is exactly
    the kind of decision that should appear in a report.
    """
    keeper_of = None
    if src_root is not None:
        try:
            from extract_to_s3 import keeper_stems
            keeper_of = keeper_stems
        except Exception:                                    # noqa: BLE001
            keeper_of = None

    groups: dict[str, list[dict]] = {}
    for r in rows:
        groups.setdefault(region_of(r), []).append(r)

    kept, collisions = [], []
    gate_rank = {"pass": 0, "review": 1}
    for qreg, group in sorted(groups.items()):
        if len(group) == 1:
            kept.append(group[0])
            continue
        candidates = group
        rule = "tiebreak"
        if keeper_of is not None:
            jur_dir = (src_root or Path(".")) / group[0]["product_dir"] / group[0]["jurisdiction"]
            try:
                stems = keeper_of(jur_dir, group[0]["product"]) if jur_dir.exists() else None
            except Exception:                                # noqa: BLE001
                stems = None
            if stems:
                narrowed = [r for r in group if str(r["doc_id"]) in stems]
                if narrowed:
                    candidates, rule = narrowed, "keeper_stems"
        winner = sorted(candidates, key=lambda r: (
            gate_rank.get(r.get("gate") or "", 9),
            -(r.get("worst_score") or 0),
            str(r["doc_id"]),
        ))[0]
        kept.append(winner)
        collisions.append({"region": qreg, "rule": rule, "kept": winner["doc_id"],
                           "dropped": [r["doc_id"] for r in group
                                       if r["doc_id"] != winner["doc_id"]]})
    return kept, collisions


def stage_content(prog, s3, bucket: str, run_prefix: str, rows: list[dict],
                  data_dir: Path, trees_dir: Path, src_root: Path | None) -> dict:
    """Download each promoted document's stage-3 markdown and build its content.json."""
    kept, collisions = resolve_collisions(rows, src_root)
    for c in collisions:
        log(f"  ! {c['region']}: kept {c['kept']}, dropped {','.join(c['dropped'])} "
            f"({c['rule']})")
    prog.d["stages"].setdefault("content", {})["total"] = len(kept)
    prog.d["collisions"] = collisions

    built, failed, dropped_guidance = 0, [], 0
    rewritten: list[str] = []
    stopped = False
    for i, row in enumerate(kept, 1):
        if prog.stopping():
            # Reported, not just broken out of. A caller that cannot tell "stopped at 195
            # of 322" from "converted all 322" marks the stage complete and walks on to
            # embed, publish and CUTOVER with a partially rewritten index — and the verify
            # gate cannot catch it, because seeding already put every incumbent region in
            # place, so the superset assertion holds. See the `stopped` branch in
            # run_index_stages.
            stopped = True
            log(f"stop requested — content halted at {i - 1}/{len(kept)} documents")
            break
        qreg = region_of(row)
        prog.d["current"] = {"job": row["job"], "region": qreg, "started_at": time.time()}
        # Bound per iteration, because a failure can be raised before either is assigned
        # (no stage-3 markdown in S3, for one) and the restore below must not then read a
        # name left over from the PREVIOUS document — which would copy one region's
        # incumbent over another's content.
        cj = incumbent = None
        try:
            tree = _download_tree(s3, bucket, f"{run_prefix}/{row['job']}", trees_dir,
                                  row["job"])
            if tree is None:
                raise RuntimeError(f"no {STAGE3} markdown in S3 for {row['job']}")
            art = artifacts_dir(data_dir, qreg)
            art.mkdir(parents=True, exist_ok=True)
            cj = art / f"{qreg}.content.json"

            # The incumbent is the guidance/alerts source. Copied aside rather than read
            # in place, because tree_to_content OVERWRITES cj wholesale.
            #
            # An existing .incumbent is NEVER overwritten. It is only ever left behind by a
            # pass that parked the real content here and then failed before the merge, so it
            # — not cj — is the pre-promotion content. Copying cj over it on the retry
            # destroys the guidance source permanently: run 2026-08-21-02's Iceland failed
            # with "0 sections", leaving a 239-byte cj beside a 366KB .incumbent holding 79
            # sections, and the next pass would have overwritten the second with the first.
            incumbent = cj.with_suffix(".json.incumbent")
            if incumbent.exists():
                pass                                         # a previous pass's, and true
            elif cj.exists():
                shutil.copy2(cj, incumbent)
            else:
                incumbent = None

            rc, out = _run([sys.executable, str(TREE_TO_CONTENT), str(cj), str(tree),
                            qreg, row["product"]])
            if rc != 0:
                raise RuntimeError(f"tree_to_content failed: {out[-300:]}")
            nsec = len(json.loads(cj.read_text()).get("sections", []))
            if not nsec:
                # Five regions once published as EMPTY rather than obviously broken. A
                # region with no sections is a silent hole in the corpus, not a document.
                raise RuntimeError("content.json has 0 sections")

            note = "clause bodies only (no incumbent)"
            if incumbent is not None:
                rc2, out2 = _run([sys.executable, str(MERGE_GUIDANCE), str(cj),
                                  str(incumbent)])
                note = (out2.strip().splitlines() or ["(merge: no output)"])[-1]
                if rc2 != 0:
                    raise RuntimeError(f"merge_guidance_alerts failed: {out2[-300:]}")
                # "N attached (M dropped)" — a re-extraction that renumbers parts can strand
                # a lawyer's curated Q&A, and nothing has ever read this line.
                if "dropped" in note:
                    try:
                        dropped_guidance += int(note.split("(")[1].split(" dropped")[0])
                    except (IndexError, ValueError):
                        pass
                incumbent.unlink(missing_ok=True)

            # THE STALE NPZ — see the module docstring. Delete it so `aci reembed` rebuilds
            # exactly this region and skips every seeded one.
            npz = art / f"{qreg}.sections.npz"
            npz.unlink(missing_ok=True)
            rewritten.append(qreg)

            built += 1
            prog.bump("content")
            prog._ledger_row({"ts": time.time(), "stage": "content", "region": qreg,
                              "sections": nsec, "note": note, "status": "ok"})
            log(f"  ✓ [{i}/{len(kept)}] {qreg:44s} sec={nsec:<4} {note}")
        except Exception as e:                               # noqa: BLE001
            # Put the region back the way it was found. tree_to_content has already
            # overwritten cj by the time most failures are raised, so leaving it alone
            # leaves a BROKEN region on disk — an empty content.json that a later
            # --allow-content-failures run would happily publish over a good one. The
            # failure is still reported and still fails the stage; this only ensures the
            # data dir never carries a half-converted region forward.
            if cj is not None and incumbent is not None and incumbent.exists():
                shutil.copy2(incumbent, cj)
            failed.append({"region": qreg, "job": row["job"],
                           "error": f"{type(e).__name__}: {e}"[:400]})
            prog.bump("content")
            prog.bump("content", "failed")
            log(f"  ✗ [{i}/{len(kept)}] {qreg}: {e}")
        prog.publish()

    prog.d["current"] = None
    return {"built": built, "failed": failed, "rewritten": rewritten,
            "collisions": collisions, "guidance_dropped": dropped_guidance,
            "documents": len(kept), "stopped": stopped}


def _download_tree(s3, bucket: str, job_prefix: str, trees_dir: Path,
                   job: str) -> Path | None:
    """The stage-3 markdown for one document, into a local scratch tree.

    A recursive listing scoped to ONE job directory, which is a few dozen keys — not the
    run-wide recursive listing the gallery walk is forbidden from doing.
    """
    prefix = f"{job_prefix}/{STAGE3}/"
    dest = trees_dir / job / STAGE3
    keys = []
    for page in s3.get_paginator("list_objects_v2").paginate(Bucket=bucket, Prefix=prefix):
        for obj in page.get("Contents", []):
            if obj["Key"].endswith(TREE_SUFFIX):
                keys.append(obj["Key"])
    if not keys:
        return None

    def fetch(key: str):
        out = dest / key[len(prefix):]
        out.parent.mkdir(parents=True, exist_ok=True)
        if not out.exists():
            s3.download_file(bucket, key, str(out))

    with ThreadPoolExecutor(max_workers=DOWNLOAD_FANOUT) as ex:
        list(ex.map(fetch, keys))
    return dest


# ---------------------------------------------------------------- embed + flat
def stage_embed(prog, data_dir: Path, rewritten: list[str], embed_env: dict) -> dict:
    """One `aci reembed`, which rebuilds exactly the regions whose .npz the content stage
    deleted and skips every seeded one by model match.

    One subprocess for the whole stage rather than one per region: at ~88 regions the
    per-process startup would dominate, and reembed's own resume rule is what makes a single
    invocation correct. Its stdout is streamed so the stage reports progress instead of
    looking frozen for ten minutes.
    """
    prog.d["stages"].setdefault("embed", {})["total"] = len(rewritten) or 1
    done = {"n": 0}

    def on_line(line: str):
        # reembed prints one line per region, "skip <r> (already ...)" or the region's
        # result. Counting lines that name a rewritten region is enough for a percentage,
        # and it costs nothing — the alternative is parsing a format that will change.
        if any(r in line for r in rewritten):
            done["n"] += 1
            prog.d["stages"]["embed"]["done"] = min(done["n"], len(rewritten))
            prog.publish()
        if line.strip():
            log(f"    {line[:160]}")

    env = {"ACI_DATA_DIR": str(data_dir), **embed_env}
    rc, tail = _run(_aci("reembed"), env=env, on_line=on_line)
    if rc != 0:
        raise RuntimeError(f"aci reembed failed (rc={rc}): {tail[-500:]}")
    prog.d["stages"]["embed"]["done"] = len(rewritten) or 1
    return {"regions": len(rewritten), "tail": tail.splitlines()[-3:]}


def stage_flat(prog, data_dir: Path, version: str, run: str, embed_env: dict) -> dict:
    """`aci reindex` -> products/_multi/{multi.npz,manifest.json}.

    ACI_INDEX_VERSION and ACI_SOURCE_RUN are set here so the manifest RECORDS which version
    it is and which extraction produced it — the two fields index_verify and a pod's
    readiness check both need, and which nothing wrote before.
    """
    env = {"ACI_DATA_DIR": str(data_dir), "ACI_INDEX_VERSION": version,
           "ACI_SOURCE_RUN": run, **embed_env}
    rc, tail = _run(_aci("reindex"), env=env, on_line=lambda ln: log(f"    {ln[:160]}"))
    if rc != 0:
        raise RuntimeError(f"aci reindex failed (rc={rc}): {tail[-500:]}")
    man = read_manifest(data_dir)
    return {"rows": man.get("rows"), "regions": man.get("regions"),
            "model": man.get("model"), "dim": man.get("dim")}


def read_manifest(data_dir: Path) -> dict:
    p = data_dir / "products" / "_multi" / "manifest.json"
    try:
        return json.loads(p.read_text())
    except (OSError, ValueError):
        return {}


# ---------------------------------------------------------------- publish
def stage_publish(prog, bucket: str, version: str, data_dir: Path,
                  profile: str | None) -> dict:
    """`aci publish --no-set-latest`. Staged: nothing points at it yet.

    --no-set-latest is not a preference. The pointer flip is a separate stage AFTER the
    verify gate, so a bad index is never something a reader can reach.
    """
    argv = _aci("publish", bucket, version, "--no-set-latest")
    if profile:
        argv += ["--profile", profile]
    rc, tail = _run(argv, env={"ACI_DATA_DIR": str(data_dir)},
                    on_line=lambda ln: log(f"    {ln[:160]}"))
    if rc != 0:
        raise RuntimeError(f"aci publish failed (rc={rc}): {tail[-500:]}")
    return {"tail": tail.splitlines()[-2:], "prefix": f"index/{version}"}


# ---------------------------------------------------------------- the gate
def stage_index_verify(prog, s3, bucket: str, version: str, current: str | None,
                       data_dir: Path, promoted_regions: list[str]) -> dict:
    """Everything that must be true before the pointer moves. Cheapest check first.

    Runs while NOTHING a reader can see has changed, which is why a failure here has nothing
    to roll back — the whole reason this stage sits between publish and cutover.
    """
    checks: list[dict] = []

    def check(name: str, ok: bool, detail: str = "") -> bool:
        checks.append({"check": name, "ok": bool(ok), "detail": detail})
        log(f"    {'✓' if ok else '✗'} {name}" + (f" — {detail}" if detail else ""))
        return bool(ok)

    man = read_manifest(data_dir)
    rows, regions = man.get("rows"), man.get("jurisdictions") or []
    names = {j["name"] if isinstance(j, dict) else str(j) for j in regions}

    ok = True
    ok &= check("manifest has a row count", bool(rows), f"rows={rows}")
    ok &= check("manifest names this version", man.get("version") == version,
                f"{man.get('version')!r} vs {version!r}")

    # 2. rows == the matrix. The check both silent partial loads needed and neither had.
    try:
        import numpy as np
        npz = data_dir / "products" / "_multi" / "multi.npz"
        n = int(np.load(npz, allow_pickle=False)["matrix"].shape[0])
        ok &= check("rows match multi.npz", n == rows, f"{n} in npz, {rows} in manifest")
    except Exception as e:                                   # noqa: BLE001
        ok &= check("rows match multi.npz", False, f"{type(e).__name__}: {e}")

    # 3. THE SUPERSET CHECK. Flipping index/latest to a version missing regions the live one
    # has DELETES those jurisdictions from search. This is the check that makes the promotion
    # provably additive rather than hopefully additive.
    if current:
        live = _live_regions(s3, bucket, current)
        if live is None:
            ok &= check("incumbent regions are all present", False,
                        f"could not read index/{current}'s manifest — refusing to flip "
                        f"blind. Pass --no-set-latest to stage anyway.")
        else:
            missing = sorted(live - names)
            ok &= check("incumbent regions are all present", not missing,
                        f"{len(missing)} region(s) would DISAPPEAR from search: "
                        f"{', '.join(missing[:5])}" if missing
                        else f"{len(live)} live regions all carried over")
    else:
        check("incumbent regions are all present", True, "no incumbent (first index version)")

    # 4. Every promoted region actually made it in. Otherwise the gallery shows documents
    # the index cannot answer about.
    absent = sorted(set(promoted_regions) - names)
    ok &= check("promoted regions are in the index", not absent,
                f"{len(absent)} missing: {', '.join(absent[:5])}" if absent
                else f"{len(promoted_regions)} region(s)")

    # 5. Artifacts on disk, per region, non-empty and with sections. A region that published
    # as an empty content.json is invisible rather than obviously broken.
    empty, unreadable = [], []
    for name in sorted(names):
        art = artifacts_dir(data_dir, name)
        cj, npz = art / f"{name}.content.json", art / f"{name}.sections.npz"
        if not cj.exists() or not npz.exists() or npz.stat().st_size == 0:
            unreadable.append(name)
            continue
        try:
            if not (json.loads(cj.read_text()).get("sections") or []):
                empty.append(name)
        except (OSError, ValueError):
            unreadable.append(name)
    ok &= check("every region has readable artifacts", not unreadable,
                f"{len(unreadable)}: {', '.join(unreadable[:5])}" if unreadable else "")
    ok &= check("no region has zero sections", not empty,
                f"{len(empty)}: {', '.join(empty[:5])}" if empty else "")

    # 6. The published prefix holds what we just built.
    published = _count_published(s3, bucket, version)
    expected = 2 * len(names) + 2                            # npz + content per region, + _multi
    ok &= check("published object count is plausible", published >= 2 * len(names),
                f"{published} objects under index/{version}/ (expected ~{expected})")

    return {"ok": ok, "checks": checks, "rows": rows, "regions": len(names),
            "promoted_regions": len(promoted_regions)}


def _live_regions(s3, bucket: str, current: str) -> set[str] | None:
    try:
        body = s3.get_object(Bucket=bucket,
                             Key=f"index/{current}/products/_multi/manifest.json"
                             )["Body"].read()
        man = json.loads(body)
    except Exception:                                        # noqa: BLE001
        return None
    return {j["name"] if isinstance(j, dict) else str(j)
            for j in (man.get("jurisdictions") or [])}


def _count_published(s3, bucket: str, version: str) -> int:
    n = 0
    for page in s3.get_paginator("list_objects_v2").paginate(
            Bucket=bucket, Prefix=f"index/{version}/products/"):
        n += len(page.get("Contents", []))
    return n


# ---------------------------------------------------------------- cutover
def stage_cutover(prog, s3, bucket: str, version: str, current: str | None,
                  pointer: str = "index/latest") -> dict:
    """Flip the pointer. ONE object, and the whole cutover.

    Inert for RUNNING pods: /data is a read-only volume mount whose prefix is fixed at pod
    creation (Dockerfile:6-13 — the artifacts are mounted, not baked), so this changes what
    the NEXT pod resolves and nothing else. That is exactly why it is safe to do before the
    vector reload, and exactly why the promotion cannot make itself live on its own.
    """
    s3.put_object(Bucket=bucket, Key=pointer, Body=version.encode(),
                  ContentType="text/plain")
    log(f"    {pointer}: {current or '(unset)'} -> {version}")
    return {"pointer": pointer, "was": current, "now": version}


# ---------------------------------------------------------------- vectors
def stage_vectors(prog, data_dir: Path, vector_env: dict) -> dict:
    """Clear the vector index and reload it from the new multi.npz.

    `scripts/load_vectors.py` -> `vector_load.load_opensearch`, which deletes the index and
    refills it. So there IS a window — measured around four minutes on this corpus — in which
    search returns nothing, and no rollback target while it runs. That is the trade this mode
    makes deliberately; `docs/VECTOR_INDEX_LIFECYCLE.md` requirement 1 (a versioned index
    behind an alias, flipped atomically) is the version without the window, and is not built.

    A partial load RAISES rather than reporting success — `load_opensearch` compares the
    final count against the row total, because 2,000-of-84,197 and 11,000-of-77,939 both
    once looked like clean runs.
    """
    env = {"ACI_DATA_DIR": str(data_dir), **vector_env}
    rc, tail = _run([sys.executable, str(LOAD_VECTORS)], env=env,
                    on_line=lambda ln: log(f"    {ln[:160]}"))
    if rc != 0:
        raise RuntimeError(f"load_vectors failed (rc={rc}): {tail[-500:]}")
    return {"backend": env.get("ACI_VECTOR_BACKEND"),
            "index": env.get("ACI_VECTOR_INDEX", "aci-vectors"),
            "tail": tail.splitlines()[-3:]}


# ---------------------------------------------------------------- orchestration
def run_index_stages(prog, s3, s3ro, cfg, rows: list[dict], run: str, run_prefix: str,
                     version: str) -> int:
    """The index half, on the promotion's own progress object. -> exit code.

    `cfg` is promote_run's parsed args. Stages run in the order STAGES declares, and each one
    is skipped if a previous pass already finished it — so a re-run with the same --version
    picks up where it stopped rather than repeating an hour of embedding.
    """
    data_dir = Path(cfg.data_dir or f"data-{version}").resolve()
    trees_dir = Path(cfg.trees_dir or f"out/_promote_trees/{version}").resolve()
    src_root = Path(cfg.src_root_local).resolve() if cfg.src_root_local else None
    if src_root is not None and not src_root.is_dir():
        log(f"⚠ --src-root-local {src_root} does not exist — the keeper filter for "
            f"colliding documents will fall back to gate/score/id")
        src_root = None
    embed_env = {k: v for k, v in (
        ("ACI_EMBED_BACKEND", cfg.embed_backend),
        ("ACI_BEDROCK_REGION", cfg.bedrock_region),
    ) if v}
    vector_env = {k: v for k, v in (
        ("ACI_VECTOR_BACKEND", cfg.vector_backend),
        ("ACI_VECTOR_INDEX", cfg.vector_index),
        ("ACI_OPENSEARCH_URL", cfg.opensearch_url),
    ) if v}

    current = _read_pointer(s3, cfg.bucket)
    log(f"index chain -> version {version}  (index/latest is currently "
        f"{current or 'unset'})")
    log(f"  data dir   {data_dir}")
    log(f"  trees      {trees_dir}")

    def done(key: str) -> bool:
        # `PromotionProgress.fail()` also stamps `finished_at` (so `stage_rows()`'s ladder can
        # tell "failed a while ago" from "still stuck mid-stage" for the UI) -- which makes a
        # bare `finished_at` check indistinguishable from a real completion here. Without the
        # state guard, a resume after any stage failure treats that stage as already done and
        # skips straight past it: measured on this promotion, an embed failure on one region
        # left its .npz missing, and the resume silently built and published a 321-region
        # index instead of 322 -- caught by index_verify's superset check, but only by luck of
        # that particular gate existing, not because the resume logic was correct.
        rec = prog.d["stages"].get(key) or {}
        return bool(rec.get("finished_at")) and rec.get("state") != "failed"

    # ---- seed ----
    if not done("seed"):
        prog.stage("seed", total=1)
        try:
            res = stage_seed(prog, s3, cfg.bucket, current, data_dir, cfg.seed_from)
        except Exception as e:                               # noqa: BLE001
            prog.fail("seed", f"{type(e).__name__}: {e}")
            return 1
        prog.finish("seed", note=f"{res['seeded']} artifact(s) from {res['from']}")

    # ---- content ----
    if not done("content"):
        prog.stage("content", total=len(rows))
        try:
            res = stage_content(prog, s3, cfg.bucket, run_prefix, rows, data_dir,
                                trees_dir, src_root)
        except Exception as e:                               # noqa: BLE001
            prog.fail("content", f"{type(e).__name__}: {e}")
            return 1
        prog.d["content"] = {k: v for k, v in res.items() if k != "rewritten"}
        prog.d["rewritten"] = res["rewritten"]
        if res.get("stopped"):
            # A stop is not a completed stage. Failing here leaves `finished_at` unset, so
            # `done("content")` stays false and a re-run resumes content instead of
            # embedding and publishing a half-rewritten index. --allow-content-failures
            # deliberately does NOT cover this: that flag is about documents that cannot
            # convert, not about a stage that never finished asking.
            prog.fail("content", f"stopped by request at {res['built']}/{res['documents']} "
                                 f"document(s) — nothing published, index/latest untouched")
            return 1
        if res["failed"] and not cfg.allow_content_failures:
            # A document that reached the gallery but has no content.json is a document the
            # gallery offers and the index cannot answer about. Refusing here keeps the two
            # describing the same corpus; --allow-content-failures is the deliberate override.
            prog.fail("content", f"{len(res['failed'])} document(s) failed to convert: "
                                 f"{', '.join(f['region'] for f in res['failed'][:5])}"
                                 f" — pass --allow-content-failures to publish without them")
            return 1
        note = f"{res['built']} region(s)"
        if res["collisions"]:
            note += f", {len(res['collisions'])} collision(s) resolved"
        if res["guidance_dropped"]:
            note += f", {res['guidance_dropped']} curated guidance key(s) dropped"
            log(f"  ⚠ {res['guidance_dropped']} curated guidance clause-key(s) could not be "
                f"re-attached — a re-extraction that renumbers parts strands them. "
                f"Worth a look before this goes live.")
        prog.finish("content", note=note)

    rewritten = prog.d.get("rewritten") or []

    # ---- embed ----
    if not done("embed"):
        prog.stage("embed", total=len(rewritten) or 1,
                   note=f"{len(rewritten)} region(s) rewritten; the rest are skipped by "
                        f"model match")
        try:
            res = stage_embed(prog, data_dir, rewritten, embed_env)
        except Exception as e:                               # noqa: BLE001
            prog.fail("embed", f"{type(e).__name__}: {e}")
            return 1
        prog.finish("embed", note=f"{res['regions']} region(s) embedded")

    # ---- flat ----
    if not done("flat"):
        prog.stage("flat", total=1)
        try:
            res = stage_flat(prog, data_dir, version, run, embed_env)
        except Exception as e:                               # noqa: BLE001
            prog.fail("flat", f"{type(e).__name__}: {e}")
            return 1
        prog.d["index"] = res
        prog.finish("flat", done=1,
                    note=f"{res['rows']} rows across {res['regions']} regions "
                         f"({res['model']})")

    # ---- publish ----
    if not done("publish"):
        prog.stage("publish", total=1)
        try:
            res = stage_publish(prog, cfg.bucket, version, data_dir, cfg.profile)
        except Exception as e:                               # noqa: BLE001
            prog.fail("publish", f"{type(e).__name__}: {e}")
            return 1
        prog.finish("publish", done=1, note=f"staged at {res['prefix']} (latest untouched)")

    # ---- the gate ----
    promoted_regions = sorted({region_of(r) for r in rows})
    prog.stage("index_verify", total=1)
    try:
        res = stage_index_verify(prog, s3, cfg.bucket, version, current, data_dir,
                                 promoted_regions)
    except Exception as e:                                   # noqa: BLE001
        prog.fail("index_verify", f"{type(e).__name__}: {e}")
        return 1
    prog.d["verify"] = res
    if not res["ok"]:
        bad = [c["check"] for c in res["checks"] if not c["ok"]]
        prog.fail("index_verify", "failed: " + "; ".join(bad))
        log("")
        log("✗ VERIFY FAILED — nothing has been flipped and nothing is live.")
        log(f"  index/{version}/ is staged and index/latest still points at "
            f"{current or '(unset)'}.")
        log("  Fix the cause and re-run with the same --version; the finished stages resume.")
        return 1
    prog.finish("index_verify", done=1, note=f"{len(res['checks'])} checks passed")

    # ---- cutover ----
    if not cfg.set_latest:
        prog.publish(force=True)
        log("")
        log(f"✓ index {version} PUBLISHED AND VERIFIED, pointer NOT flipped "
            f"(--no-set-latest).")
        log(f"  Flip it when ready:  aws s3 cp - s3://{cfg.bucket}/index/latest "
            f'<<< "{version}"')
        return 0
    if not done("cutover"):
        prog.stage("cutover", total=1)
        try:
            res = stage_cutover(prog, s3, cfg.bucket, version, current)
        except Exception as e:                               # noqa: BLE001
            prog.fail("cutover", f"{type(e).__name__}: {e}")
            return 1
        prog.d["cutover"] = res
        prog.finish("cutover", done=1, note=f"index/latest -> {version}")

    # ---- vectors ----
    if cfg.reload_vectors and not done("vectors"):
        prog.stage("vectors", total=1,
                   note="the store is EMPTY while this runs — search returns nothing")
        try:
            res = stage_vectors(prog, data_dir, vector_env)
        except Exception as e:                               # noqa: BLE001
            # The pointer is already flipped and the store may be empty or partial. Say so
            # in the terms an operator needs, not as a traceback.
            prog.fail("vectors", f"{type(e).__name__}: {e}")
            log("")
            log("✗ VECTOR LOAD FAILED, and index/latest is ALREADY flipped. The vector "
                "store may be empty or partial — search will return nothing or too little.")
            log(f"  Re-run just this step:  ACI_DATA_DIR={data_dir} "
                f"ACI_VECTOR_BACKEND={vector_env.get('ACI_VECTOR_BACKEND', 'opensearch')} "
                f"python scripts/load_vectors.py")
            log(f"  Or roll back the pointer:  aws s3 cp - "
                f's3://{cfg.bucket}/index/latest <<< "{current}"')
            return 1
        prog.finish("vectors", done=1,
                    note=f"reloaded {res['index']} ({res['backend']})")

    prog.publish(force=True)
    _final_report(prog, cfg, version, current, data_dir)
    return 0


def _pointer_is_absent(exc: Exception) -> bool:
    """True only for "that key is not there", never for "I could not ask"."""
    code = getattr(exc, "response", {}).get("Error", {}).get("Code", "")
    status = getattr(exc, "response", {}).get("ResponseMetadata", {}).get("HTTPStatusCode")
    return code in ("NoSuchKey", "NotFound", "404") or status == 404


def _read_pointer_strict(s3, bucket: str, pointer: str, what: str) -> str | None:
    """The index pointer, or None ONLY when it genuinely does not exist.

    An ABSENT pointer and an UNREADABLE one are different facts, and collapsing both to
    None — which a bare `except Exception: return None` does — is how an expired SSO token
    silently empties the search index. The chain is short and entirely quiet: the pointer
    reads as None, `stage_seed` skips (there is no incumbent to seed FROM and it says so in
    a warning nobody is watching for), `stage_index_verify`'s superset check is WAIVED via
    its "no incumbent (first index version)" branch, and `stage_cutover` flips index/latest
    to a version holding only the promoted regions. Every jurisdiction outside this
    promotion disappears from search, and every gate passes.

    So a read failure raises here instead. A first-ever index in an empty bucket still
    returns None, because that is a real 404 rather than a failure to ask.
    """
    try:
        body = s3.get_object(Bucket=bucket, Key=pointer)["Body"].read()
    except Exception as e:                                   # noqa: BLE001
        if _pointer_is_absent(e):
            return None
        # The S3 error CODE is the actionable half — "ExpiredToken" and "AccessDenied"
        # need different fixes, and the exception type says neither.
        code = getattr(e, "response", {}).get("Error", {}).get("Code") or type(e).__name__
        raise RuntimeError(
            f"could not read s3://{bucket}/{pointer} ({code}: {e}). "
            f"Refusing to continue{what}: an unreadable pointer is not the same fact as "
            f"'no incumbent', and treating it as one publishes an index containing only "
            f"the promoted regions. If this is an expired session: "
            f"aws sso login --sso-session tools1") from e
    return body.decode().strip() or None


def _read_pointer(s3, bucket: str, pointer: str = "index/latest") -> str | None:
    return _read_pointer_strict(s3, bucket, pointer, " with the index chain")


def _final_report(prog, cfg, version: str, previous: str | None, data_dir: Path) -> None:
    """What is live, what is not, and the one thing this cannot do for you."""
    idx = prog.d.get("index") or {}
    log("")
    log(f"✓ {version} is now index/latest — {idx.get('rows', '?')} rows across "
        f"{idx.get('regions', '?')} regions.")
    if cfg.reload_vectors:
        log(f"  Vector store reloaded from {data_dir}/products/_multi/multi.npz.")
    else:
        log("  Vector store NOT reloaded (--no-reload-vectors): search still answers from "
            "the previous index's vectors.")
    log("")
    # The honest limit. Saying "done" here would be the one claim an operator most needs to
    # not be wrong about.
    log("  NOT yet searchable for RUNNING pods. /data is a read-only volume mount whose")
    log("  prefix is fixed at pod creation, so a running pod still has the previous")
    log("  version's content.json. Newly promoted regions will be dropped from results")
    log("  (registry logs BundleUnavailable per region) until the pods are replaced:")
    log("      kubectl -n <ns> rollout restart deploy/core-index")
    log("  Locally: docker compose up -d --force-recreate core-index")
    log("")
    log(f"  Roll back:  aws s3 cp - s3://{cfg.bucket}/index/latest <<< \"{previous}\"")
    log(f"              then re-run load_vectors.py against index/{previous}'s artifacts.")
