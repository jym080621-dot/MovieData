# CONTRACT.md —— zgdypf.zgdypw.cn 接口实测契约

> 本文档中每一条都对应一次真实请求的返回值，**不采信任何二手叙述**。
> 采集代码（`zgdypf/`）的所有参数名、字段名、单位判断均以本文为准。
> `contract_test.py` 把其中 32 条固化为可执行断言，跑一次约 15 秒。

实测时间：2026-11-13，数据日期以 2026-04-01 ~ 2026-11-12 为主。

---

## 一、先说结论：五段叙述里错在哪

| # | 叙述的说法 | 实测事实 | 后果 |
|---|---|---|---|
| 1 | 影院榜字段含 `sales`（单位分） | **影院榜行没有 `sales`**，只有 `salesDesc` 字符串 | 金额只能解析，且单位在「元/万」间漂移；按分读取会得到 NULL |
| 2 | `getChainList` 传 `geoCode=1&geoType=1` 表示全国 | `geoType=1` 是**区域**（geoCode=1 → 华北） | 静默少算 86%：238 万 vs 全国 1707 万 |
| 3 | `searchMovie` 参数是 `keyword` | 必须 `movieName`，`keyword` 直接 **400** | 片名反查全挂 |
| 4 | `getSingleDayData?date=...` 可按日筛 | 参数**被完全忽略**，恒返回 20 条历史纪录 | 按日期取数会拿到别人的数据 |
| 5 | `getDateRange.endDate` = 昨日 | 返回**今天** | 当日数据可查，但仍在滚动 |
| 6 | 204 = 无数据 | 对 `getCinemaBoxData` 成立；`getCinemaSumData` 是 **200 + `value:"--"`** | 把 `"--"` 当 0 入库 |
| 7 | 历史日期均可查 | 影院维度**约 1/3 的日期整片缺失** | 见第五节，这是最致命的一条 |

---

## 二、端点清单（24 个数据集）

### 2.1 大盘与榜单

| 端点 | 参数 | 返回 | 备注 |
|---|---|---|---|
| `/getDateRange` | 无 | `{startDate, endDate}` | `2017-01-01` ~ **今天** |
| `/getNational` | `date`, `withSvcFee` | `{salesDesc:{unit,value}, splitSalesDesc:{unit,value}, updateGapSecond, updateTimestamp}` | `salesDesc` 是 **dict**；`unit` 恒为 `"万"` |
| `/getProvinceList` | `date`, `withSvcFee`, `limit`, `offset` | `{salesRankList[], updateTimeDesc}` | 31 条；行内 `sales` 是**整数分**，`salesDesc` 是**字符串** |
| `/getChainList` | `date`, `geoCode`, `geoType` | 同上 | **缺 geoCode/geoType 直接 400**；全国 = `0,0` |
| `/getCinemaList` | `date`, `withSvcFee`, `limit`, `offset`, [`geoCode`,`geoType`] | `{hasMore, salesRankList[], updateGapSecond, updateTimeDesc}` | **行内无 `sales`**；`geoType=2` → 500；`geoType=3` + 6 位区码 = 按城市 |
| `/getDayData` | `date`, `withSvcFee` | `{list[], nationalSales, updateGapSecond, updateTimestamp}` | 影片级全国榜，约 137 条 |

### 2.2 影院 / 院线

| 端点 | 参数 | 返回 | 备注 |
|---|---|---|---|
| `/getCinemaSumData` | `cinemaCode`, `date` | `{nationalQuotaInfo:{movieCount,newMovieCount,sessionCount}, salesDesc:{unit,value}, updateGapSecond}` | 空日 = 200 + `value:"--"`，且**无 nationalQuotaInfo** |
| `/getCinemaBoxData` | `cinemaCode`, `date` | `{movieCount, newMovieCount, sessionCount, salesDesc, movieSalesList[]}` | 空日 = **204**；`movieCount == len(movieSalesList)` |
| `/getCinemaManageData` | `cinemaCode`, `dateType=0` | `list[14]` | **14 条 ≠ 14 天**，按「有数据的最近 14 天」返回，含今天 |
| `/searchCinema` | `cinemaName`, `limit`, `offset` | `[{code,name,cityCode,countyCode,hallCount,seatCount}]` | 比叙述多 4 个字段，可用 `cityCode[:2]+"0000"` 反推省份 |
| `/getChainSumData` | `cinemaChainCode`, `date` | 同 `getCinemaSumData` | — |
| `/getChainBoxData` | `cinemaChainCode`, `date` | 同 `getCinemaBoxData` | — |
| `/getChainManageData` | `cinemaChainCode`, `dateType` | `list[15]` | — |
| `/getChainYesterday` | `cinemaChainCode`, `geoCode`, `geoType` | 单个对象（非数组） | 返回的是**区域**切片，如 `name:"华北"` |

