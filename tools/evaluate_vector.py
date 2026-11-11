"""Reopen the Chroma catalog in a separate process and compare retrieval on the same inputs."""

import argparse
from datetime import datetime, timezone
import json
from math import isclose
import os
from pathlib import Path
import subprocess
import sys
from tempfile import TemporaryDirectory

from ingest_forum import write_json
from library import ROOT, load, search
from vector_search import (COLLECTION, DIMENSIONS, MODEL, STORE, SYNC_REPORT, card_record,
                           digest, encoder, load_cards, open_collection, query_many,
                           stored_metadata, sync_collection)


PARAPHRASES = (
    ("行进和改变朝向各由不同的动力单元控制", "rm-803685-014"),
    ("桌面台架上先做两节腿机构的控制联调", "rm-815703-cb91f7be3de320dfd"),
    ("同一个转动中心套两路传动，分别带动大腿和小腿", "rm-776744-010"),
    ("用一块机加工件把车轮模组接到减振机构", "rm-1935523-022"),
)
PROBES = ("rm-803685-014", "rm-1935523-022", "rm-815703-cb91f7be3de320dfd")


def incremental_fixture(root, path):
    records = load_cards(root)
    first, replaced, added = PROBES
    model = encoder(root)
    collection = open_collection(path, create=True)
    initial = {mid: records[mid] for mid in (first, replaced)}
    start = sync_collection(collection, initial, model)
    old = collection.get(ids=[first], include=["embeddings"])["embeddings"][0]
    amended = {**records[first]["card"], "summary": "增量验证样本更新了悬挂行程说明。"}
    after = {first: card_record(amended), added: records[added]}
    change = sync_collection(collection, after, model)
    noop = sync_collection(collection, after, model)
    new = collection.get(ids=[first], include=["embeddings", "documents"])
    passed = (start["added"] == 2 and (change["added"], change["updated"], change["removed"]) == (1, 1, 1)
              and noop["unchanged"] == 2 and old.tolist() != new["embeddings"][0].tolist()
              and amended["summary"] in new["documents"][0] and collection.get(ids=[replaced])["ids"] == [])
    return {"passed": passed, "initial": start, "changed": change, "unchanged": noop}


def compact(hit):
    return {key: hit[key] for key in ("id", "name", "cad_source", "distance", "score", "model_matches")
            if key in hit}


def target_rank(hits, targets):
    return next((i for i, hit in enumerate(hits, 1) if hit["id"] in targets), None)


def evaluate(root):
    sync = load(root / SYNC_REPORT)
    if not sync or sync["writer_pid"] == os.getpid():
        raise ValueError("Run synchronization in one process, then run this evaluator separately")
    records = load_cards(root)
    model = encoder(root)
    collection = open_collection(root / STORE)
    metadata_ok = stored_metadata(collection) == {mid: record["metadata"] for mid, record in records.items()}
    if not metadata_ok:
        raise ValueError("Vector catalog is stale. Run vector_search.py sync")
    sample = collection.get(ids=list(PROBES), include=["documents", "embeddings", "metadatas"])
    embeddings = sample["embeddings"]
    raw_search = collection.query(query_embeddings=embeddings.tolist(), n_results=5,
                                  include=["distances"])
    roundtrips = []
    for index, (mid, embedding, document) in enumerate(zip(sample["ids"], embeddings, sample["documents"], strict=True)):
        cad = records[mid]["card"]["cad_source"]
        path = (root / cad["path"]).resolve()
        rank = raw_search["ids"][index].index(mid) + 1 if mid in raw_search["ids"][index] else None
        passed = (len(embedding) == DIMENSIONS and isclose(float(embedding @ embedding), 1.0, abs_tol=1e-4)
                  and document == records[mid]["text"] and rank is not None
                  and path.is_relative_to(root) and path.is_file() and digest(path) == cad["sha256"])
        roundtrips.append({"id": mid, "rank": rank, "dimension": len(embedding), "cad_source": cad, "passed": passed})

    fixture = load(root / "catalog/retrieval-benchmark.json")
    queries = [(kind, row[kind], row["targets"], kind, row["id"]) for row in fixture["pairs"]
               for kind in ("function", "model")]
    queries.extend(("paraphrase", query, [mid], "function", None) for query, mid in PARAPHRASES)
    negatives = [row for row in fixture["boundaries"] if not row["expected"]]
    queries.extend(("unknown_model", row["query"], [], "model", None) for row in negatives)
    result = query_many(collection, records, model, [row[1] for row in queries])
    rows = []
    for (kind, query, targets, sqlite_mode, sample_id), hits in zip(queries, result, strict=True):
        sqlite_hits = search(root, query, mode=sqlite_mode)
        rows.append({
            "kind": kind, "sample": sample_id, "query": query, "targets": targets, "sqlite_mode": sqlite_mode,
            "chroma_rank": target_rank(hits, targets), "sqlite_rank": target_rank(sqlite_hits, targets),
            "chroma": [compact(hit) for hit in hits], "sqlite": [compact(hit) for hit in sqlite_hits],
        })
    metrics = {}
    for kind in ("function", "model", "paraphrase"):
        cases = [row for row in rows if row["kind"] == kind]
        metrics[kind] = {"queries": len(cases)}
        for engine in ("chroma", "sqlite"):
            ranks = [row[f"{engine}_rank"] for row in cases]
            metrics[kind][engine] = {"top_1": ranks.count(1), "top_5": sum(rank is not None for rank in ranks)}
    with TemporaryDirectory(dir=root / "runtime", prefix="vector-fixture-") as scratch:
        stdout = subprocess.check_output([sys.executable, "-B", __file__, "--root", str(root),
                                          "--fixture-path", scratch], encoding="utf-8")
        incremental = json.loads(stdout)
    natural_roundtrips = [
        {"query": row["query"], "target": row["targets"][0], "rank": row["chroma_rank"]}
        for row in rows if row["kind"] == "function" and row["sample"] in {"B2", "C2", "F"}
    ]
    return {
        "checked_at_utc": datetime.now(timezone.utc).isoformat(),
        "passed": (len(roundtrips) == len(PROBES) and all(item["passed"] for item in roundtrips)
                   and sum(row["rank"] is not None for row in natural_roundtrips) >= 2
                   and incremental["passed"]),
        "collection": COLLECTION, "model": MODEL, "writer_pid": sync["writer_pid"],
        "reader_pid": os.getpid(), "count": collection.count(), "metadata_matches_catalog": metadata_ok,
        "roundtrips": roundtrips, "natural_roundtrips": natural_roundtrips,
        "incremental": incremental, "metrics": metrics, "queries": rows,
    }


