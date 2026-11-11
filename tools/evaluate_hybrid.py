"""Benchmark the actual loopback HTTP hybrid endpoint against both source rankings."""

import argparse
from collections import Counter
from datetime import datetime, timezone
import json
from pathlib import Path
from threading import Thread
from urllib.parse import urlencode
from urllib.request import urlopen

from evaluate_vector import PARAPHRASES, target_rank
from http_service import make_server
from ingest_forum import write_json
from library import ROOT, load, result_content_key, search


LIMIT = 5


def evaluate(root):
    server = make_server(root, 0)
    thread = Thread(target=server.serve_forever, daemon=True)
    thread.start()
    base = f"http://127.0.0.1:{server.server_port}/v1/search?"

    def http(query, mode):
        args = {"q": query, "mode": mode, "limit": LIMIT}
        with urlopen(base + urlencode(args), timeout=30) as response:
            return json.load(response)

    try:
        fixture = load(root / "catalog/retrieval-benchmark.json")
        cases = [("original", row["id"], row["function"], row["targets"]) for row in fixture["pairs"]]
        cases += [("paraphrase", f"P{i}", query, [mid]) for i, (query, mid) in enumerate(PARAPHRASES, 1)]
        rows = []
        for group, sample, query, targets in cases:
            hybrid = http(query, "function")
            if hybrid["retrieval"] != "hybrid" or hybrid["vector_status"] != "ready":
                raise RuntimeError(f"HTTP did not run both retrieval channels: {hybrid['vector_status']}")
            lexical = search(root, query, LIMIT, mode="function")
            semantic = server.catalog.semantic.search(query, LIMIT)
            rankings = {"sqlite": lexical, "chroma": semantic, "hybrid": hybrid["items"]}
            cards = server.catalog.modules
            lexical_keys = {result_content_key(cards[hit["id"]]) for hit in lexical}
            new_hybrid = [hit["id"] for hit in hybrid["items"]
                          if result_content_key(cards[hit["id"]]) not in lexical_keys]
            rows.append({
                "group": group, "sample": sample, "query": query, "targets": targets,
                "target_ranks": {method: target_rank(hits, targets) for method, hits in rankings.items()},
                "top_5": {method: [hit["id"] for hit in hits] for method, hits in rankings.items()},
                "new_hybrid_candidates": new_hybrid,
                "hybrid_channels": {hit["id"]: hit["retrieval_channels"] for hit in hybrid["items"]},
            })
        model_cases = [row["model"] for row in fixture["pairs"]] + [row["query"] for row in fixture["boundaries"]]
        model_results = []
        for query in dict.fromkeys(model_cases):
            expected = [hit["id"] for hit in search(root, query, LIMIT, mode="model")]
            response = http(query, "model")
            actual = [hit["id"] for hit in response["items"]]
            model_results.append({"query": query, "expected": expected, "actual": actual,
                                  "passed": actual == expected and response["vector_status"] == "not_requested"})
        frozen = load(root / "reports/MVP_BASELINE.json")["retrieval"]
        auto = {row["query"]: [hit["id"] for hit in http(row["query"], "auto")["items"]] == row["top_5"]
                for row in frozen}
    finally:
        server.shutdown()
        server.server_close()
        thread.join()

    metrics = {}
    for group in ("original", "paraphrase", "all"):
        subset = [row for row in rows if group == "all" or row["group"] == group]
        by_method = {}
        for method in ("sqlite", "chroma", "hybrid"):
            ranks = [row["target_ranks"][method] for row in subset]
            by_method[method] = {"top_1": ranks.count(1),
                                 "top_3": sum(rank is not None and rank <= 3 for rank in ranks),
                                 "top_5": sum(rank is not None for rank in ranks),
                                 "mrr_5": round(sum(1 / rank for rank in ranks if rank) / len(ranks), 4)}
        metrics[group] = {"queries": len(subset), **by_method}
    top_5_channels = Counter("+".join(channels) for row in rows for channels in row["hybrid_channels"].values())
    return {
        "evaluated_at_utc": datetime.now(timezone.utc).isoformat(), "passed": all(auto.values())
        and all(row["passed"] for row in model_results)
        and metrics["all"]["hybrid"]["top_5"] >= metrics["all"]["sqlite"]["top_5"],
        "metrics": metrics, "rows": rows, "hybrid_top_5_channels": dict(top_5_channels),
        "model": model_results, "frozen_auto": auto,
    }


def write_summary(path, result):
    names = {"original": "原功能描述", "paraphrase": "另写的功能描述", "all": "合计"}
    methods = {"sqlite": "SQLite", "chroma": "Chroma", "hybrid": "HTTP 混合"}
    lines = ["# HTTP 混合召回验收", "",
             "在同一组件目录上重新运行原来的功能描述和改写。排名按预先指定的目标卡计算。"
             "SQLite 单路使用原功能模式，Chroma 单路和 HTTP 混合复用同一持久库。", "",
             "| 样本 | 查询数 | 检索 | Top‑1 | Top‑3 | Top‑5 | MRR@5 |",
             "|---|---:|---|---:|---:|---:|---:|"]
    for group, data in result["metrics"].items():
        for method, name in methods.items():
            m = data[method]
            lines.append(f"| {names[group]} | {data['queries']} | {name} | {m['top_1']} | "
                         f"{m['top_3']} | {m['top_5']} | {m['mrr_5']:.4f} |")
    lines.extend(["", "## 目标排名", "", "| 查询 | SQLite | Chroma | HTTP 混合 |",
                  "|---|---:|---:|---:|"])
    for row in result["rows"]:
        ranks = row["target_ranks"]
        lines.append(f"| {row['query']} | {ranks['sqlite'] or '—'} | {ranks['chroma'] or '—'} | {ranks['hybrid'] or '—'} |")
    model = result["model"]
    auto = result["frozen_auto"]
    changed = sum(bool(row["new_hybrid_candidates"]) for row in result["rows"])
    channels = result["hybrid_top_5_channels"]
    lines.extend(["", f"严格型号与定位验证 {sum(row['passed'] for row in model)}/{len(model)} 条和 SQLite 一致。"
                  f"冻结的默认搜索 {sum(auto.values())}/{len(auto)} 条保持完整排名。",
                  "", f"{changed} 条功能查询的混合前五引入了原 SQLite 前五之外的候选。"
                  f"混合前五共 {channels.get('sqlite+chroma', 0)} 个结果来自两路共同命中，"
                  f"{channels.get('chroma', 0)} 个结果仅来自 Chroma。"
                  "样本仅标注指定目标卡，新进入前五的候选尚未逐件标注相关性。"
                  " [完整排名和各路证据](HYBRID_ACCEPTANCE.json)", ""])
    path.write_text("\n".join(lines), encoding="utf-8")


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--root", type=Path, default=ROOT)
    root = parser.parse_args().root.resolve()
    result = evaluate(root)
    write_json(root / "reports/HYBRID_ACCEPTANCE.json", result)
    write_summary(root / "reports/HYBRID_ACCEPTANCE.md", result)
    print(json.dumps(result["metrics"], indent=2))
    return 0 if result["passed"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
