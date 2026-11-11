# 本机 HTTP 检索服务

服务地址为 `http://127.0.0.1:8765`。只监听本机回环地址，为桌面插件和本机 AI 提供只读查询。SQLite 路径可独立运行，功能混合检索使用项目虚拟环境中的 Chroma 和 FastEmbed。服务在本机后台运行。

## 启停

在 `[drive D]/SOLIDWORKS\RoboMasterCADLibrary` 运行：

```powershell
powershell.exe -NoProfile -ExecutionPolicy Bypass -File scripts/start-service.ps1
powershell.exe -NoProfile -ExecutionPolicy Bypass -File scripts/status-service.ps1
powershell.exe -NoProfile -ExecutionPolicy Bypass -File scripts/stop-service.ps1
```

日志在 `runtime/stdout.log` 和 `runtime/stderr.log`，进程记录在 `runtime/service.json`。更新样本库时先停止服务，再构建并验证 SQLite，运行 `tools/vector_search.py sync` 后重新启动。首次功能查询按需加载本地模型和集合，`/health` 的 `vector_status` 显示加载结果。

## 接口

| 方法 | 路径 | 用途 |
|---|---|---|
| GET | /health | 健康与样本数量 |
| GET | /openapi.json | AI及客户端接口描述 |
| GET | /v1/sections | 三类名称及数量 |
| GET | /v1/search?q=关键词&section=分类&limit=5&mode=auto | 功能、型号或默认搜索，返回完整卡片 |
| GET | /v1/similar?reference_id=卡片ID&mode=size&limit=5 | 从已索引参考卡查找几何相似 CAD |
| GET | /v1/models/{model_id} | 模型详情、来源、验证状态、下载链接 |
| GET | /v1/models/{model_id}/reuse | 复用证据、CAD 选择范围、下载指纹和待核验项 |
| GET / HEAD | /v1/models/{model_id}/file | CAD入口文件 |
| GET / HEAD | /v1/models/{model_id}/package | 来源模型包（原始ZIP或STEP等） |

section取gimbal（云台）、chassis（底盘）、wheel_or_leg（轮子或轮腿）。省略分类会检索全部可用卡片，包括补充资料。当前卡片数量和分类数量可从 `/health` 与 `/v1/sections` 获取。q长1–256字符，limit为1–50。无命中返回200及空items。未知ID返回404，参数错误400，文件内容与登记SHA-256不符返回409。

响应中的 source/source_url 指向原帖，entry记录CAD路径与SHA-256，verification记录解析/运动/配合等状态，dependency_scope记录已保存引用检查结果，limitations记录限制。engineering_parameters中的null表示未验证，不能当作0。downloads.model和downloads.package是相对于服务地址的路径，package_sha256用于校验原始包。下载响应也包含X-Content-SHA256及ETag。

搜索使用分字段 FTS5，名称和别名优先，并核对查询中的型号词。返回的 `matches` 列出命中的字段与索引词，`exact_match` 表示整条查询命中 ID、名称、类别或关键词，`score` 综合精确匹配优先级和加权 BM25。详情和搜索结果都带 `retrieval_profile`，其中 `facts` 逐条带原帖链接和本地证据定位，`reuse_checks` 列出针对该机构的待核验项。比如查询「MGN7 滑轨 气弹簧」可定位 3508 双电机舵轮的悬挂设计和作者 PDF 第 8 页。