def write_summary(path, report):
    lines = [
        "# Chroma 与 SQLite 检索对照", "",
        f"写入进程 PID {report['writer_pid']}，重开进程 PID {report['reader_pid']}。"
        f"Chroma 重开后读到 {report['count']} 条记录，卡片元数据与 SQLite 逐条核对通过。"
        "三个持久化样本用存储向量回查卡片，再复核源 CAD 的 SHA-256。"
        f"同批卡片的自然描述有 {sum(row['rank'] is not None for row in report['natural_roundtrips'])}/"
        f"{len(report['natural_roundtrips'])} 条在前五找回。", "",
        "| 输入 | 数量 | SQLite Top‑1 | SQLite Top‑5 | Chroma Top‑1 | Chroma Top‑5 |",
        "|---|---:|---:|---:|---:|---:|",
    ]
    for key, label in (("function", "原评测功能描述"), ("model", "型号或精确定位"), ("paraphrase", "另写的功能描述")):
        metric = report["metrics"][key]
        sql, vec = metric["sqlite"], metric["chroma"]
        lines.append(f"| {label} | {metric['queries']} | {sql['top_1']} | {sql['top_5']} |"
                     f" {vec['top_1']} | {vec['top_5']} |")
    lines.extend(["", "## 逐条对照", "", "| 输入 | SQLite 目标排名 | Chroma 目标排名 | Chroma 首条 |",
                  "|---|---:|---:|---|"])
    for row in report["queries"]:
        if not row["targets"]:
            continue
        first = row["chroma"][0]["name"] if row["chroma"] else "空"
        lines.append(f"| {row['query']} | {row['sqlite_rank'] or '—'} | {row['chroma_rank'] or '—'} | {first} |")
    negatives = [row for row in report["queries"] if not row["targets"]]
    lines.extend(["", f"未知型号用例共 {len(negatives)} 条。SQLite 有结果的查询为 {sum(bool(row['sqlite']) for row in negatives)} 条，"
                  f"Chroma 有相似候选的查询为 {sum(bool(row['chroma']) for row in negatives)} 条。精确型号过滤由 SQLite 承担。",
                  "向量对照按目标卡 ID 计算排名。功能样本和改写数量有限，语义候选的相关性未逐条标注。",
                  " [完整候选、来源与增量测试](VECTOR_ACCEPTANCE.json)", ""])
    path.write_text("\n".join(lines), encoding="utf-8")


def main():
    sys.stdout.reconfigure(encoding="utf-8")
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--root", type=Path, default=ROOT)
    parser.add_argument("--fixture-path", type=Path, help=argparse.SUPPRESS)
    args = parser.parse_args()
    root = args.root.resolve()
    if args.fixture_path:
        fixture = args.fixture_path.resolve()
        if fixture.parent != root / "runtime" or not fixture.name.startswith("vector-fixture-") or any(fixture.iterdir()):
            raise ValueError("Incremental acceptance requires a new, empty scratch directory")
        result = incremental_fixture(root, fixture)
        print(json.dumps(result))
    else:
        result = evaluate(root)
        write_json(root / "reports/VECTOR_ACCEPTANCE.json", result)
        write_summary(root / "reports/VECTOR_ACCEPTANCE.md", result)
        print(json.dumps({"passed": result["passed"], "count": result["count"], "metrics": result["metrics"],
                          "writer_pid": result["writer_pid"], "reader_pid": result["reader_pid"]}, indent=2))
    return 0 if result["passed"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
