"""Inspect local STEP files incrementally in isolated worker processes."""

import argparse
import json
from pathlib import Path
import subprocess
import sys
import tempfile

from component_cards import searchable_row
from inspect_step import SCHEMA_VERSION, inspect_step


def inspect_one(root, row, output, timeout):
    with tempfile.TemporaryDirectory(prefix="step-", dir=output.parent) as temporary:
        result_path = Path(temporary) / "result.json"
        try:
            job = subprocess.run(
                [sys.executable, "-B", __file__, "--root", str(root), "--worker",
                 row["path"], str(result_path)],
                stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                timeout=timeout,
            )
            if job.returncode == 0 and result_path.is_file():
                return json.loads(result_path.read_text(encoding="utf-8"))
            error = f"CAD worker exit code {job.returncode}"
        except subprocess.TimeoutExpired:
            error = f"CAD worker exceeded {timeout} seconds"
    return {"schema_version": SCHEMA_VERSION, "path": row["path"], "sha256": row["sha256"],
            "status": "worker_error", "errors": [error]}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--root", type=Path, default=Path(__file__).resolve().parents[1])
    parser.add_argument("--retry-errors", action="store_true")
    parser.add_argument("--timeout", type=int, default=60)
    parser.add_argument("--worker", nargs=2, metavar=("INPUT", "OUTPUT"), help=argparse.SUPPRESS)
    args = parser.parse_args()
    root = args.root.resolve()
    if args.worker:
        input_path = (root / args.worker[0]).resolve()
        target = Path(args.worker[1]).resolve()
        if not input_path.is_relative_to(root) or not target.is_relative_to(root / "reports"):
            parser.error("Worker paths must stay within the library.")
        hierarchy_only = input_path.stat().st_size > 32 * 1024**2
        report = inspect_step(input_path, root=root, max_nodes=30000,
                              max_metric_definitions=0 if hierarchy_only else 1800,
                              world_metrics=not hierarchy_only)
        target.write_text(json.dumps(report, ensure_ascii=False), encoding="utf-8")
        return
    output = root / "reports" / "step-inspection.jsonl"
    reports = {}
    if output.exists():
        for line in output.read_text(encoding="utf-8").splitlines():
            if line.strip():
                report = json.loads(line)
                reports[report["path"]] = report
    rows = json.loads((root / "catalog" / "inventory.json").read_text(encoding="utf-8"))
    by_bytes = {}
    for row in rows:
        report = reports.get(row["path"])
        if (report and report.get("schema_version") == SCHEMA_VERSION
                and report["status"] == "ok" and report["sha256"] == row["sha256"]):
            by_bytes[(row["asset_id"], row["sha256"])] = report
    for row in rows:
        if row["extension"] not in {".step", ".stp"} or not searchable_row(row):
            continue
        old = reports.get(row["path"])
        if (old and old.get("schema_version") == SCHEMA_VERSION and old["sha256"] == row["sha256"]
                and (old["status"] == "ok" or not args.retry_errors)):
            continue
        key = row["asset_id"], row["sha256"]
        prior = by_bytes.get(key)
        report = dict(prior, path=row["path"], reused_report_from=prior["path"]) if prior else inspect_one(
            root, row, output, args.timeout
        )
        if report["status"] == "ok":
            by_bytes[key] = report
        reports[row["path"]] = report
        temporary = output.with_suffix(".jsonl.part")
        temporary.write_text("".join(json.dumps(reports[key], ensure_ascii=False) + "\n"
                                     for key in sorted(reports)), encoding="utf-8")
        temporary.replace(output)
        print(f"{report['status']} {row['path']} ({report.get('definition_count', 0)} definitions)", flush=True)


if __name__ == "__main__":
    main()
