# MovieData

MovieData 是一个桌面电影票房采集工具。它从中国电影票房网站 `zgdypf.zgdypw.cn` 读取大盘、榜单、影片、影院和院线数据，保存到本地 SQLite，并导出 CSV 和 JSON。本说明适用于 **v1.5.0**。

## 选择采集方式

| 入口 | 适合的任务 | 输出 |
| --- | --- | --- |
| 采集控制台 | 从 24 个数据集中选择大盘、榜单、影片详情、指定影院、样本影院等任务 | 影片与影院索引、逐日数据、可选整表 |
| 全国全影院历史票房采集 | 选择一部影片和日期范围，逐家核查已知影院的日片目 | 离线执行计划、影院日 CSV、逐日审计及批次记录 |

全国历史页面采用固定的全国范围。程序合并逐日榜单、补充名单和本地已知影院，再按影院和日期安排采集。同一影院同一天的有效片目可跨电影复用。全国历史名单的完整性取决于来源证据，当前审计标为 `unverified`。

全国历史页面支持按预算分批运行，也可一键生成无限制计划，审查后连续采集。点击「暂停并保存断点」会保存已取得的结果，准备续跑计划。蓝条显示已知名单的累计核查进度。

## 快速开始

- **Windows 免安装包**完整解压到可写目录后，双击 `MovieData.exe`。也可双击 `打开全国全影院历史.bat` 直达全国历史页面。
- **从源码运行**需要包含 Tkinter 和 SQLite 的 Python 3.10 或更新版本。运行 `python gui.py`，或在 Windows 双击 `启动采集器.bat`。
- **接续旧进度**时，退出旧程序，将原 `boxoffice.db` 放在新版 EXE 或 `gui.py` 同目录。保留 `raw/` 和 `output/`，便于查阅原始响应和历史导出。

第一次可以在主界面只勾选「02 全国大盘」，选一天，点「估算」后开始采集。采集单片全国历史时，从主界面点击「全国全影院历史票房采集」，填好影片与日期，先生成离线计划，再按计划启动。

[查看完整使用说明](使用教程.md)

## 数据与结果

| 位置 | 内容 |
| --- | --- |
| `boxoffice.db` | 采集结果、影院名单、缓存和断点 |
| `raw/` | 源站原始响应，用于回溯 |
| `output/` | 控制台导出的影片、影院和整表 |
| `output/national_history/` | 全国历史的计划、批次记录、影院日 CSV 和逐日审计 |

影院明细的粒度为「日期 × 影院 × 影片」。全国大盘提供含服务费与分账两套口径，使用时请按字段区分。影院详情的票房整数单位为分，榜单解析值可能有舍入。分析全国历史数据时，请结合 `status` 与逐日审计判断数据覆盖和金额含义。

## 源码、验证与打包

| 文件 | 职责 |
| --- | --- |
| `gui.py`、`national_history_gui.py` | 主控制台和全国历史窗口 |
| `pipeline.py` | 命令行采集、查询、审计、导出和补采 |
| `zgdypf/datasets.py` | 数据集注册与任务执行 |
| `zgdypf/national_history.py`、`zgdypf/history_roster.py`、`zgdypf/cinema_detail.py` | 计划估算、名单分页、影院片目校验与审计 |
| `zgdypf/client.py`、`zgdypf/db.py`、`zgdypf/export.py` | 请求、数据存储和通用导出 |

```text
python pipeline.py --help
python pipeline.py movie-history --help
python -m unittest discover -s tests -v
```

测试使用离线响应，界面测试需要可用的桌面环境。`contract_test.py` 用于验证源站接口，会发出真实网络请求。Windows 打包需安装 PyInstaller，在源码目录运行 `python build_release.py --build`，产物写入 `dist/`。

源码包内的 `CONTRACT.md` 记录接口与字段实测，`单片历史影院使用说明.md` 说明命令行补采与审计，`CHANGELOG.md` 记录版本变更。项目使用 MIT 许可证，见 `LICENSE`。
