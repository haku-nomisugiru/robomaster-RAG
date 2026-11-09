"""Build the 2025+ open-source robot-mechanism CAD set from RoboMaster BBS public posts.

Policy (fixed for this batch):
  * category = ARTICLE, tag group = 机械 (tags 20-29)
  * approvals (论坛点赞) >= 40
  * createAt >= 2025-01-01  ("2024 之后发布"; 2024 及更早的帖子剔除,
    迁移到新论坛被批量改写日期的 RM2021~RM2023 旧帖按标题里声明的赛季剔除)
  * attachment hosted on the forum's public OSS host (可直接下载), 网盘-only 的帖子剔除
  * 至少一个附件是 CAD/三维几何档 (zip/rar/7z/step/stp/sldprt/sldasm/easm/iges/dwg/dxf/x_t)

Usage: python tools/open_cad_2025.py discover | download | inventory | report | all
"""
from __future__ import annotations

import concurrent.futures
import hashlib
import html
import json
import re
import sys
import time
import urllib.request
import zipfile
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from range_fetch import resumable_download  # noqa: E402

ROOT = Path(__file__).resolve().parents[1]
CAT = ROOT / "catalog"
RAW = ROOT / "raw"
UA = {"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 Chrome/140 Safari/537.36"}

API = "https://bbs.robomaster.com/developers-server/rest/posts/list"
TAG_IDS = [20, 21, 22, 23, 24, 25, 26, 27, 28, 29]
MIN_APPROVALS = 40
AFTER = "2025-01-01"
OSS_HOSTS = {"hz-rm-bbs-web-prod.oss-cn-hangzhou.aliyuncs.com", "bbs-web-static.robomaster.com"}
CAD_EXT = {".step", ".stp", ".sldprt", ".sldasm", ".slddrw", ".easm", ".eprt", ".x_t", ".x_b",
           ".iges", ".igs", ".dwg", ".dxf", ".3dxml", ".obj", ".stl", ".jt"}
ARCHIVE_EXT = {".zip", ".rar", ".7z"}
NON_ROBOT = {"场地图纸", "元器件汇总", "裁判系统、电机电调"}
NON_CAD_FORMAT = {"MJCF", "USD文件", "PCB滑环"}


def now():
    return datetime.now(timezone.utc).isoformat()


def post_json(url, payload):
    req = urllib.request.Request(url, data=json.dumps(payload).encode(),
                                 headers={**UA, "content-type": "application/json"})
    return json.loads(urllib.request.urlopen(req, timeout=60).read().decode())


def http_get(url):
    return urllib.request.urlopen(urllib.request.Request(url, headers=UA), timeout=90).read().decode("utf-8", "ignore")


def list_articles():
    out, page = [], 1
    while True:
        j = post_json(API, {"pageSize": 100, "pageNo": page, "filter": {
            "category": "ARTICLE", "sortByCreateAt": None, "tagIds": TAG_IDS,
            "sortByViews": True, "official": None, "marrow": None}})
        d = j["data"]
        lst, total = (d, None) if isinstance(d, list) else (d.get("list", []), d.get("total"))
        out += lst
        if len(lst) < 100 or (total and len(out) >= total):
            return out
        page += 1


def js_unescape(s):
    try:
        return json.loads('"' + s + '"')
    except ValueError:
        return s.replace("\\u002F", "/").replace("\\/", "/").replace('\\"', '"')


