import json,sys
from pathlib import Path
r=Path(__file__).resolve().parents[1];inv=json.loads((r/'catalog/inventory.json').read_text(encoding='utf-8'))
spec=[
('1883259','底盘总装.SLDASM','工程机器人底盘总装','chassis'),('1883259','悬挂总装.SLDASM','工程机器人悬挂总装','suspension'),('1883259','大臂总装.SLDASM','工程机器人机械臂大臂总装','robotic_arm'),('1883259','15°侧取抬升模块.SLDASM','工程机器人侧取抬升模块','lifting'),('1883259','直取抬升模块.SLDASM','工程机器人直取抬升模块','lifting'),
('1883384','26联盟赛哨兵底盘.SLDASM','2026哨兵底盘','chassis'),('1883384','舵步底盘.SLDASM','舵步底盘','chassis'),('1883384','26联盟赛哨兵鹅颈云台.SLDASM','2026哨兵鹅颈云台','gimbal'),
('776744','底盘总装.SLDASM','串联腿步兵底盘总装','chassis'),('776744','串联腿装配方案2.SLDASM','串联腿装配方案2','wheel_leg'),('776744','yaw轴装配.SLDASM','步兵云台Yaw轴装配','gimbal'),('776744','pitch装配.SLDASM','步兵云台Pitch轴装配','gimbal'),
('803685','chassis_v2.2025.10.1.SLDASM','工程机器人底盘V2','chassis'),('803685','3508_3508_Swerve_drive_v2.2025.10.1.SLDASM','3508双电机舵轮模块','swerve'),('803685','L4.2025.10.1.SLDASM','机械臂L4关节装配','robotic_arm'),('803685','L6_v2.2025.10.1.SLDASM','机械臂L6关节装配','robotic_arm'),
('1936175','二六式轻型P15.8减速箱改三乙.step','P15.8轻型减速箱改三乙','gearbox'),('1936175','二六式重型P15.8减速箱改五丙.step','P15.8重型减速箱改五丙','gearbox'),
('816172','银矿抓取.SLDASM','工程机器人抓取机构','gripper')]
picks=[]
for sid,name,title,category in spec:
 files=[x for x in inv if x['source_id']=='bbs-'+sid and x['name']==name]
 if not files: print('MISSING',sid,name);continue
 f=files[0]
 picks.append({'id':'rm-'+sid+'-'+str(len(picks)+1).zfill(3),'name':title,'source_id':f['source_id'],'entry_path':f['path'],'category':category,'summary':'来自原帖模型包的'+title+'，按文件名和所在模型包识别，接口、配置及运动关系待检查。','source_features':[],'limitations':['功能边界为基于文件名的初步分类，需在装配上下文中复核。']})
path=next(x['path'] for x in inv if x['source_id']=='bbs-1935523' and x['name']=='步兵悬挂.step')
picks.append({'id':'rm-1935523-001','name':'全向轮悬挂总成','source_id':'bbs-1935523','entry_path':path,'category':'suspension','summary':'全向轮悬挂机构；STEP中可追溯上下臂、轮组及避震子装配。','source_features':['作者说明采用上下臂限位及避震布局，报告辊子抖动问题。'],'limitations':[]})
for did,name,cat in [('0:1:1:2','90避震器子装配','suspension'),('0:1:1:7','悬挂连接子装配','suspension'),('0:1:1:14','悬挂上臂子装配','suspension'),('0:1:1:18','全向轮轮组子装配','wheel'),('0:1:1:19','轮毂子装配','wheel'),('0:1:1:34','悬挂下臂子装配','suspension')]:
 picks.append({'id':'rm-1935523-'+str(len(picks)+1).zfill(3),'name':name,'source_id':'bbs-1935523','entry_path':path,'scope':'definition','definition_id':did,'category':cat,'summary':'来源悬挂STEP中名称可识别的'+name+'；保留原文件与定义定位。','source_features':[],'limitations':['该子装配从原STEP中定位，原生配合和独立依赖未验证。']})
(r/'catalog/module-selections.json').write_text(json.dumps(picks,ensure_ascii=False,indent=2),encoding='utf-8')
print('selected',len(picks))
