"""Run the curated functional vs. exact-model benchmark on the current local index."""

import argparse
from datetime import datetime, timezone
from pathlib import Path

from ingest_forum import write_json
from library import ROOT, load, search


LIMIT = 5


def run_query(root, query, mode, targets=()):
    hits = search(root, query, limit=LIMIT, mode=mode)
    rank = next((i for i, hit in enumerate(hits, 1) if hit["id"] in targets), None)
    return {"query": query, "target_rank": rank, "hits": [
        {**{key: hit[key] for key in ("id", "name", "category", "source_url", "matches", "model_matches")
            if key in hit},
         **({"target_source_facts": (hit["retrieval_profile"] or {}).get("facts", [])}
            if hit["id"] in targets else {})}
        for hit in hits
    ]}


def evaluate(root):
    fixture = load(root / "catalog/retrieval-benchmark.json")
    if fixture["schema_version"] != "1.0":
        raise ValueError("Unsupported retrieval benchmark version")
    pairs = []
    for case in fixture["pairs"]:
        targets = case["targets"]
        function = run_query(root, case["function"], "function", targets)
        model = run_query(root, case["model"], "model", targets)
        auto = run_query(root, case["model"], "auto", targets)
        roles = {item["role"] for hit in model["hits"] if hit["id"] in targets
                 for item in hit["model_matches"]}
        unwanted = set(case.get("reject_categories", []))
        result = {
            **case, "function": function, "model": model, "auto_for_model_query": auto,
            "model_role_passed": case.get("expected_role") in roles if "expected_role" in case else True,
            "strict_rejected_categories": [hit["id"] for hit in model["hits"] if hit["category"] in unwanted],
            "auto_rejected_categories": [hit["id"] for hit in auto["hits"] if hit["category"] in unwanted],
        }
        if "function_refined" in case:
            result["function_refined"] = run_query(root, case["function_refined"], "function", targets)
        pairs.append(result)
    boundaries = []
    for case in fixture["boundaries"]:
        result = run_query(root, case["query"], "model")
        actual = [hit["id"] for hit in result["hits"]]
        boundaries.append({**case, "hits": result["hits"], "passed": set(actual) == set(case["expected"])})
    baseline = {
        row["query"]: [hit["id"] for hit in search(root, row["query"], limit=LIMIT)] == row["top_5"]
        for row in load(root / "reports/MVP_BASELINE.json")["retrieval"]
    }
    metrics = {}
    for mode in ("function", "model", "auto_for_model_query"):
        ranks = [row[mode]["target_rank"] for row in pairs]
        metrics[mode] = {"queries": len(ranks), "top_1": ranks.count(1),
                         "top_5": sum(rank is not None for rank in ranks),
                         "mrr_at_5": round(sum(1 / rank for rank in ranks if rank) / len(ranks), 4)}
    passed = (metrics["function"]["top_5"] == len(pairs) and metrics["model"]["top_1"] == len(pairs)
              and all(row["model_role_passed"] and not row["strict_rejected_categories"] for row in pairs)
              and all(row["passed"] for row in boundaries) and all(baseline.values()))
    return {"schema_version": "1.0", "evaluated_at_utc": datetime.now(timezone.utc).isoformat(),
            "passed": passed, "metrics": metrics, "pairs": pairs,
            "boundaries": boundaries, "frozen_auto_baseline": baseline}


def write_markdown(path, report):
    typed = sum("expected_role" in row for row in report["pairs"])
    located = len(report["pairs"]) - typed
    lines = ["# RoboMaster 功能与型号检索对照", "",
             f"本次评测{'通过' if report['passed'] else '未通过'}。同一对照项共用预先指定的目标卡，"
             f"全库检索，每次最多读取 5 条。对照输入包含 {typed} 条型号查询与 {located} 条精确定位查询。"
             "Top‑1 和 Top‑5 衡量目标卡是否出现。MRR@5 是首个目标排名倒数的平均值，未命中计零。", "",
             "| 入口 | 查询数 | Top‑1 | Top‑5 | MRR@5 |", "|---|---:|---:|---:|---:|"]
    for key, label in (("function", "功能描述"), ("model", "严格型号或精确定位"),
                       ("auto_for_model_query", "同一型号或定位输入走默认搜索")):
        metric = report["metrics"][key]
        lines.append(f"| {label} | {metric['queries']} | {metric['top_1']} | {metric['top_5']} | {metric['mrr_at_5']:.4f} |")
    lines.extend(["", "## 成对查询", "",
                  "| 样本 | 功能输入 | 目标排名 | 型号或定位输入 | 类型 | 目标排名 |",
                  "|---|---|---:|---|---|---:|"])
    for row in report["pairs"]:
        f, m = row["function"], row["model"]
        lines.append(f"| {row['label']} | {f['query']} | {f['target_rank'] or '未进入前五'} |"
                     f" `{m['query']}` | {row['locator_kind']} | {m['target_rank'] or '未进入前五'} |")
    lines.extend(["", "## 排序与粒度", ""])
    for row in report["pairs"]:
        if refined := row.get("function_refined"):
            function = row["function"]
            first = function["hits"][0]["name"] if function["hits"] else "无"
            lines.append(f"- 「{function['query']}」首条为「{first}」，目标排名 {function['target_rank'] or '前五外'}。"
                         f"改用「{refined['query']}」后，目标排名 {refined['target_rank'] or '前五外'}。")
        if row["auto_rejected_categories"]:
            categories = "、".join(f"`{category}`" for category in row["reject_categories"])
            lines.append(f"- `{row['model']['query']}` 的默认搜索有 {len(row['auto_rejected_categories'])} 张 {categories} 卡片进入前五，"
                         f"严格型号模式有 {len(row['strict_rejected_categories'])} 张。")
        if not row["auto_for_model_query"]["target_rank"] and row["model"]["target_rank"]:
            lines.append(f"- `{row['model']['query']}` 用默认搜索时，目标卡未进入前五。"
                         f"按{row['locator_kind']}查找时目标排名 {row['model']['target_rank']}。")
    lines.extend(["", "## 型号边界", "",
                  "| 查询 | 验证项 | 命中数 | 结果 |", "|---|---|---:|---|"])
    for row in report["boundaries"]:
        lines.append(f"| `{row['query']}` | {row['note']} | {len(row['hits'])} | {'通过' if row['passed'] else '失败'} |")
    baseline = report["frozen_auto_baseline"]
    lines.extend(["", f"冻结的默认查询中，{sum(baseline.values())}/{len(baseline)} 条保留了完整的 Top‑5 排序。"
                  "本报告衡量所列样本的目标卡召回。正式型号覆盖随来源证据与 CAD 命名累积，功能查询的其他结果未逐条标注相关性。"
                  " [完整排名与证据](RETRIEVAL_COMPARISON.json)", ""])
    path.write_text("\n".join(lines), encoding="utf-8")


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--root", type=Path, default=ROOT)
    root = parser.parse_args().root.resolve()
    report = evaluate(root)
    write_json(root / "reports/RETRIEVAL_COMPARISON.json", report)
    write_markdown(root / "reports/RETRIEVAL_COMPARISON.md", report)
    for mode, metrics in report["metrics"].items():
        print(mode, metrics)
    print("PASS" if report["passed"] else "FAIL")
    return 0 if report["passed"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