def parse_article(tid, raw):
    title_m = re.search(r"<title>(.*?)</title>", raw, re.S)
    title = html.unescape(title_m.group(1)).replace("-RoboMaster 社区", "").strip() if title_m else str(tid)
    atts = []
    for m in re.finditer(r'data-w-e-type=\\?"attachment', raw):
        seg = raw[m.start(): m.start() + 1600]
        i = seg.find('href=\\"')
        skip = 7
        if i < 0:
            i, skip = seg.find('href="'), 6
        if i < 0:
            continue
        j = seg.find('\\"', i + skip)
        k = seg.find('"', i + skip)
        end = min([x for x in (j, k) if x > 0] or [-1])
        if end < 0:
            continue
        url = html.unescape(js_unescape(seg[i + skip:end]))
        rest = seg[end:]
        name = re.search(r'data-file-name=\\?"((?:\\.|[^"\\])*?)\\?"', rest)
        size = re.search(r'data-file-len=\\?"(\d+)', rest)
        md5 = re.search(r'data-file-md5=\\?"([0-9a-fA-F]{32})', rest)
        atts.append({"url": url, "name": html.unescape(js_unescape(name.group(1))) if name else "",
                     "bytes": int(size.group(1)) if size else None,
                     "md5_declared": (md5.group(1).lower() if md5 else None)})
    body = ""
    start = raw.find("window.__NUXT__=")
    if start >= 0:
        cands = [m.group() for m in re.finditer(r'"(?:[^"\\]|\\.)*"', raw[start:])
                 if "\\u003Cp" in m.group() and len(m.group()) > 200]
        if cands:
            try:
                body = json.loads(max(cands, key=len))
            except ValueError:
                body = ""
    text = re.sub(r"<(p|br|h1|h2|h3|h4|li|div|tr)[^>]*>", "\n", body)
    text = html.unescape(re.sub(r"<[^>]+>", "", text))
    text = re.sub(r"[ \t\u00a0]+", " ", text)
    text = re.sub(r"\n\s*\n+", "\n\n", text).strip()
    return title, text, atts


def discover():
    arts = list_articles()
    print(f"mechanical-group articles: {len(arts)}", flush=True)
    seeds = [a for a in arts if (a.get("approvals") or 0) >= MIN_APPROVALS]
    seeds.sort(key=lambda a: -(a["approvals"]))
    records = []
    for a in seeds:
        tid = a["id"]
        folder = RAW / "pages" / str(tid)
        folder.mkdir(parents=True, exist_ok=True)
        page = folder / "open-cad-page.html"
        for attempt in range(3):
            try:
                raw = page.read_text(encoding="utf-8") if page.exists() and page.stat().st_size > 5000 else http_get(f"https://bbs.robomaster.com/article/{tid}")
                title, text, atts = parse_article(tid, raw)
                if not atts and "pan.baidu" not in text:
                    raise ValueError("no attachment anchors and no netdisk link")
                page.write_text(raw, encoding="utf-8", newline="")
                break
            except Exception as e:
                if attempt == 2:
                    title, text, atts = a["title"], "", []
                    print(f"  !! {tid} {e}", flush=True)
                time.sleep(1.5)
        rec = {
            "thread_id": tid, "url": f"https://bbs.robomaster.com/article/{tid}",
            "title": title, "author": a.get("authorNickname"),
            "approvals": a["approvals"], "views": a["views"], "comments": a["comments"],
            "createAt": a["createAt"], "tags": [t.get("name") for t in (a.get("tags") or [])][:6],
            "body_text": text, "attachments": atts,
            "page_path": f"raw/pages/{tid}/open-cad-page.html",
            "retrieved_at_utc": now(),
        }
        rec["decision"], rec["reason"] = classify(rec)
        records.append(rec)
        print(f"{a['approvals']:4d} {a['createAt'][:10]} att={len(atts)} {rec['decision']:7s} {title[:52]}", flush=True)
        time.sleep(0.3)
    write(CAT / "open-cad-2025-candidates.json", records)
    inc = [r for r in records if r["decision"] == "include"]
    print(f"\nincluded {len(inc)} / {len(records)}")
    return records


def classify(rec):
    title = rec["title"]
    if rec["createAt"][:10] < AFTER:
        return "exclude", "2024 或更早发布"
    if re.search(r"RM202[123]\b|RM202[123]-", title):
        return "exclude", "RM2021~2023 旧帖，迁移日期不代表发布时间"
    if not rec["attachments"]:
        return "exclude", "仅网盘/外链，无直链附件"
    if any(k in title for k in NON_ROBOT):
        return "exclude", "非机器人机械结构（场地/官方元器件）"
    if any(k in title for k in NON_CAD_FORMAT):
        return "exclude", "非 CAD 交换格式（仿真/PCB）"
    exts = {Path(at["name"]).suffix.lower() for at in rec["attachments"]}
    if not (exts & (CAD_EXT | ARCHIVE_EXT)):
        return "exclude", f"附件仅 {'/'.join(sorted(e.lstrip('.') for e in exts if e))}，无 CAD"
    hosts = {urllib_request_host(at["url"]) for at in rec["attachments"]}
    if not hosts <= OSS_HOSTS or "" in hosts:
        return "exclude", f"附件不在可直接下载的公开 OSS：{sorted(hosts)}"
    if any((at["bytes"] or 0) == 0 for at in rec["attachments"]):
        return "exclude", "存在 0 字节失效附件"
    return "include", "直链 CAD 附件"


