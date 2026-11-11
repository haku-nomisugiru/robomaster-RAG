"""Validate evidence-linked mechanism searches against the local HTTP API."""

import argparse
from collections import Counter
from datetime import datetime, timezone
import json
from pathlib import Path
from threading import Thread
from urllib.parse import urlencode
from urllib.request import urlopen

from http_service import make_server
from ingest_forum import write_json
from library import MECHANISM_CATEGORIES, ROOT, load, search


def rank(hits, targets):
    return next((i for i, hit in enumerate(hits, 1) if hit["id"] in targets), None)


def evaluate(root):
    fixture = load(root / "catalog/mechanism-benchmark.json")
    server = make_server(root, 0)
    thread = Thread(target=server.serve_forever, daemon=True)
    thread.start()
    rows = []
    groups = {key: Counter() for key in MECHANISM_CATEGORIES}
    for card in server.catalog.modules.values():
        profile = card.get("mechanism_profile", {})
        for key in profile.get("categories", []):
            groups[key].update(cards=1, source_reviewed=int(profile["review_state"] == "source_reviewed"),
                               has_advantage=int(any(c["kind"] == "advantage" for c in profile["claims"])),
                               observed_children=int(bool(profile["assembly_contexts"])))
    try:
        for row in fixture:
            query, category, targets = row["query"], row["category"], row["targets"]
            params = {"q": query, "category": category, "mode": "function", "limit": 5}
            endpoint = f"http://127.0.0.1:{server.server_port}/v1/search"
            with urlopen(f"{endpoint}?{urlencode(params)}", timeout=30) as response:
                result = json.load(response)
            params.pop("category")
            with urlopen(f"{endpoint}?{urlencode(params)}", timeout=30) as response:
                unfiltered = json.load(response)
            hits = result["items"]
            target_rank = rank(hits, targets)
            if result["retrieval"] != "hybrid" or any(category not in hit["mechanism_profile"]["categories"]
                                                     for hit in hits):
                raise RuntimeError(f"Category or hybrid retrieval failed: {query}")
            chosen = hits[target_rank - 1] if target_rank is not None else None
            rows.append({
                **row,
                "baseline_rank": rank(search(root, query, category=category), targets),
                "feature_sqlite_rank": rank(search(root, query, mode="function", category=category), targets),
                "unfiltered_rank": rank(unfiltered["items"], targets),
                "rank": target_rank, "hits": [{"id": hit["id"], "name": hit["name"]} for hit in hits],
                "target_claims": chosen["mechanism_profile"]["claims"] if chosen else [],
                "cad_source": chosen["cad_source"] if chosen else None,
            })
    finally:
        server.shutdown()
        server.server_close()
        thread.join()
    return {"checked_at_utc": datetime.now(timezone.utc).isoformat(), "groups": groups,
            "metrics": {field: {"top_1": sum(row[field] == 1 for row in rows),
                                "top_5": sum(row[field] is not None for row in rows)}
                        for field in ("baseline_rank", "feature_sqlite_rank", "unfiltered_rank", "rank")},
            "passed": all(row["rank"] and row["target_claims"] for row in rows), "queries": rows}


def write_summary(path, result):
    lines = ["# 机械组件特征检索验收", "",
             "按组件卡统计。跨类别的机构可在多个类别出现。作者优势、CAD 定位和装配实例分别记录。", "",
             "| 类别 | 可定位卡片 | 已核对作者资料 | 有优势证据 | 有直接子项观测 |",
             "|---|---:|---:|---:|---:|"]
    for key, label in MECHANISM_CATEGORIES.items():
        item = result["groups"][key]
        lines.append(f"| {label} | {item['cards']} | {item['source_reviewed']} | "
                     f"{item['has_advantage']} | {item['observed_children']} |")
    lines += ["", "## 功能描述对照", "",
              "| 查询 | 原词法同类筛选 | 特征词法同类筛选 | 混合全库 | 混合同类筛选 | 首条卡片 |",
              "|---|---:|---:|---:|---:|---|"]
    for row in result["queries"]:
        first = row["hits"][0]["name"] if row["hits"] else "—"
        lines.append(f"| {row['query']} | {row['baseline_rank'] or '—'} | {row['feature_sqlite_rank'] or '—'} | {row['unfiltered_rank'] or '—'} | "
                     f"{row['rank'] or '—'} | {first} |")
    lines.extend(["", "查询以预先指定的候选卡为目标。作者描述按对应版本和局部作用解释。实物承载、耐久和装配兼容性仍需独立验证。"
                  " [完整命中、原始 CAD 入口和证据](MECHANISM_ACCEPTANCE.json)", ""])
    path.write_text("\n".join(lines), encoding="utf-8")


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--root", type=Path, default=ROOT)
    root = parser.parse_args().root.resolve()
    result = evaluate(root)
    write_json(root / "reports/MECHANISM_ACCEPTANCE.json", result)
    write_summary(root / "reports/MECHANISM_ACCEPTANCE.md", result)
    print(json.dumps({"passed": result["passed"], "metrics": result["metrics"]}, indent=2))
    return 0 if result["passed"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
