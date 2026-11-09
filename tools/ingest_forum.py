"""Acquire publicly linked RoboMaster samples; never executes downloaded content."""
from __future__ import annotations
import argparse, concurrent.futures, hashlib, html, json, os, re, shutil, stat, sys, time, urllib.error, urllib.parse, urllib.request, zipfile
from datetime import datetime, timezone
from html.parser import HTMLParser
from pathlib import Path, PurePosixPath
from range_fetch import resumable_download

CAD = {".step", ".stp", ".sldasm", ".sldprt", ".slddrw"}
ALLOWED = CAD | {".pdf", ".txt", ".md", ".dwg", ".dxf", ".png", ".jpg", ".jpeg"}
HOSTS = {"bbs.robomaster.com", "bbs-web-static.robomaster.com", "hz-rm-bbs-web-prod.oss-cn-hangzhou.aliyuncs.com"}
def stamp(): return datetime.now(timezone.utc).isoformat()
def write_json(path, value):
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(path.name + ".part")
    temporary.write_text(json.dumps(value, ensure_ascii=False, indent=2), encoding="utf-8")
    temporary.replace(path)
def digest_file(path, algo="sha256"):
    h=hashlib.new(algo)
    with path.open("rb") as f:
        for block in iter(lambda:f.read(4*1024*1024), b""): h.update(block)
    return h.hexdigest()
def encoded_url(url):
    p=urllib.parse.urlsplit(url)
    if p.scheme!="https" or p.hostname not in HOSTS or p.username or p.password:
        raise ValueError("Attachment URL is outside the permitted public source hosts")
    return urllib.parse.urlunsplit((p.scheme,p.netloc,urllib.parse.quote(p.path,safe="/%"),p.query,""))
def request(url, method="GET"):
    return urllib.request.Request(encoded_url(url), method=method, headers={"User-Agent":"RoboMasterCADLibrary/0.1 sample acquisition", "Accept-Encoding":"identity"})
class BodyParser(HTMLParser):
    def __init__(self):
        super().__init__(); self.attachments=[]; self.text=[]
    def handle_starttag(self, tag, attrs):
        a=dict(attrs)
        if tag=="a" and a.get("data-w-e-type")=="attachment":
            self.attachments.append({"name":a.get("data-file-name") or urllib.parse.unquote(urllib.parse.urlsplit(a.get("href","")).path.rsplit("/",1)[-1]),"url":a.get("href",""),"declared_md5":a.get("data-file-md5") or None,"declared_bytes":int(a["data-file-len"]) if a.get("data-file-len","").isdigit() else None})
        if tag in {"p","h1","h2","h3","h4","li","br"}: self.text.append("\n")
    def handle_data(self, value): self.text.append(value)
def parse_page(raw, tid):
    start=raw.find("window.__NUXT__=")
    strings=[]
    if start>=0:
        for m in re.finditer(r'"(?:[^"\\]|\\.)*"', raw[start:]):
            value=m.group()
            if "data-w-e-type" in value or ("\\u003Cp" in value and len(value)>200):
                try: strings.append(json.loads(value))
                except ValueError: pass
    body=max(strings,key=len) if strings else ""
    p=BodyParser(); p.feed(body)
    title_m=re.search(r"<title>(.*?)</title>",raw,re.S)
    title=html.unescape(title_m.group(1)).removesuffix("-RoboMaster 社区") if title_m else str(tid)
    text=re.sub(r"\n[ \t]*\n+", "\n\n", "".join(p.text)).strip()
    unique={a["url"]:a for a in p.attachments if a["url"]}
    return {"source_id":f"bbs-{tid}","thread_id":tid,"url":f"https://bbs.robomaster.com/article/{tid}","title":title,"body_text":text,"attachments":list(unique.values()),"body_extraction":"public_nuxt_serialized_html","authentication_used":False}