def urllib_request_host(url):
    m = re.match(r"https://([^/]+)/", url)
    return m.group(1) if m else ""


def write(path, value):
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".part")
    tmp.write_text(json.dumps(value, ensure_ascii=False, indent=1), encoding="utf-8")
    tmp.replace(path)


def safe_name(name):
    name = Path(name.replace("\\", "/")).name
    return re.sub(r'[<>:"/\\|?*\x00-\x1f]', "_", name)[:180] or "attachment"


def md5_file(path, block=8 * 1024**2):
    h = hashlib.md5()
    with path.open("rb") as f:
        for chunk in iter(lambda: f.read(block), b""):
            h.update(chunk)
    return h.hexdigest()


def sha256_file(path, block=8 * 1024**2):
    h = hashlib.sha256()
    with path.open("rb") as f:
        for chunk in iter(lambda: f.read(block), b""):
            h.update(chunk)
    return h.hexdigest()


def download():
    recs = json.loads((CAT / "open-cad-2025-candidates.json").read_text(encoding="utf-8"))
    inc = [r for r in recs if r["decision"] == "include"]
    results = []
    for rec in inc:
        folder = RAW / "downloads" / str(rec["thread_id"])
        folder.mkdir(parents=True, exist_ok=True)
        files = []
        for at in rec["attachments"]:
            dest = folder / safe_name(at["name"])
            entry = {"name": at["name"], "url": at["url"], "declared_bytes": at["bytes"],
                     "declared_md5": at["md5_declared"], "local": str(dest.relative_to(ROOT)).replace("\\", "/")}
            try:
                if dest.exists() and dest.stat().st_size == at["bytes"]:
                    info = {"bytes": dest.stat().st_size, "resumed": True}
                else:
                    info = resumable_download(at["url"], dest, expected_size=at["bytes"],
                                              max_bytes=3 * 1024**3, workers=6)
                actual = dest.stat().st_size
                entry.update({"bytes": actual, "ok": at["bytes"] in (None, actual),
                              "md5": md5_file(dest), "sha256": sha256_file(dest)})
                if at["md5_declared"]:
                    entry["md5_match"] = entry["md5"] == at["md5_declared"]
                print(f"  OK {rec['thread_id']} {actual/1e6:8.1f}MB {at['name'][:60]}", flush=True)
            except Exception as e:
                entry.update({"ok": False, "error": f"{type(e).__name__}: {e}"})
                print(f"  FAIL {rec['thread_id']} {at['name'][:60]} {e}", flush=True)
            files.append(entry)
        rec["files"] = files
        results.append(rec)
    write(CAT / "open-cad-2025.json", results)
    bad = [f for r in results for f in r["files"] if not f.get("ok")]
    print(f"\ndownloaded posts={len(results)} files={sum(len(r['files']) for r in results)} failed={len(bad)}")
    return results


def archive_listing(path: Path):
    if path.suffix.lower() == ".zip":
        with zipfile.ZipFile(path) as z:
            return z.namelist()
    import subprocess
    out = subprocess.run(["bsdtar", "-tf", str(path)], capture_output=True, timeout=1800)
    text = out.stdout.decode("utf-8", "replace")
    if text.count("\ufffd") > 20:
        text = out.stdout.decode("gbk", "replace")
    if not text.strip():
        raise RuntimeError((out.stderr or b"").decode("utf-8", "replace")[:300] or "bsdtar returned no entries")
    return [ln for ln in text.splitlines() if ln.strip()]


