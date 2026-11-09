"""Install pinned headless CAD reader from hash-checked official wheels."""
import hashlib,json,subprocess,sys,urllib.request
from pathlib import Path
from range_fetch import resumable_download
ROOT=Path(__file__).resolve().parents[1]
wheelhouse=ROOT/".wheelhouse";wheelhouse.mkdir(exist_ok=True)
wheels=[];manifest=[]
for project in ["cadquery-ocp-novtk","cadquery-ocp-proxy"]:
    with urllib.request.urlopen(f"https://pypi.org/pypi/{project}/7.9.3.1.1/json",timeout=40) as r:meta=json.load(r)
    tag=f"cp{sys.version_info.major}{sys.version_info.minor}-cp{sys.version_info.major}{sys.version_info.minor}-win_amd64.whl"
    candidates=[f for f in meta["urls"] if f["filename"].endswith(tag) or f["filename"].endswith("py3-none-any.whl")]
    if len(candidates)!=1:raise RuntimeError("Cannot select unambiguous wheel for "+project)
    f=candidates[0];path=wheelhouse/f["filename"];expected=f["digests"]["sha256"]
    def filehash(p):
        h=hashlib.sha256()
        with p.open("rb") as s:
            for block in iter(lambda:s.read(4*1024*1024),b""):h.update(block)
        return h.hexdigest()
    if not path.exists() or filehash(path)!=expected:
        resumable_download(f["url"],path,expected_size=f["size"],max_bytes=80*1024**2,workers=6,chunk_bytes=2*1024**2)
    if filehash(path)!=expected:raise RuntimeError("Official PyPI SHA256 mismatch")
    wheels.append(str(path));manifest.append({"project":project,"version":"7.9.3.1.1","filename":f["filename"],"url":f["url"],"sha256":expected,"bytes":f["size"]})
(wheelhouse/"manifest.json").write_text(json.dumps(manifest,indent=2),encoding="utf-8")
subprocess.run([str(ROOT/".venv"/"Scripts"/"python.exe"),"-m","pip","install","--no-index",*wheels],check=True)
print("Pinned headless CAD dependencies installed and SHA256-verified.",flush=True)
