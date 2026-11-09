"""Incrementally inspect immutable STEP assets; retain prior hash-matching results."""
import argparse,json,sys
from pathlib import Path
from inspect_step import inspect_step
def main():
 p=argparse.ArgumentParser();p.add_argument("--root",type=Path,default=Path(__file__).resolve().parents[1]);p.add_argument("--retry-errors",action="store_true");a=p.parse_args()
 root=a.root.resolve();path=root/"reports"/"step-inspection.jsonl";reports={}
 if path.exists():
  for line in path.read_text(encoding="utf-8").splitlines():
   if line.strip():
    r=json.loads(line);reports[r["path"]]=r
 files=json.loads((root/"catalog"/"inventory.json").read_text(encoding="utf-8"))
 for f in files:
  if f["extension"] not in {".step",".stp"}:continue
  old=reports.get(f["path"])
  if old and old.get("sha256")==f["sha256"] and (old.get("status")=="ok" or not a.retry_errors):continue
  print("INSPECT "+f["path"],flush=True)
  report=inspect_step(root/f["path"],root=root,max_nodes=30000,max_metric_definitions=3000)
  reports[f["path"]]=report
  temporary=path.with_name(path.name+".part")
  temporary.write_text("".join(json.dumps(reports[k],ensure_ascii=False)+"\n" for k in sorted(reports)),encoding="utf-8");temporary.replace(path)
  print(json.dumps({"path":f["path"],"status":report.get("status"),"definitions":report.get("definition_count"),"occurrences":report.get("occurrence_count"),"errors":report.get("errors")},ensure_ascii=False),flush=True)
 print("STEP reports retained: "+str(len(reports)),flush=True)
if __name__=="__main__":
 sys.stdout.reconfigure(encoding="utf-8");main()
