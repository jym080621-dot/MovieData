# MovieData

MovieData 是一个桌面电影票房采集工具。它从中国电影票房网站 `zgdypf.zgdypw.cn` 读取大盘、榜单、影片、影院和院线数据，保存到本地 SQLite，并导出 CSV 和 JSON。本说明适用于 **v1.5.3**。

## 选择采集方式

| 入口 | 适合的任务 | 输出 |
| --- | --- | --- |
| 采集控制台 | 从 24 个数据集中选择大盘、榜单、影片详情、指定影院、样本影院等任务 | 影片与影院索引、逐日数据、可选整表 |
| 全国全影院历史票房采集 | 选择一部影片和日期范围，逐家核查已知影院的日片目 | 离线执行计划、影院日 CSV、逐日审计及批次记录 |

全国历史页面采用固定的全国范围。程序合并逐日榜单、补充名单、独立影院名录和本地已知影院，再按影院和日期安排采集。同一影院同一天的有效片目可跨电影复用。当前全国历史名录的完整性仍待核实，审计标为 `unverified`。

## v1.5.3 新增影院名单下载

主页面「获取最新影院名录」自动查询站点公布的最新可查询日期，下载该日所有公开影院榜分页，并将候选影院编码写入 `output/cinema_roster/`。最近一天无榜单时，程序在最近七个可查询日期内向前查找。CSV 标注实际榜单日期、来源和 `unverified`，可作为该日的补充名单导入。

## 全国历史采集

- **独立名录**。导入带同名 `.manifest.json` 的影院编码 CSV，校验文件哈希与编码数量，逐家纳入候选。手动导出可查看名录差集及省级审计。
- **复核缺口**。点「③ 复核缺口」生成新计划，审查后再启动。复核任务会跨日期和影院轮转。名单已处理的日期可在当前复核的续跑计划中接续。
- **按需导出**。每批结束自动写逐日审计与汇总。完整影院日矩阵由「导出当前 CSV / 审计」生成。

全国历史页继续支持不限请求总次数与运行时间、暂停保存断点，以及从数据库恢复累计核查进度。无限制计划仍按所选频率发请求。

## 快速开始

- **Windows 免安装包**完整解压到可写目录后，双击 `MovieData.exe`。也可双击 `打开全国全影院历史.bat` 直达全国历史页面。
- **从源码运行**需要包含 Tkinter 和 SQLite 的 Python 3.10 或更新版本。运行 `python gui.py`，或在 Windows 双击 `启动采集器.bat`。
- **接续旧进度**时，退出旧程序，将原 `boxoffice.db` 放在新版 EXE 或 `gui.py` 同目录。保留 `raw/` 和 `output/`，便于查阅原始响应和历史导出。

第一次可以在主界面只勾选「02 全国大盘」，选一天，点「估算」后开始采集。采集单片全国历史时，从主界面点击「全国全影院历史票房采集」，填好影片与日期，先生成离线计划，再按计划启动。

[查看完整使用教程](使用教程.md)

[全国影院名录导入与核对](全国影院名录导入说明.md)

## 数据与结果

| 位置 | 内容 |
| --- | --- |
| `boxoffice.db` | 采集结果、影院名单、缓存和断点 |
| `raw/` | 源站原始响应，用于回溯 |
| `output/` | 控制台导出的影片、影院和整表 |
| `output/cinema_roster/` | 主页面下载的公开影院榜候选 CSV |
| `output/national_history/` | 全国历史的计划、批次记录和逐日审计。手动导出完整影院日 CSV 和名录差集 |

影院明细的粒度为「日期 × 影院 × 影片」。全国大盘提供含服务费与分账两套口径，使用时请按字段区分。影院详情的票房整数单位为分，榜单解析值可能有舍入。分析全国历史数据时，请结合 `status` 与逐日审计判断数据覆盖和金额含义。

## 源码、验证与打包

| 文件 | 职责 |
| --- | --- |
| `gui.py`、`national_history_gui.py` | 主控制台和全国历史窗口 |
| `pipeline.py` | 命令行采集、查询、审计、导出和补采 |
| `zgdypf/datasets.py` | 数据集注册与任务执行 |
| `zgdypf/national_history.py`、`zgdypf/history_roster.py`、`zgdypf/cinema_detail.py` | 计划估算、名单分页、影院片目校验与审计 |
| `zgdypf/cinema_registry.py`、`zgdypf/national_quality.py` | 独立名录校验与差集审计、影院日状态汇总 |
| `zgdypf/client.py`、`zgdypf/db.py`、`zgdypf/export.py` | 请求、数据存储和通用导出 |

```text
python pipeline.py --help
python pipeline.py movie-history --help
python -m unittest discover -s tests -v
```

测试使用离线响应，界面测试需要可用的桌面环境。`contract_test.py` 用于验证源站接口，会发出真实网络请求。用 `python build_release.py --source-only` 只打源码包。Windows 下安装 PyInstaller 后可用 `python build_release.py --build` 构建免安装包。产物写入 `dist/`。

源码包内的 `CONTRACT.md` 记录接口与字段实测，`单片历史影院使用说明.md` 说明命令行补采与审计，`VALIDATION_v1.5.2.md` 记录原始交付的复验结果，`CHANGELOG.md` 记录版本变更。项目使用 MIT 许可证，见 `LICENSE`。