### 2.3 影片

| 端点 | 参数 | 返回 | 备注 |
|---|---|---|---|
| `/searchMovie` | **`movieName`**, `limit`, `offset` | `[{code,name,pubDesc,typeDesc,firstPicture,salesDesc}]` | 无 `url` 字段；海报在 `firstPicture` |
| `/movie/detail/<code>` | — | HTML（约 74KB），内嵌 `window.__INITIAL_STATE__` | **没有 JSON 接口**；6 个 `getMovie*` 全 404 |

`__INITIAL_STATE__` 结构：

```
movieInfoData   {code,name,pubDesc,typeDesc,drama,celebrityInfoList}
movieBoxSum     [{title:"累计票房"/"首周票房"/"首日票房", unit:"亿", value:"154.46"}]
movieBoxTop     [{name,title:"地区"/"院线",unit:"万",value}]
movieIndexData  [{showDate,weekDate,xAxisName,releaseDays,sales(分),salesCount,
                  sessionCount,seatCount,salesInWanDesc,salesRateDesc,
                  sessionRateDesc,seatRateDesc,avgSalesCount,bestSessionRateDesc}]
```

### 2.4 历史纪录（参数一律无效）

`/getSingleDayData`、`/getReleaseDayData`、`/getNationSingleDayData`、
`/getNationSingleWeekData`、`/getNationSingleMonthData`
→ 全部返回 `{rankList[20], title, updateTimeDesc}`，传任何参数结果**字节级相同**。

### 2.5 周期 / 城市 / 年度

| 端点 | 参数 | 备注 |
|---|---|---|
| `/getPeriodData` | `date` = **周期起始日**, `dateType` 1周/2月/3年 | 传中间日期**静默返回空 list**，不报错 |
| `/getCityData` | `date`, `movieCode` | `cityRank[]` 的 `value` 单位是**元**（唯一例外）；首行是 `"全国"` 且无 `code` |
| `/getYearTabs` | `year` | `list[11]` |
| `/getYearData` | `year` | `{rankList[], title, updateTimeDesc}` |

---

## 三、字段类型对照（同名不同型）

| 字段 | 出现位置 | 类型 | 单位 |
|---|---|---|---|
| `sales` | 省份榜 / 院线榜 | `int` | **分** |
| `sales` | **影院榜** | **不存在** | — |
| `salesDesc` | `getNational` / `getCinemaSumData` / `getChainSumData` | `dict{unit,value}` | `unit` 恒为 `"万"`；无数据 `value="--"`；**小影院 `value="<0.1"`** |
| `salesDesc` | 三类榜单行内 | `str` | 带中文单位，如 `"187.38万"`；< 1 万时是裸「元」 |
| `salesDesc` | `getCinemaBoxData` 顶层 | `str` | **裸数字 = 元**（`"9453"`），`"5.65万"` = 万 |
| `salesDesc` | `movieSalesList[]` / `sumSalesDesc` | `str` | **同上，元/万混用** |
| `salesInWanDesc` | `getDayData` | `str` | **裸数字 = 万**（字段名自带 InWan） |
| `value` | `getCityData.cityRank[]` | `int` | **元** |

金额解析规则（`zgdypf/parse.py` 已实现）：

```
有「万」→ ×1e4，有「亿」→ ×1e8
无单位 → 默认「元」；字段名含 InWan 的按「万」
"--" / "" / "-" → None（软失败，不抛异常）
"<0.1" / ">0.1" → 数值 + cmp 标记（上界/下界），不丢样
```

**实测补充：`salesDesc` 是向下截断，不是四舍五入。** 同一影院同一天：