def fetch_source(root, seed):
    tid=seed["thread_id"]; folder=root/"raw"/"pages"/str(tid); folder.mkdir(parents=True,exist_ok=True)
    path=folder/"page.html"; raw=None
    for attempt in range(3):
        try:
            if path.exists() and "window.__NUXT__=" in path.read_text(encoding="utf-8"): raw=path.read_text(encoding="utf-8")
            else:
                with urllib.request.urlopen(request(f"https://bbs.robomaster.com/article/{tid}"),timeout=50) as r:
                    if r.status!=200: raise ValueError(f"HTTP {r.status}")
                    raw=r.read(12*1024*1024).decode("utf-8")
            item=parse_page(raw,tid)
            if not item["attachments"]: raise ValueError("Incomplete response or no public attachment anchors; response is not accepted as a valid cached article")
            path.write_text(raw,encoding="utf-8",newline="")
            item["seed"]=seed; item["retrieved_at_utc"]=stamp(); item["page_sha256"]=digest_file(path)
            (folder/"article.txt").write_text(item["body_text"],encoding="utf-8")
            item["body_path"]=str((folder/"article.txt").relative_to(root)).replace("\\","/")
            write_json(folder/"source.json",item)
            print(f"DISCOVERED {tid}: {len(item['attachments'])} public attachments",flush=True)
            return item
        except Exception as e:
            if attempt<2: time.sleep(1)
            else: return {"thread_id":tid,"source_id":f"bbs-{tid}","url":f"https://bbs.robomaster.com/article/{tid}","error":f"{type(e).__name__}: {e}","seed":seed}
def safe_name(name):
    name=Path(name.replace("\\","/")).name
    return re.sub(r'[<>:"/\\|?*\x00-\x1f]',"_",name)[:180] or "attachment"
def download(root, source, attachment, max_bytes):
    tid=source["thread_id"]; folder=root/"raw"/"downloads"/str(tid); folder.mkdir(parents=True,exist_ok=True)
    suffix=Path(attachment["name"]).suffix.lower()
    record={"source_id":source["source_id"],"thread_id":tid,**attachment,"downloaded_at_utc":stamp(),"authentication_used":False}
    if suffix not in {".zip",".7z",".step",".stp",".pdf"}:
        return {**record,"status":"skipped_format"}
    if attachment.get("declared_bytes",0) and attachment["declared_bytes"]>max_bytes:
        return {**record,"status":"skipped_size_limit"}
    path=folder/(hashlib.sha256(attachment["url"].encode()).hexdigest()[:10]+"_"+safe_name(attachment["name"]))
    temporary=path.with_name(path.name+".part")
    for attempt in range(3):
        try:
            if not path.exists():
                resumable_download(attachment["url"],path,expected_size=attachment.get("declared_bytes"),max_bytes=max_bytes,workers=4,chunk_bytes=4*1024**2)
            size=path.stat().st_size
            if attachment.get("declared_bytes") and size!=attachment["declared_bytes"]: raise ValueError("Declared size mismatch")
            md5=digest_file(path,"md5")
            if attachment.get("declared_md5") and md5.lower()!=attachment["declared_md5"].lower(): raise ValueError("Forum MD5 mismatch")
            sha=digest_file(path)
            record.update(status="downloaded",bytes=size,md5=md5,sha256=sha,asset_id=f"bbs-{tid}-{sha[:16]}",path=path.relative_to(root).as_posix(),forum_checksum_verified=bool(attachment.get("declared_md5")),access="anonymous_https")
            print(f"VERIFIED {tid}/{attachment['name']}: {size} bytes",flush=True)
            return record
        except urllib.error.HTTPError as e:
            if e.code in {401,403}: return {**record,"status":"access_denied","http_status":e.code,"error":str(e)}
            error=f"{type(e).__name__}: {e}"
        except Exception as e: error=f"{type(e).__name__}: {e}"
        if temporary.exists(): temporary.unlink()
        if path.exists() and "mismatch" in error.lower():
            quarantine=path.with_name(path.name+".invalid")
            path.replace(quarantine)
        if attempt<2: time.sleep(2)
    return {**record,"status":"download_failed","error":error}
def safe_member_path(dest, name):
    normalized=name.replace("\\","/")
    p=PurePosixPath(normalized)
    if p.is_absolute() or any(x in {"..",""} for x in p.parts) or any(":" in x for x in p.parts):
        raise ValueError(f"Unsafe archive member: {name}")
    target=dest.joinpath(*p.parts).resolve()
    if not target.is_relative_to(dest.resolve()): raise ValueError("Archive path escapes sample root")
    return target
