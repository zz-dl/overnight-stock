# 一夜持股法模拟交易 — 设计规格

**日期：** 2026-06-01  
**项目：** F:\OvernightStock  

## 目标

每次扫描后记录 6/7+ 股票为模拟买入，下次扫描时用当前价格结算卖出，分析七大标准的有效性，自动停用滚动30笔胜率 < 40% 的标准。

---

## 数据存储

GitHub API 写入 `zz-dl/overnight-stock` 仓库 `sim_data/` 目录（branch: master）：

| 文件 | 内容 |
|------|------|
| `sim_data/pending.json` | 当前未结算买入（最新一次扫描的 6/7+ 候选） |
| `sim_data/trades.json` | 已结算交易历史（滚动最近 90 笔） |
| `sim_data/criteria_stats.json` | 每条标准的滚动30笔胜率 + 活跃状态 |

### pending.json 结构
```json
{
  "date": "2026-06-01",
  "scan_time": "14:52:10",
  "positions": [
    {
      "code": "000001", "name": "平安银行", "price": 10.5,
      "criteria": {"chg":true,"turnover":true,"vol_ratio":true,
                   "cap":true,"limit_gene":false,"vwap":true,"stronger":true},
      "score": 6
    }
  ]
}
```

### trades.json 结构
每条 pending position + `sell_price`、`sell_date`、`return_pct`、`holding_days`。

### criteria_stats.json 结构
```json
{
  "active_criteria": ["chg","turnover","vol_ratio","cap","limit_gene","vwap","stronger"],
  "stats": {
    "chg": {"wins": 12, "losses": 8, "win_rate": 0.6, "active": true},
    ...
  }
}
```

---

## 后端逻辑（app.py）

### GitHub 辅助函数
- `gh_read(path)` → `(data, sha)`：先 GitHub API，Render 无本地文件
- `gh_write(path, data, sha)` → bool

### 扫描流程（在 `/api/scan` 和 `/api/scan/force` 中）

```
1. gh_read pending.json
2. 若 pending 存在且日期 ≠ 今天：
     a. 用腾讯 API 批量查当前价格
     b. 计算每只股票 return_pct
     c. 追加到 trades.json（保留最近90笔）
     d. 更新 criteria_stats.json（每条标准的胜负统计）
     e. 自动停用/恢复标准（见标准管理规则）
     f. 清空 pending.json
3. 读取 active_criteria
4. 用活跃标准运行扫描（非活跃标准视为"未通过"）
5. 筛选 score >= 6（基于活跃标准总数）
6. 将 6/7+ 候选写入 pending.json（附当前价格和标准详情）
```

### 标准管理规则
- 窗口：每条标准最近30笔包含它的交易
- 停用：win_rate < 0.40
- 恢复：win_rate ≥ 0.50
- 安全底线：始终保留至少3条活跃标准（优先保留历史胜率最高的）

### 新增 API
- `GET /api/sim` → 返回 pending + trades(最近20笔) + criteria_stats

---

## 前端（index.html）

新增第三个 Tab **"复盘"**，三个区块：

1. **当前模拟仓**：今日买入的股票列表（代码、名称、买入价、通过标准）
2. **最近交易记录**：最近10笔，每行显示日期、股票、收益%、胜/负
3. **标准胜率分析**：7条标准的胜率柱状图（绿色≥50%、红色<40%、橙色40-50%）；已停用标准显示橙色警告

---

## 错误处理

- GitHub API 失败：静默跳过，不影响正常扫描
- 无法获取卖出价格：跳过该股票，不计入统计
- pending 日期是非交易日（周末）：仍正常结算，以下次开盘价为准

---

## 不在范围内

- 精确的 10:00 开盘价（用当前价格近似）
- 复杂的算法参数自动调整（仅停用/恢复标准）
- 历史回测