| 分片接口精确值（分） | 影院榜 `salesDesc` | 解析值（分） | 截断后损失 |
|---:|---|---:|---:|
| 9,543,000 | `"9.54万"` | 9,540,000 | 3,000 |
| 4,534,100 | `"4.53万"` | 4,530,000 | 4,100 |
| 3,118,610 | `"3.11万"` | 3,110,000 | 8,610 |
| 2,507,816 | `"2.50万"` | 2,500,000 | 7,816 |

即 ≥1 万的金额被 floor 到 100 元，单店单日最多损失 99 元。
12,000 家影院累加约 60 万元/天，足以解释「影院榜合计只有全国 96%」中的一部分。
**结论：任何需要精确金额的分析都必须用分片接口，不能用车库榜的 `salesDesc`。**

---

## 四、HTTP 语义

| 状态 | 含义 | 处理 |
|---|---|---|
| 200 | 正常 | 解析 |
| 200 + `"--"` | 该影院该日无数据（仅 `getCinemaSumData` 系） | 记 `empty`，不重试 |
| 204 | 该无数据（`getCinemaBoxData`） | 记 `empty`，不重试 |
| 400 | 参数缺失/错误（`getChainList` 少 geo、`searchMovie` 传 keyword） | **不重试**，修参数 |
| 404 | 端点不存在 | **不重试** |
| 500 | `geoType=2` 省级筛选 | 不用该参数 |
| 5xx / 超时 | 临时故障 | 重试 3 次，退避 1s/4s/16s |

---

## 五、最致命的一条：影院维度按日整片塌缩

同一日期，`getCinemaList` 返回的影院数会在 **1 家 ~ 12000 家** 之间跳变，且
`getCinemaBoxData`、`getCinemaCity`、`getCinemaManageData` 同步失效（全 204）。
此时 `getNational` / `getProvinceList` / `getDayData` / 影片 SSR **仍然正常**。

实测（2026-11-13 当天多次探测）：

| 日期 | 状态 |
|---|---|
| 2026-09-14 ~ 10-13 | 10 天残缺（每 3 天塌 1 天） |
| 2026-10-14 ~ 11-12 | 10 天残缺（每隔 3 天） |
| 2026-11-01 ~ 05-21 | 21 天连续残缺 |
| 2026-03-01 ~ 04-30 | **全部完整**（60 天） |
| 2026-04-01 ~ 04-30 | 全部完整（本面板所用窗口） |

**塌缩是静默的**：接口 200、`hasMore=false`、字段齐全，只是行数只有个位数。
若按「204 = 无数据」直接入库，会把整天的样本写成空值，且事后无法区分
「影院真的没营业」和「站点数据没了」。

因此采集器必须：

1. **按日开闸**：用 `limit=10&offset=11000` 探针（廉价且精确）判断该日是否有 ≥11010 家；
2. **整日跳过**：闸门不过就一天都不抓，绝不写入 204；
3. **事后修复**：记录 `date_status`，换时间窗口重跑。

> 注意：`limit=2` 时接口返回 `hasMore=true` 是**假阳性**（底层只有 25 行也会说还有更多），
> 用小 limit 判完整性会得出「全部正常」的错误结论。这是本人踩过的第二个坑。

---

## 六、其他坑

1. `getCinemaManageData` 返回的 14 条不是 14 个日历日，跨度可达 20 天；按 `[-7:]` 取「近 7 天」会取到 11 天。
2. `getPeriodData` 传非周期起始日静默返回空，不报错。
3. `getCityData` 的 `movieList` 在实测中为空数组，别指望它返回影片下拉。
4. `getChainYesterday` 返回单个对象而非数组。
5. 影片详情页的 `__INITIAL_STATE__=` 与等号之间**没有空格**，正则里必须两边都允许 `\s*`。
6. 站点当日数据 `updateGapSecond=5`，白天持续滚动；历史日 T+1 定版。

---

## 七、口径

