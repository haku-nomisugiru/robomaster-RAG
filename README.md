# RoboMaster CAD RAG

按机构功能检索 RoboMaster 模型，为设计助手提供候选、原帖、CAD 文件指纹、已验证信息和适用限制。一张机构卡片可以对应一个 CAD 入口文件，也可以指向文件中的特定子结构。

仓库随附 26 张机构卡片，涵盖云台、底盘、轮组或轮腿，以及补充机构。服务采用 SQLite FTS5 词法检索，通过只读 HTTP 返回 JSON。模型生成由调用方负责。

## 下载启动包

下载 [带机构索引的启动包](https://github.com/haku-nomisugiru/robotmaster-RAG/releases/download/v0.2.0/RoboMaster-CAD-RAG-v0.2.0.zip) · [Release 与 SHA-256](https://github.com/haku-nomisugiru/robotmaster-RAG/releases/tag/v0.2.0)。

1. 准备 Python 3.11+，其 SQLite 需支持 FTS5。
2. 完整解压 ZIP，在解压目录运行下面的命令。Windows 也可双击 `start-rag.cmd`。默认端口为 8765，可以加 `--port 8766` 改用其他端口。
3. 在浏览器打开 [本机健康检查](http://127.0.0.1:8765/health)。正常结果的 `models` 为 26。启动终端中按 Ctrl+C 停止服务。

~~~powershell
python -B tools/http_service.py --port 8765
~~~

启动包已包含 26 张机构卡片和预建 SQLite 索引，无需安装 Python 第三方库或先运行 `index`。如需下载模型文件，请按“数据与资产”准备本地附件。

## 从源码启动

需要 Python 3.11+ 和包含 FTS5 的 SQLite。检索和 HTTP 服务使用 Python 标准库。克隆后，在仓库根目录执行。

~~~powershell
python tools/library.py index
python tools/library.py search "滑轨悬挂底盘"
python tools/http_service.py --port 8765
~~~

另开一个终端。

~~~powershell
Invoke-RestMethod 'http://127.0.0.1:8765/health'
$q = [uri]::EscapeDataString('舵轮')
$hits = Invoke-RestMethod "http://127.0.0.1:8765/v1/search?q=$q&section=wheel_or_leg&limit=5"
$hits.items | Select-Object id, name, source_url
~~~

`index` 用随附卡片重建 SQLite 搜索库。Windows 下也可用 `python -m venv .venv` 建立环境，再用 `scripts/` 中的脚本启停服务。脚本预期 `.venv/Scripts/python.exe` 已存在。更新索引前先停止服务，更新后重新启动。

## HTTP 接口

| 方法 | 路径 | 内容 |
| --- | --- | --- |
| GET | /health | 状态与机构卡片数量 |
| GET | /openapi.json | 客户端接口描述 |
| GET | /v1/sections | 分类及各类数量 |
| GET | /v1/search?q=关键词&section=分类&limit=5 | 搜索机构并返回来源、验证和限制 |
| GET | /v1/models/{id} | 一张机构卡片的详情 |
| GET / HEAD | /v1/models/{id}/file | 校验并下载本地登记的 CAD 入口 |
| GET / HEAD | /v1/models/{id}/package | 校验并下载本地登记的来源包 |

分类为 `gimbal`、`chassis`、`wheel_or_leg`。省略 `section` 时一并搜索补充卡片。详情中的相对下载路径以服务地址为根。资产缺失返回 404，哈希变化返回 409。服务绑定 `127.0.0.1`，供本机客户端使用。见 [HTTP 服务说明](reports/HTTP_SERVICE.md)及 `examples/` 中的 Python 和 C# 示例。

## 数据与资产

- `catalog/modules.jsonl` 保存机构卡片，`sources.json` 保存原帖来源和证据指纹，`downloads.json` 保存归档路径及文件哈希。
- `verification.file_present` 反映卡片建库时的观察。下载时服务重新检查当前文件及其 SHA-256。
- 原始模型包、解包后的 CAD、原帖快照和生成的 SQLite 保存在本地。按 `source_url` 查阅原作者说明并取得获准使用的附件。将来源包放在 `catalog/downloads.json` 登记的相对路径，将入口文件放在卡片的 `entry.path`，核对 SHA-256 后即可通过服务下载。原生装配应优先取完整包，并检查引用。
- `tools/ingest_forum.py`、`tools/open_cad_2025.py` 与 `tools/import_local.py` 可采集公开来源或导入本地附件。完整重新入库需要实际模型、库存清单、来源记录与模块选择文件，再运行 `tools/library.py build` 和 `validate`。可在独立数据目录使用 `--root`，保留随附的检索基线。
- 可选 STEP 解析使用 `requirements-cad.txt` 中的依赖。`tools/inspect_native.py` 的 SolidWorks 引用检查需要相应 Windows 环境与 pywin32。第三方 CAD 的使用和再分发遵循原作者授权。

## 接入机械设计工作台

启动本服务后，在 [RoboMaster 机械设计工作台](https://github.com/haku-nomisugiru/dsh-autorobotmaster) 使用“模型库”。其他本机 AI 客户端也可将检索、详情和下载封装为工具。检索后将候选来源、验证状态和限制一起交给生成模型。作者陈述、静态解析、配合、运动及目标适配分别记录。未验证参数保持未知。

## 验证

~~~powershell
python tools/library.py index
python -m unittest discover -s tests -p "test_*.py" -v
~~~

HTTP 搜索、详情和参数边界可在干净检出上验证。两项资产下载测试需要原样本，STEP 解析测试需要可选 CAD 依赖。测试在本机临时端口运行。
