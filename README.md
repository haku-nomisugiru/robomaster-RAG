# RoboMaster CAD RAG

按功能、精确型号、结构化规格和参考 CAD 检索 RoboMaster 开源机构。每张卡保留可定位的原帖、CAD 文件和组件定义，并记录配置、接口量测及复用前要检查的条件。

## 从干净检出启动

准备 Python 3.12+ 和支持 FTS5 的 SQLite。在仓库根目录执行以下命令。Windows 可直接双击 `start-rag.cmd`。

~~~powershell
python -B tools/restore_catalog.py
python -B tools/http_service.py --port 8765
~~~

恢复脚本从带 SHA-256 的快照初始化 SQLite 文本索引和几何索引，保留已经存在的本地数据库。访问 [健康检查](http://127.0.0.1:8765/health)，当前快照应返回 3,468 张卡片。SQLite 型号、功能检索和复用证据可直接查询。几何相似检索会校验原始 CAD 和用于量测的 STEP 文件，需先按卡片记录补齐这些文件。混合召回按下文安装 Chroma、准备固定版本的编码器并生成本地向量。

~~~powershell
python -B tools/library.py search '3508 舵轮' --mode model
python -B examples/reuse_cad.py --query '3508 舵轮' --mode model
~~~

[旧版 v0.2.0 启动包](https://github.com/haku-nomisugiru/robotmaster-RAG/releases/tag/v0.2.0)对应早期的 26 张卡片基线。

## 当前交付

当前快照包含 3,468 张组件卡，覆盖 2,617 个 CAD 文件入口和 851 张 STEP 定义卡。接口、SQLite/Chroma 混合召回、参考 CAD 检索及 HTTP 复用的对照结果保存在 `reports/`。首批样本的历史基线见 [第一阶段交付记录](reports/PHASE1_HANDOFF.md)。

## 使用

安装依赖后，以本目录为根运行：

~~~powershell
.venv\Scripts\python.exe tools\ingest_forum.py discover
.venv\Scripts\python.exe tools\ingest_forum.py download
.venv\Scripts\python.exe tools\ingest_forum.py inventory
.venv\Scripts\python.exe tools\inspect_step.py --root . --inventory catalog\inventory.json --output reports\step-inspection.jsonl
.venv\Scripts\python.exe tools\library.py build
.venv\Scripts\python.exe tools\library.py validate
.venv\Scripts\python.exe tools\library.py search "滑轨悬挂底盘"
~~~

下载可用 --threads 指定原帖编号；已验证原始文件可以复用。数据源仅为 RoboMaster 论坛原帖中公开提供的附件，不读取账号、浏览器Cookie或用户私有工程。

## 数据

- catalog/source-seeds.json：首批来源与摘要；待确认日期保持null。
- catalog/sources.json：真实原帖与附件发现记录、快照哈希、作者说明。
- catalog/downloads.json：每个附件的公开URL、实际大小、MD5、SHA-256、下载状态。
- catalog/inventory.json：解包后CAD/说明的相对路径、类型、大小、内容指纹。
- catalog/module-selections.json：经来源与文件清单核对的模块选取及其功能边界。
- catalog/retrieval-notes.json：逐机构整理的原帖与附件事实、检索别名、结构关键词、复用前核验项。来源证据保存本地路径、正文或文件指纹、章节或页码。
- catalog/modules.jsonl：HTTP 服务和 AI 工具使用的机构卡片。
- catalog/library.sqlite3：按名称、关键词和描述分字段加权的本地 FTS5 索引。
- reports/step-inspection.jsonl：STEP的定义、实例、变换与静态几何读取结果。
- reports/SAMPLE_CATALOG.md：可阅读的样本目录。
- raw、assets：原帖快照与真实CAD，仅作为队内工作数据，不默认发布到开源仓库。

可直接浏览 [面向检索的机构卡片](reports/SAMPLE_CATALOG.md#面向检索的机构卡片)。搜索示例包括「MGN7 滑轨 气弹簧」「麦轮 纵臂 双避震」「P15.8 Hexroll 平替」「低成本聚氨酯辊子 铜套」。新增或更新资料时，先核对事实适用的组件和版本，再修改对应卡片的检索记录并运行 `build` 和 `validate`。原帖与 PDF 中的作者声明、CAD 文件名归类及 STEP 解析证据在卡片中分别记录。

## 本地组件全量索引

~~~powershell
.venv\Scripts\python.exe -B tools\import_all_local.py
.venv\Scripts\python.exe -B tools\inspect_pending.py
.venv\Scripts\python.exe -B tools\library.py build
.venv\Scripts\python.exe -B tools\library.py validate
~~~

- 导入使用现有本地文件和来源快照。ZIP 用 Python 读取，RAR 和分卷 RAR 使用本机 UnRAR，也可用 `--unrar` 指定路径。附件及处理结果见 [本地覆盖清单](reports/local-coverage.json)。索引面向通用机械零件与机构，专用发射、供弹、制导部件及相应整机包不在检索范围内。
- 旧人工卡 ID 继续可查。新增卡以包内 CAD 文件或 STEP 定义标签定位。STEP 的重复安装实例留在同一张定义卡，卡片给出数量、部分路径示例和完整解析报告的位置。大 STEP 可先只采集层级与位姿。
- 原生文件级卡保留精确路径、哈希和包归属。若有保存引用快照，卡片列出候选组件和匹配状态。功能分类由名称或定义标签推断的卡片标为 `component`，已有作者证据并经人工整理的卡片单独标记。
- [索引汇总](reports/library-summary.json) 列出文件覆盖、STEP 拆卡和原生引用状态。内容相同的零件或 STEP 定义在搜索结果中合并显示，各自的路径卡仍保留。原生装配按文件位置区分依赖上下文。

MVP 样本的 CAD 指纹、卡片 ID 和查询排名见 [冻结基线](reports/MVP_BASELINE.json)。活动配置的实例、文件状态与位姿见 [原生装配快照](reports/native-occurrences.jsonl)，对应的 STEP 定义和实例见 [STEP 解析报告](reports/step-inspection.jsonl)。

## 组件目录和接口证据

组件目录复用现有 SQLite 和原生、STEP 解析报告。卡片的 `component_id` 表示资产内的文件路径与可选 STEP 定义标签，同一位置的内容版本由 SHA-256 区分。同一零件多次安装时，`component_occurrences` 分别记录实例路径、父节点和配置。只有证据支持的实例才关联到目标组件。当前原生实例按已观察的活动配置或分支登记。

- `components` 与 `component_cards` 分别保存组件和卡片的关联。原帖及资产归属可沿 `source_id`、`asset_id` 查询。
- [接口选面清单](catalog/interface-selections.json) 绑定目标文件、版本、配置及选面签名。[接口证据](catalog/interface-evidence/) 每面一条，记录局部基准、法向、圆形开口直径与坐标、中心距、包络和证据来源。
- `interface_evidence` 保留有效和过期的记录，`interface_openings` 只收有效记录的数值。组件卡给出本件的 `interfaces.measurements`，装配卡给出 `interfaces.component_measurements` 和实例定位。
- 首批量测覆盖 3508 舵轮与车架的对接位置，以及步兵悬挂连接板一侧。装配位姿对照、实测结果和检索回归见 [接口验收记录](reports/INTERFACE_ACCEPTANCE.json)。

组件身份、装配实例和接口量测分表存放。别名、数值规格和源文件入口分别由 `component_aliases`、`component_specs`、`cad_source` 返回。别名保留来源类型，区分人工整理、卡片标题、原文件名、去日期的文件名和 STEP 定义名称。CAD 来源保留原帖和快照哈希、归档和包内路径、CAD 哈希、STEP 定义或实例标签。 [PartCAD 接口定义](https://partcad.readthedocs.io/en/latest/config_interfaces.html) · [step.parts 目录结构](https://github.com/earthtojake/step.parts/blob/main/DEVELOPMENT.md)

~~~powershell
.venv\Scripts\python.exe -B tools\library.py search --alias '舵下滑轨悬挂'
.venv\Scripts\python.exe -B tools\library.py search --spec 'opening_diameter_mm:6' --spec 'opening_count:9' --spec 'opening_center_distance_mm:54.99:55.01'
.venv\Scripts\python.exe -B tools\library.py search --spec 'bbox_x_mm::100'
~~~

数值规格支持 `键:值` 或 `键:最小:最大`，空边界表示该侧不限。包围盒键为 `bbox_x_mm`、`bbox_y_mm`、`bbox_z_mm`。接口键为 `opening_diameter_mm`、`opening_count`、`opening_center_distance_mm`、`mounting_face_area_mm2`。多个包围盒条件在同一坐标范围或配置内匹配，多个接口条件在同一实测面匹配。中心距可来自该面的任意两个已选开口。每条规格带单位、量测范围和证据；缺少量测的组件仍可用别名和文件入口检索。

新增公开附件时，把原始 CAD 或压缩包放到 `raw/downloads/<原帖ID>/`，为新版本使用独立附件名并保留原帖来源记录。运行上面的导入、待处理解析、构建和验证四步。导入按附件哈希复用已入库资产，STEP 解析器只处理缺少有效报告的文件。新零件可先入库，接口量测可按实际复用需要补录。

补录接口时，直接使用原始 STEP，或在 SolidWorks 中按确认的配置生成独立 STEP 量测副本。使用 `measure_interfaces.py --scan "STEP 文件路径"` 列出带圆形开口的平面。用 `--definition` 指定装配内的 STEP 定义，用 `--face` 展开对应面的开口序号。复制选面清单中的相应结构，填写源文件指纹、配置、选面签名和开口序号，然后按证据 ID 单独生成。

~~~powershell
.venv\Scripts\python.exe -B tools\measure_interfaces.py --id '证据ID'
.venv\Scripts\python.exe -B tools\library.py build
.venv\Scripts\python.exe -B tools\library.py validate
~~~

同一位置发生换版时，重建会根据目标和量测输入的哈希重新核对证据。过期记录留作追溯，旧孔径和孔距退出可用结果。装配引用变化时，仅对应的装配关联失效，零件本身的量测可继续使用。重新选面量测时可以新建证据 ID 保留历史。已有人工定位的 STEP 换版时，应先重跑解析并核对定义标签与选卡。服务已启动时，按 [HTTP 服务说明](reports/HTTP_SERVICE.md) 重启以加载新卡片。

## 功能与型号检索

~~~powershell
.venv\Scripts\python.exe -B tools\library.py search '行走和转向分别用独立驱动' --mode function
.venv\Scripts\python.exe -B tools\library.py search '3508 舵轮' --mode model
.venv\Scripts\python.exe -B tools\library.py search 'P15.8 双轴承支撑' --mode model
.venv\Scripts\python.exe -B tools\evaluate_retrieval.py
~~~

`function` 用名称、功能词和来源事实排序。需求涉及下臂、轮组或减速箱时，可明确写上所需的总成或子件。已知型号时使用 `model`，型号字符串精确匹配，剩余用途词用于筛选和排序。结果的 `model_matches` 区分整机型号、系列型号、所用部件、作者对照产品和 CAD 文件名，并附上来源定位。默认 `auto` 沿用原有检索。

新导入的 CAD 文件可按完整文件名、文件主名或卡片 ID 精确定位。需要按正式型号或所用部件查找时，在来源标注的 `model_identifiers` 下登记并关联作者证据，再运行 `build` 和 `validate`。STEP 子组件可用卡片 ID 精确定位。 [来源标注](catalog/retrieval-notes.json)

[功能与型号对照报告](reports/RETRIEVAL_COMPARISON.md) 按同一目标对比两种输入。可复跑用例在 [检索评测清单](catalog/retrieval-benchmark.json)，完整排名和证据在 [机器可读结果](reports/RETRIEVAL_COMPARISON.json)。

## 按机械组件特征检索

功能检索可按底盘 `chassis`、悬挂 `suspension`、串联腿与轮腿 `wheel_leg`、舵轮 `swerve`、机械臂 `robotic_arm`、抓取机构 `gripper` 和减速箱 `gearbox` 筛选。跨机构卡可同时属于多个类别，例如带舵下悬挂的舵轮和带吸盘末端的线性抬升装配。分类沿用拆卡的原始身份，精确型号仍走现有型号索引。

~~~powershell
.venv\Scripts\python.exe -B tools\library.py search '转动时气管和电线分别通过转接头' --mode function --category robotic_arm
.venv\Scripts\python.exe -B examples\search_models.py '柔软吸盘贴合，托盘防止工件摆动' --mode function --category gripper
.venv\Scripts\python.exe -B tools\evaluate_mechanisms.py
~~~

返回卡片的 `mechanism_profile.claims` 区分结构、原理、优势、取舍和适用场景，每条保留来源页码或章节。`scope=source_family` 表示作者描述的是设计系列或上一级机构，复用前应核对目标 CAD 的版本和边界。`cad_observations` 与 `assembly_contexts` 分别记录 CAD 的定位、可核实量测，以及报告中观察到的直接子项和配置。子项数按该次装配观察统计。只从原生文件保存引用中定位的零件在 `saved_reference_candidates` 单独列为候选。

新增来源声明时在特征清单登记卡片 ID、CAD SHA‑256 和证据，再重建 SQLite 并同步 Chroma。CAD 换版后，原版本的人工特征退出检索，当前 CAD 的可定位事实仍由构建过程生成。 [特征来源清单](catalog/mechanism-features.json) · [七类检索验收](reports/MECHANISM_ACCEPTANCE.md)

## 本地 Chroma 文本向量

FastEmbed 在本机 CPU 上用固定版本的 BGE 小型中文模型编码卡片文本。Chroma 用 `PersistentClient` 把外部向量和卡片 ID 写入 `runtime/chroma/`，查询时按 ID 回到当前 SQLite 卡片和原 CAD 定位。代码显式传入文档向量和查询向量。 [Chroma 写入说明](https://docs.trychroma.com/docs/collections/add-data) · [固定模型版本](https://huggingface.co/Qdrant/bge-small-zh-v1.5/tree/46fbe35fd4374a00fee7de77dfddaeb6dd6a2c59)

~~~powershell
.venv\Scripts\python.exe -m pip install -r requirements-vector.txt
.venv\Scripts\python.exe -B tools\vector_search.py prepare
.venv\Scripts\python.exe -B tools\vector_search.py sync
.venv\Scripts\python.exe -B tools\vector_search.py search '把轮组接到悬挂的机加工法兰' --limit 5
.venv\Scripts\python.exe -B tools\evaluate_vector.py
~~~

首次部署时 `prepare` 下载并校验模型权重。新文件入库或卡片内容变化后，先停止 HTTP 服务，再重建 SQLite、运行 `sync` 并重新启动服务。同步按卡片内容与 CAD 版本指纹更新有变化的向量，移除已离开目录的派生向量。HTTP 的 `mode=function` 合并 SQLite 和 Chroma 候选，`mode=model` 走精确型号索引。运行 `tools/evaluate_hybrid.py` 可复跑 HTTP 对照。 [重开与对照验收](reports/VECTOR_ACCEPTANCE.md) · [混合召回验收](reports/HYBRID_ACCEPTANCE.md)

## 参考 CAD 相似检索

OCP 以毫米读取原始 STEP 或带配置记录的 SolidWorks STEP 导出。几何索引记录包围盒、表面积、封闭体积、主惯性矩比例、面类型和实体数。它按 STEP 内容指纹和定义标签合并重复来源，结果保留所有副本的卡片 ID、原 CAD 入口和几何计算所用文件。装配体积按组成实体计算。 [OCP 几何属性文档](https://occt3d.com/dev/doc/refman/html/class_b_rep_g_prop.html)

~~~powershell
.venv\Scripts\python.exe -m pip install -r requirements-geometry.txt
.venv\Scripts\python.exe -B tools\geometry_search.py build
.venv\Scripts\python.exe -B tools\geometry_search.py search --card rm-1935523-cc08defa62bc17d10
.venv\Scripts\python.exe -B tools\geometry_search.py search --file 'D:\模型\参考.step' --mode shape
.venv\Scripts\python.exe -B tools\geometry_search.py search --file 'D:\模型\参考.step' --sample-points 512
.venv\Scripts\python.exe -B tools\geometry_search.py preview --card rm-1935523-cc08defa62bc17d10 --output reports\geometry-examples\suspension-plate.glb --points reports\geometry-examples\suspension-plate.ply
.venv\Scripts\python.exe -B tools\evaluate_geometry.py
~~~

`size` 使用实际毫米尺寸，`shape` 忽略整体等比缩放。默认在同类几何对象内查找，`--kind` 和 `--category` 可以缩小范围。使用卡片检索时默认略过同一 STEP 定义的副本，需核对原件时加 `--include-reference`。参考 STEP 可用 `--definition` 指定 XCAF 子定义。相似距离越小，描述量越接近。

默认排序以 B-Rep 属性为依据。`--sample-points` 会用 OCP 生成三角网格，再用 trimesh 按面积采样并重排候选前列。`preview` 生成以米为单位的 GLB，可选生成以毫米为单位的 PLY 点云。原生 SolidWorks 参考文件可先导出所需配置为 STEP。新增文件完成主目录构建后，再运行几何索引的 `build` 复用原有几何描述量。 [trimesh 表面采样文档](https://trimesh.org/trimesh.sample.html) · [几何索引覆盖](reports/GEOMETRY_INDEX.json) · [参考 CAD 验收](reports/GEOMETRY_ACCEPTANCE.json)

## 证据与能力边界

AI 可通过现有 HTTP 检索选择候选，再读取 `reuse_href` 获取 CAD 定义、配置、接口证据和可下载的原文件。客户端在校验文件后保留原始 CAD 入口和待核验项。 [AI 复用流程与命令](reports/HTTP_SERVICE.md#ai调用) · [端到端验收](reports/AI_REUSE_ACCEPTANCE.json)

1. 原帖中的性能、质量、寿命与兼容性是作者陈述，除非另有本地测量记录。
2. STEP解析成功仅说明当前静态交换几何可读取；不代表原生特征树、配合、运动和制造验证通过。
3. 未量测安装接口、未打开的原生配置及依赖状态保持未知。
4. 带有节点定位的子模块仍引用完整源文件，不自动宣称已导出独立可运动装配。
5. 公共源码许可与第三方CAD许可分别处理；各来源的原始说明保留，原始资产不自动取得公共再分发资格。
6. SolidWorks 实机复用记录覆盖 3508 双电机舵轮原车安装位的静态验收。其孔位和配合通过，整车强制重建及外部引用完整性仍有已记录的限制。 [SolidWorks 复用验收](reports/solidworks-swerve-acceptance/README.md)

## 第二批：论坛 2025 年后开源机械结构 CAD（点赞 ≥ 40，直链）

来源与口径全部由 `tools/open_cad_2025.py` 复现（只读论坛公开接口与公开 OSS 附件，不读账号 Cookie）：

~~~powershell
.venv\Scripts\python.exe tools\open_cad_2025.py discover    # 机械标签组 378 篇 → 点赞≥40 的 63 篇，逐篇抓原帖 HTML 解析附件
.venv\Scripts\python.exe tools\open_cad_2025.py download    # 入选帖附件直链下载，断点续传 + 按原帖声明 MD5 校验
.venv\Scripts\python.exe tools\open_cad_2025.py inventory   # 列压缩包条目，核对包内 CAD 件数
.venv\Scripts\python.exe tools\open_cad_2025.py report      # 生成报告与各目录说明
~~~

- `catalog/open-cad-2025-candidates.json`：63 篇候选的点赞、浏览、发布时间、正文、附件、入选/剔除理由。
- `catalog/open-cad-2025.json`：23 篇入选记录的完整清单、文件 MD5/SHA-256、本地路径、压缩包内 CAD 计数。
- `reports/OPEN-CAD-2025.md`：可读总表 + 每帖介绍与原帖链接 + 被剔除帖子及原因附录。
- `raw/downloads/<帖子ID>/`：附件原文件，同目录 `说明.md` 记录标题、原帖链接、点赞/浏览/评论、发布日、正文介绍节选、文件 MD5 与原直链。
- `raw/pages/<帖子ID>/`：原帖 HTML 证据页与压缩包条目清单（`*.listing.txt`）。

口径边界：点赞取论坛 `approvals`；发布时间取接口 `createAt`（UTC），2024 年及更早一律不收，旧论坛迁移帖按标题里的赛季声明剔除；只有网盘链接或附件仅 PDF/PPTX 的帖子不收（清单见报告附录）。附件版权与使用范围以原帖作者声明为准，本目录仅作队内检索样本。


## 第二阶段：本机HTTP服务

已实现本机只读检索、模型详情、校验后CAD及原包下载、OpenAPI描述。部署与插件/AI调用见 [HTTP服务说明](reports/HTTP_SERVICE.md)。三部分目录见 [样本目录](reports/SAMPLE_CATALOG.md)。原文中“尚未实现HTTP”描述保留为第一阶段历史边界。