`mode=auto` 保留原有默认词法行为。`mode=function` 对用途、工作原理或结构描述同时使用 SQLite FTS5 和本地 Chroma。两路按 CAD 来源和定义去重，再用加权倒数排名融合。SQLite 权重为 1，Chroma 权重为 0.35，排名偏移为 4。SQLite 命中可按去重后的查询词覆盖率额外获得最多 0.12 分。整条查询精确命中 SQLite 的卡片名称或别名时优先。 [倒数排名融合论文](https://cormack.uwaterloo.ca/cormacksigir09-rrf.pdf)

`mode=model` 只走 SQLite，精确查找完整卡片 ID、文件名或型号。组合输入中的型号须在同一卡片都有依据，其他词用于匹配用途。例如 `P15.8 双轴承支撑` 在 P15.8 系列内查重型变体。

混合结果的 `retrieval` 为 `hybrid`，各卡片的 `retrieval_channels` 给出来源、原始名次和对应分数或向量距离。混合 `score` 用于同一次查询内排序。Chroma 缺失、加载失败或指纹与当前目录不符时，功能查询自动返回 SQLite 结果，`vector_status` 显示 `unavailable` 或 `stale`。 [同批样本对照](HYBRID_ACCEPTANCE.md)

型号模式的 `model_matches` 返回每个匹配型号的 `code`、`role` 和 `evidence`。`used_component` 表示该机构用了该型号，`reference_product` 表示作者把它作为对照产品，`cad_name` 表示依据为文件命名，`module_model` 和 `model_family` 分别指向机构型号和系列。没有用途词时，型号模式的 `matches` 可为空，型号依据从 `model_matches` 读取。 [功能与型号对照报告](RETRIEVAL_COMPARISON.md)

`/v1/search` 支持 `alias` 精确别名和重复的 `spec` 数值过滤，可以与 `q` 组合或单独使用。型号模式需要 `q`。例如 `/v1/search?spec=opening_diameter_mm:6&spec=opening_count:9` 返回符合这两个接口条件的组件，结果中的 `component_specs` 保留每个数值的量测范围和证据，`cad_source` 给出原 CAD 入口。 [规格字段与示例](../README.md#组件目录和接口证据)

`category` 按机构功能筛选，七个枚举见 OpenAPI。功能模式把该条件同时用于 SQLite 和 Chroma，例如 `mode=function&category=gripper` 可搜吸盘贴合、托盘承托、独立前伸等特征。返回的 `mechanism_profile` 区分有来源的优势、取舍、适用范围，以及 CAD 观察到的直接子项。作者证据附原文定位，`cad_source` 保留可打开的模型入口。 [七类机构验收](MECHANISM_ACCEPTANCE.md)

`/v1/similar` 使用已入库的 `reference_id`。可选 `configuration` 指定原生导出配置，`mode=size` 比较实际尺寸，`mode=shape` 比较等比缩放下的形状，`include_reference=true` 可返回原定义及其副本。响应含排序距离、分项、原始 CAD 路径、STEP 定义和详情链接。参考本机其他 STEP 或进行表面采样时使用几何检索命令行。 [参考 CAD 使用说明](../README.md#参考-cad-相似检索)

`component_locator` 返回包内路径和哈希。对 STEP 子定义还返回 `definition_id`、实例数、部分实例路径及报告位置。原生装配的 `saved_reference_links` 来自保存引用快照，匹配状态和候选 ID 供查证。下载字段为 `null` 时应查看卡片定位和依赖说明。分卷归档返回本地卷清单，STEP 子定义的文件入口是完整的源 STEP。

已现场读取的 MVP 原生样本通过 `native_live_report` 定位活动配置的实例快照。 `live_reference_targets` 只计入已解析并且目标文件在同包内通过哈希校验的实例。轻化或抑制组件仍可列出文件候选，`path_semantics` 会标明该路径的证据类型。STEP 定义另外带解析器版本和直接子定义 ID。

模型详情与搜索结果都返回 `component_id`。装配卡的 `interfaces.component_measurements` 列出有效的接口证据链接，通过其中的 `component_card_id` 查询对应零件。零件卡的 `interfaces.measurements` 提供量测配置、基准、孔组、包络和输入指纹。证据为 `stale_evidence` 时，旧数值只留在历史记录中。现有搜索入口按文字查找卡片，按孔径过滤可读取本机 SQLite 的 `interface_openings.diameter_mm`。几何对齐和物理适配分别记录。 [接口验收记录](INTERFACE_ACCEPTANCE.json)

## 插件调用

```powershell
Invoke-RestMethod 'http://127.0.0.1:8765/health'
$q = [uri]::EscapeDataString('把轮组接到悬挂的机加工法兰')
$result = Invoke-RestMethod "http://127.0.0.1:8765/v1/search?q=$q&mode=function&section=wheel_or_leg&limit=3"
$result.items | Select-Object id,name,retrieval_channels
```

C#示例见 examples/CadLibraryClient.cs，引用System.Net.Http。先SearchAsync，再GetModelAsync，读取详情中的哈希后调用DownloadAsync。JSON解析可使用现有插件依赖。下载采用流式写入临时文件、客户端SHA-256校验后再落盘，不覆盖已有目标文件。示例未加入现有SolidWorks插件工程，也未在其目标框架内编译验证。

原生SLDASM应优先取package以保留包内相对目录；仅下载入口SLDASM通常不足以打开装配。package是原始来源包，并不保证所有依赖已齐全。STEP子定义仍返回完整STEP，客户端需按definition_id定位。此服务不启动SolidWorks、不修改配合、不自动插入装配。

## AI调用

本机 AI 可通过 `/openapi.json` 发现检索、详情、复用证据和下载操作。按用途或结构选 `/v1/search?mode=function`，已知型号选 `mode=model`，已有参考卡选 `/v1/similar`。

1. 选中候选后读取它的 `reuse_href`。查看作者事实及其页码、原始 CAD 指纹、`selection` 和 `required_checks`。
2. 用 `component_measurements[].component_reuse_href` 查看零件上的接口量测。原生装配的 `native_configurations` 与子件量测的配置分别记录。
3. 从 `artifacts` 选择下载项。原生装配在原包可用时优先用包，按 `selection.asset_relative_path` 找入口并核对外部引用。STEP 子件使用原始 STEP 及 `selection.definition_id`。
4. 用 `artifacts[*].sha256` 核对 HTTP 的 `X-Content-SHA256` 和实际下载字节。复用前查看 `verification` 及 `required_checks`，并在目标装配中检查接口。

在项目目录可用本机客户端执行上述流程。

```powershell
.venv\Scripts\python.exe -B examples\reuse_cad.py --query '把轮组接到悬挂的机加工法兰' --category suspension
.venv\Scripts\python.exe -B examples\reuse_cad.py --query '3508 舵轮' --mode model --category swerve
.venv\Scripts\python.exe -B examples\reuse_cad.py --reference rm-1935523-c1ab8d2a2c8db5352 --output runtime\ai-reuse
.venv\Scripts\python.exe -B tools\evaluate_reuse.py --port 8765
```

客户端输出候选和复用证据，指定 `--output` 时下载选中的文件。`--select` 可指定候选卡，`--artifact` 可指定文件或原包。验收通过 HTTP 下载样本，复算 SHA‑256，复读 STEP 定义并重测接口，也核对原包内的装配文件指纹。 [AI 复用验收](AI_REUSE_ACCEPTANCE.json)

远端 AI 可以通过本机插件或本机工具代理发起请求。WebView 通过插件宿主调用回环服务。单路向量查询仍可通过 `tools/vector_search.py` 独立运行。每次下载前服务都会计算 CAD 文件的 SHA-256。 [Chroma 查询与验收](VECTOR_ACCEPTANCE.md)