def inventory():
    recs = json.loads((CAT / "open-cad-2025.json").read_text(encoding="utf-8"))
    for rec in recs:
        for f in rec["files"]:
            if not f.get("ok"):
                continue
            p = ROOT / f["local"]
            if p.suffix.lower() not in ARCHIVE_EXT:
                continue
            try:
                names = archive_listing(p)
            except Exception as e:
                f["listing_error"] = f"{type(e).__name__}: {e}"
                continue
            (RAW / "pages" / str(rec["thread_id"]) / (p.stem + ".listing.txt")).write_text(
                "\n".join(names), encoding="utf-8")
            cad = [n for n in names if Path(n).suffix.lower() in CAD_EXT]
            f["archive"] = {"entries": len(names), "cad_entries": len(cad),
                            "cad_examples": cad[:8], "listing": f"raw/pages/{rec['thread_id']}/{p.stem}.listing.txt"}
            print(f"{rec['thread_id']} {p.name[:50]}: {len(names)} entries, {len(cad)} CAD")
        rec["has_cad"] = any(f.get("archive", {}).get("cad_entries") for f in rec["files"]) or \
                         any(Path(f["name"]).suffix.lower() in CAD_EXT for f in rec["files"] if f.get("ok"))
    write(CAT / "open-cad-2025.json", recs)
    return recs


def md_note(rec):
    """每个下载目录自带的说明文件：介绍 + 原帖来源 + 校验信息。"""
    lines = [f"# {rec['title']}", "",
             f"- 原帖来源：{rec['url']}",
             f"- 作者：{rec['author'] or '—'}　点赞：{rec['approvals']}　浏览：{rec['views']}　评论：{rec['comments']}",
             f"- 发布时间：{rec['createAt'][:10]}（论坛服务器时间 UTC）",
             f"- 分类标签：{' / '.join(rec['tags'])}", "",
             "## 开源内容介绍（原帖正文节选）", "", rec["body_text"][:1500] or "（正文为空）", "",
             "## 本地文件", ""]
    for f in rec["files"]:
        if not f.get("ok"):
            lines.append(f"- `{f['name']}` — 下载失败：{f.get('error')}")
            continue
        extra = ""
        a = f.get("archive")
        if a:
            extra = f"　压缩包内 {a['entries']} 个条目，其中 CAD/几何件 {a['cad_entries']} 个"
        lines.append(f"- `{f['name']}` — {f['bytes']/1e6:.1f} MB{extra}")
        lines.append(f"  - MD5 `{f['md5']}`")
        lines.append(f"  - 原直链 {f['url']}")
    lines += ["", "> 文件取自 RoboMaster 社区公开帖附件；版权与使用范围以原帖作者的声明为准。"]

    return "\n".join(lines)