def extract_asset(root, asset):
    source=root/asset["path"]; dest=root/"assets"/str(asset["thread_id"])/asset["sha256"][:16]
    dest.mkdir(parents=True,exist_ok=True); files=[]; skipped=[]
    if source.suffix.lower()==".zip":
        with zipfile.ZipFile(source,metadata_encoding="gbk") as z:
            total=sum(i.file_size for i in z.infolist() if Path(i.filename).suffix.lower() in ALLOWED)
            if total>5*1024**3: raise ValueError("Archive selected contents exceed 5 GiB limit")
            for info in z.infolist():
                target=safe_member_path(dest,info.filename)
                if info.is_dir(): continue
                if stat.S_ISLNK(info.external_attr>>16): raise ValueError("Archive symlink rejected")
                if target.suffix.lower() not in ALLOWED: skipped.append(info.filename);continue
                if info.file_size>1024**3: raise ValueError("Archive member exceeds 1 GiB")
                target.parent.mkdir(parents=True,exist_ok=True)
                if not target.exists() or target.stat().st_size!=info.file_size:
                    with z.open(info) as incoming,target.open("wb") as outgoing: shutil.copyfileobj(incoming,outgoing,1024*1024)
                files.append(target)
    elif source.suffix.lower()==".7z": raise ValueError("7z is recorded but not extracted by this first importer")
    else:
        target=dest/safe_name(asset["name"])
        if not target.exists(): shutil.copy2(source,target)
        files=[target]
    result=[]
    for file in files:
        result.append({"source_id":asset["source_id"],"thread_id":asset["thread_id"],"asset_id":asset["asset_id"],"archive_path":asset["path"],"path":file.relative_to(root).as_posix(),"name":file.name,"extension":file.suffix.lower(),"bytes":file.stat().st_size,"sha256":digest_file(file),"content_type":"cad" if file.suffix.lower() in CAD else "documentation_or_preview"})
    print(f"INVENTORY {asset['thread_id']}/{asset['name']}: {len(result)} files ({len(skipped)} other members excluded)",flush=True)
    return result
def main():
    parser=argparse.ArgumentParser();parser.add_argument("action",choices=["discover","download","inventory"])
    parser.add_argument("--root",type=Path,default=Path(__file__).resolve().parents[1]);parser.add_argument("--threads",type=int,nargs="*")
    parser.add_argument("--max-file-mib",type=int,default=600);parser.add_argument("--workers",type=int,default=2)
    args=parser.parse_args();root=args.root.resolve();catalog=root/"catalog"
    seeds=json.loads((catalog/"source-seeds.json").read_text(encoding="utf-8-sig"))
    if args.threads: seeds=[s for s in seeds if s["thread_id"] in args.threads]
    if args.action=="discover":
        with concurrent.futures.ThreadPoolExecutor(max_workers=args.workers) as ex: sources=list(ex.map(lambda x:fetch_source(root,x),seeds))
        # Retain previous sources during a targeted refresh.
        if (catalog/"sources.json").exists() and args.threads:
            old=json.loads((catalog/"sources.json").read_text(encoding="utf-8")); selected={s["thread_id"] for s in sources}
            sources=[s for s in old if s["thread_id"] not in selected]+sources
        write_json(catalog/"sources.json",sources)
    elif args.action=="download":
        sources=json.loads((catalog/"sources.json").read_text(encoding="utf-8"))
        jobs=[(s,a) for s in sources if not args.threads or s["thread_id"] in args.threads for a in s.get("attachments",[])]
        jobs.sort(key=lambda pair:pair[1].get("declared_bytes") or 32*1024**2)
        selected={s["thread_id"] for s,a in jobs}
        old=json.loads((catalog/"downloads.json").read_text(encoding="utf-8")) if (catalog/"downloads.json").exists() else []
        records=[r for r in old if r["thread_id"] not in selected]
        records.extend({"source_id":s["source_id"],"thread_id":s["thread_id"],**a,"status":"queued"} for s,a in jobs)
        write_json(catalog/"downloads.json",records)
        with concurrent.futures.ThreadPoolExecutor(max_workers=args.workers) as ex:
            futures={ex.submit(download,root,s,a,args.max_file_mib*1024**2):(s,a) for s,a in jobs}
            for future in concurrent.futures.as_completed(futures):
                record=future.result()
                records=[r for r in records if not (r["thread_id"]==record["thread_id"] and r["url"]==record["url"])]+[record]
                write_json(catalog/"downloads.json",records)
    else:
        records=json.loads((catalog/"downloads.json").read_text(encoding="utf-8"));inventory=[];errors=[]
        supplemental=root/"reports"/"supplemental-downloads.json"
        if supplemental.exists():
            known={(r["thread_id"],r["url"]) for r in records}
            records.extend(r for r in json.loads(supplemental.read_text(encoding="utf-8")) if (r["thread_id"],r["url"]) not in known)
        for a in records:
            if a["status"]!="downloaded":continue
            try: inventory.extend(extract_asset(root,a))
            except Exception as e:errors.append({"asset_id":a["asset_id"],"error":f"{type(e).__name__}: {e}"});print("INVENTORY ERROR",a["name"],str(e),flush=True)
        write_json(catalog/"inventory.json",inventory);write_json(catalog/"inventory-errors.json",errors)
if __name__=="__main__":
    sys.stdout.reconfigure(encoding="utf-8")
    main()
