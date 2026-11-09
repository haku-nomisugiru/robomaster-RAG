import sys,json,hashlib,re
from pathlib import Path
root=Path(__file__).resolve().parents[1];sys.path.insert(0,str(root/'tools'))
from ingest_forum import extract_asset,digest_file,write_json
ids=['1883259','1883384','1936175','713291','776744','803685','803691','816172','1935523']
sources=json.loads((root/'catalog/sources.json').read_text(encoding='utf-8'))
downloads=json.loads((root/'catalog/downloads.json').read_text(encoding='utf-8'))
inventory=json.loads((root/'catalog/inventory.json').read_text(encoding='utf-8'))
known={x['path'] for x in inventory}; errors=[]
for tid in ids:
 d=root/'raw/downloads'/tid
 md=d/'说明.md'
 existing=next((s for s in sources if s['source_id']=='bbs-'+tid),None)
 if not existing:
  if not md.exists():continue
  body=md.read_text(encoding='utf-8-sig')
  title=body.splitlines()[0].lstrip('# ').strip()
  team=re.search(r'作者[：:]\s*([^\n]+)',body)
  source={'source_id':'bbs-'+tid,'thread_id':int(tid),'url':'https://bbs.robomaster.com/article/'+tid,'title':title,'page_sha256':digest_file(md),'body_path':md.relative_to(root).as_posix(),'seed':{'team':'待核对','published_date':None,'caveats':['功能划分依据文件名称及开源说明，需工程复核。'],'notice':'以原帖及随附说明的授权为准。'}}
  sources.append(source)
 for f in d.iterdir():
  if not f.is_file() or f.suffix.lower() not in {'.zip','.step','.stp','.sldprt','.sldasm'}:continue
  try:
   sha=digest_file(f); aid='bbs-'+tid+'-'+sha[:16]
   if any(x.get('asset_id')==aid for x in downloads):continue
   asset={'source_id':'bbs-'+tid,'thread_id':int(tid),'name':f.name,'url':'https://bbs.robomaster.com/article/'+tid,'path':f.relative_to(root).as_posix(),'status':'downloaded','asset_id':aid,'sha256':sha,'bytes':f.stat().st_size,'authentication_used':None,'origin':'user_supplied_local_file','forum_checksum_verified':False}
   entries=extract_asset(root,asset)
   inventory.extend(x for x in entries if x['path'] not in known);known.update(x['path'] for x in entries)
   downloads.append(asset)
   print('IMPORTED',tid,f.name,len(entries),flush=True)
  except Exception as e:errors.append({'source_id':'bbs-'+tid,'file':f.name,'error':str(e)});print('ERROR',tid,f.name,str(e),flush=True)
  write_json(root/'catalog/sources.json',sources);write_json(root/'catalog/downloads.json',downloads);write_json(root/'catalog/inventory.json',inventory);write_json(root/'catalog/local-import-errors.json',errors)
print('COMPLETE',len(inventory),'errors',len(errors),flush=True)