- 全站为**专资办口径**，`withSvcFee=true` 含服务费、`false` 为分账。
- 2026-11-12：含服务费 1714.11 万，分账 1548.73 万，服务费占比 **9.65%**。
- `getCinemaBoxData` **没有 `withSvcFee` 参数**。用「全量影院榜合计 ÷ 全国」反推：

  | 对比对象 | 30 天中位比值 | 判断 |
  |---|---|---|
  | 全国**含服务费** | **0.9605** | ← 更接近（差 4%） |
  | 全国**分账** | 1.0731 | 影院榜合计反而超过分账 7%，不可能 |

  → 影院榜与影院分片同属**含服务费**口径。

  但请注意：**0.96 而不是 1.00**。这 4% 的稳定缺口说明影院榜并未穷尽全国票房
  （可能有未纳入排名的影院、或直营/点播等口径差异）。做「全量面板」时不能假定
  `sum(影院榜) == 全国`，只能假定「比值稳定」。这是原方案完全没有预料到的一条。

---

## 八、24 个数据集 → 任务粒度与入库表

界面里勾选的每一项，都对应下表的一行。「任务粒度」决定请求数怎么算。

| # | 数据集 | 端点 | 任务粒度 | 入库表 | 前置 |
|---:|---|---|---|---|---|
| 01 | 可查询日期范围 | `/getDateRange` | 单次 | `fact_doc` | — |
| 02 | 全国大盘 | `/getNational` ×2 | 按日 | `fact_national` | — |
| 03 | 全国单日影片榜 | `/getDayData` | 按日 | `fact_movie_national_day` | — |
| 04 | 省份榜 | `/getProvinceList` | 按日 | `fact_province` | — |
| 05 | 院线榜 | `/getChainList` | 按日 | `fact_chain` | — |
| 06 | 影院榜（全量） | `/getCinemaList` | 按日 | `dim_cinema` + `fact_cinema_day` | — |
| 07 | 影院日报合计 | `/getCinemaSumData` | 日 × 样本 | `fact_cinema_sum` | 样本 |
| 08 | 影院×影片×日 | `/getCinemaBoxData` | 日 × 样本 | `fact_cinema_movie` + `fact_cinema_day` | 样本 |
| 09 | 影院近 14 天 | `/getCinemaManageData` | 每样本影院 1 次 | `fact_cinema_trend` | 样本 |
| 10 | 影院搜索 | `/searchCinema` | 单次 | `fact_doc` + `dim_cinema` | — |
| 11 | 院线日报合计 | `/getChainSumData` | 日 × Top院线 | `fact_chain_day` | 院线榜 |
| 12 | 院线×影片×日 | `/getChainBoxData` | 日 × Top院线 | `fact_chain_movie` | 院线榜 |
| 13 | 院线趋势 | `/getChainManageData` | 每院线 1 次 | `fact_chain_trend` | 院线榜 |
| 14 | 院线昨日对比 | `/getChainYesterday` | 每院线 1 次 | `fact_doc` | 院线榜 |
| 15 | 单片城市分布 | `/getCityData` | 日 × Top影片 | `fact_city` | 影片榜 |
| 16 | 影片搜索 | `/searchMovie` | 单次 | `fact_doc` + `dim_movie` | — |
| 17 | 影片详情页 | `/movie/detail/<code>` | 每影片 1 次 | `fact_movie_day` + `dim_movie` | 影片榜 |
| 18 | 周期榜 | `/getPeriodData` | 每个周期起始日 | `fact_period_movie` | — |
| 19 | 年度榜 | `/getYearTabs` + `/getYearData` | 每年 | `fact_year_movie` | — |
| 20 | 影片单日纪录 | `/getSingleDayData` | 单次 | `fact_history` | — |
| 21 | 影片首日纪录 | `/getReleaseDayData` | 单次 | `fact_history` | — |
| 22 | 大盘单日纪录 | `/getNationSingleDayData` | 单次 | `fact_history` | — |
| 23 | 大盘单周纪录 | `/getNationSingleWeekData` | 单次 | `fact_history` | — |
| 24 | 大盘单月纪录 | `/getNationSingleMonthData` | 单次 | `fact_history` | — |

「前置」为空表示独立可跑；标「样本」的会自动按 `--n` 建样本（必要时先补拉影院榜），
标「院线榜 / 影片榜」的会自动补拉当天榜单——所以**只勾一个下游数据集也能跑通**。

请求量速算（界面「估算」按钮同款）：

```
按日  = 天数
日×样本 = 天数 × 样本影院数
日×Top院线 = 天数 × Top院线数
日×Top影片 = 天数 × Top影片数
```


