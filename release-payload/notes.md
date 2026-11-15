## Windows x64 便携包

在解压目录打开 PowerShell，执行 `.\get-cad.cmd --all`，即可下载并导入全部 3,468 张卡片对应的 18 个文件，约 6.24 GB。包括 ACE 工程的三个 RAR 分卷，包内 UnRAR 自动解包，并生成供 HTTP 和工作台下载的 CAD ZIP。原有卡片和向量继续定位对应 CAD，无需重新向量化。重复执行会复用已下载文件和分卷；处理结果保存在 `runtime/cad-acquisition-report.json`。

修复 v0.5.2 的三个覆盖缺口：四个整包的过滤（1,476 张）、分卷导入（927 张）、装配引用标记导致的文件入口关闭（13 张）。使用本地原始附件完成全量导入验收：3,468 张卡片、2,617 个文件入口均通过 SHA-256 校验；18 个原作者文件的匿名 HTTPS 地址和大小已逐一核对。详见仓库 `reports/ACQUISITION_ACCEPTANCE.json` 与 `reports/SOURCE_URL_CHECKS.json`。

下载 `RoboMaster-CAD-RAG-v0.5.3-win-x64.zip`，完整解压后双击 `start-rag.cmd`。默认监听 `http://127.0.0.1:8765`，端口占用时自动选择空闲端口，窗口显示实际地址。包内含 Python、应用本地 C++ 运行库、SQLite 文本索引、几何索引、3,468 条 Chroma 持久向量及本地查询编码器，启动后可直接做型号精确查询和混合检索。

在含中文和空格的干净目录通过包内启动脚本进行了两次启动与 HTTP 验收。覆盖 `3508 舵轮`、全角 `Ｐ１５．８`、近似型号 `P15.76` 以及 SQLite 和 Chroma 共同命中的功能查询。测试前后 Chroma 数据与 HNSW 索引一致。验收记录和 ZIP SHA-256 随 Release 提供。

在解压目录运行 `get-cad.cmd <卡片ID>`，即可获取已匹配的作者附件，并验证附件和目标 CAD 的 SHA-256。手动下过的同版附件、CAD 或解压目录可用 `--from <路径>` 导入。STEP 卡片保留子定义编号，SolidWorks 原生装配保留包内目录结构。工作台按 README 配置 `cad_library_url` 后，可以下载准备好的文件并打开检查。第三方 CAD 从原作者渠道取得，几何相似检索还会校验相关量测 STEP。
