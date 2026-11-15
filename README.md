# MovieData
用于从https://zgdypf.zgdypw.cn/ 上获取电影数据的工具，只供学术研究使用。

# 中国电影票房（专资办口径）采集复现

对 zgdypf.zgdypw.cn 站内 JSON 接口做的一次**带契约验证**的采集复现。
目标不是把数据抓下来，而是先证明「这套方案在真实接口上成立到什么程度」，
再把可用的部分工程化。

---

## 一、结论速览

| 项 | 结果 |
|---|---|
| 24 个数据集是否真实存在 | **是**，全部实测可用 |
| 五段叙述的参数/字段是否准确 | **否**，7 处需修正（见 `CONTRACT.md` 第一节） |
| 能否做「300~500 家 × 30 天」平衡面板 | **能，但必须选对窗口**：约 1/3 的日期影院维度整片缺失 |
| 金额单位 | 分（榜单整数）／万（顶部汇总）／**元或万漂移**（影院分片 Desc），三者并存 |
| 颗粒度上限 | 影院 × 影片 × 日；无影厅、无场次、无座位级 |
| 全量影院数 | 12,109 ~ 12,717 家（随日期波动） |

---

## 二、工程结构

```
MovieData/
├── zgdypf/
│   ├── client.py     HTTP：全局限速、分级重试、原始响应落盘
│   ├── parse.py      单位解析（元/万/亿、分、占比），软失败
│   ├── ssr.py        __INITIAL_STATE__ 抽取（字符串感知的括号配对）
│   ├── api.py        24 个端点的函数封装，参数名按实测固定
│   ├── common.py     日期/金额行归一/完整性探针（CLI 与 UI 共用）
│   ├── datasets.py   24 个数据集注册表 + 执行引擎（UI 的底座）
│   ├── db.py         SQLite schema + 通用 upsert + 断点台账
│   ├── sample.py     省份 × 规模三分位分层抽样
│   └── qa.py         8 项质量校验 + Markdown 报告
├── gui.py            ★ Tkinter 图形界面（24 选 N、日期区间、进度、日志）
├── 启动采集器.bat     双击启动（pythonw，无控制台窗口）
├── pipeline.py       命令行主程序：init / day / verify / sample / box / movie / qa
├── contract_test.py  32 条契约断言（改站点前先跑它）
├── boxoffice.db      结果库
├── raw/              原始响应（按端点/日期/编码落盘，可回溯）
├── sample.csv        锁定的样本名单
├── CONTRACT.md       接口实测契约（核心文档）
├── QA_REPORT.md      质量报告（pipeline qa 生成）
├── TABLES.md         结果表（make_tables.py 生成）
├── 使用教程.md         ★ 界面完整使用手册（每个选项、每个数字的含义）
└── phase0_*.py, diag_*.py   探测与诊断脚本（保留证据）
```