def report():
    recs = json.loads((CAT / "open-cad-2025.json").read_text(encoding="utf-8"))
    recs.sort(key=lambda r: -r["approvals"])
    for rec in recs:
        folder = RAW / "downloads" / str(rec["thread_id"])
        if folder.is_dir():
            (folder / "说明.md").write_text(md_note(rec), encoding="utf-8")
    lines = ["# RoboMaster 论坛开源机械结构 CAD（2025 年后发布，点赞 ≥ 40，直链可下）", "",
             f"生成时间：{now()}　样本数：{len(recs)}　总量：{sum(f['bytes'] for r in recs for f in r['files'] if f.get('ok'))/1e9:.2f} GB", "",
             "筛选口径：RoboMaster 社区「文章」+ 机械标签组（步兵/英雄/工程/哨兵/空中机器人/飞镖/雷达/场地/实用工具/其他）；"
             "点赞（approvals）≥ 40；发布时间 ≥ 2025-01-01；附件托管在论坛公开 OSS（可直接下载，非网盘）；至少含一个 CAD/三维几何文件。", "",
             "CAD 列含义：✓ = 附件内含 CAD/三维几何件（压缩包条目已核）；⚠ = 附件只有说明文档/演示视频，无 CAD，仅作对照保留。", "",
             "| # | 点赞 | 发布 | 帖子 | 战队/作者 | CAD | 文件 | 本地目录 |", "|---|---|---|---|---|---|---|---|"]
    for i, r in enumerate(recs, 1):
        size = sum(f["bytes"] for f in r["files"] if f.get("ok")) / 1e6
        files = "<br>".join(f"{html.escape(f['name'])} ({f['bytes']/1e6:.1f}MB)" for f in r["files"] if f.get("ok"))
        has_cad = any(f.get("archive", {}).get("cad_entries") for f in r["files"]) or \
            any(Path(f["name"]).suffix.lower() in CAD_EXT for f in r["files"] if f.get("ok"))
        r["has_cad"] = has_cad
        lines.append(f"| {i} | {r['approvals']} | {r['createAt'][:10]} | [{html.escape(r['title'])}]({r['url']}) | "
                     f"{html.escape(r['author'] or '')} | {'✓' if has_cad else '⚠'} | {files} ({size:.0f}MB) | raw/downloads/{r['thread_id']}/ |")
    lines += ["", "### 备注", "",
              "- 713291 与 1936175 是同一位作者（青岛大学-未来）两次发布的同一套减速箱 STEP，文件逐字节相同，保留两条记录以便按原帖追溯。",
              "- 813002（华农英雄发射器）附件只有调试指南 PDF 和 5 段演示视频，压缩包内无 CAD 件，仅作对照保留，不属于 CAD 模型。",
              "- 761377 的 `HKUST ENTERPRIZE 2025 HERO 8.7.SLDPRT` 是单个 259MB 的总装零件文件，其余 rar 为分模块图纸。",
              "- 761042 是 3 分卷 RAR（part1~3），必须三个分卷齐全才能解压；已全部落地。",
              "- 发布时间为论坛接口返回的 `createAt`（UTC）；2024 年在旧论坛发布、后被批量迁移的 RM2021~RM2023 帖已按标题赛季剔除。", "",
              "## 各帖介绍与来源", ""]
    for i, r in enumerate(recs, 1):
        lines += [f"### {i}. {r['title']}", "",
                  f"- 原帖：{r['url']}",
                  f"- 作者：{r['author']}　点赞：{r['approvals']}　浏览：{r['views']}　评论：{r['comments']}",
                  f"- 发布时间：{r['createAt'][:10]}　标签：{' / '.join(r['tags'])}",
                  f"- 本地：`raw/downloads/{r['thread_id']}/`　证据页：`{r['page_path']}`", "",
                  "介绍（原帖正文节选）：", "", "> " + (r["body_text"][:700].replace("\n", "\n> ") or "（正文为空）"), ""]
        for f in r["files"]:
            if not f.get("ok"):
                continue
            a = f.get("archive")
            extra = f"　压缩包内 {a['entries']} 个条目，{a['cad_entries']} 个 CAD 件" if a else ""
            lines.append(f"- 文件 `{f['local']}`　{f['bytes']/1e6:.1f}MB　MD5 {f['md5']}{extra}")
        lines.append("")
    cands = json.loads((CAT / "open-cad-2025-candidates.json").read_text(encoding="utf-8"))
    excluded = [c for c in cands if c["decision"] != "include"]
    lines += ["", "## 附录：同期点赞 ≥ 40 但被剔除的帖子（供复核）", "",
              "| 点赞 | 发布 | 帖子 | 剔除原因 |", "|---|---|---|---|"]
    for c in sorted(excluded, key=lambda x: -x["approvals"]):
        lines.append(f"| {c['approvals']} | {c['createAt'][:10]} | [{html.escape(c['title'])}]({c['url']}) | {c['reason']} |")
    (ROOT / "reports").mkdir(exist_ok=True)
    (ROOT / "reports" / "OPEN-CAD-2025.md").write_text("\n".join(lines), encoding="utf-8")
    print("report ->", ROOT / "reports" / "OPEN-CAD-2025.md")


if __name__ == "__main__":
    cmd = sys.argv[1] if len(sys.argv) > 1 else "all"
    if cmd in ("discover", "all"):
        discover()
    if cmd in ("download", "all"):
        download()
    if cmd in ("inventory", "all"):
        inventory()
    if cmd in ("report", "all"):
        report()
