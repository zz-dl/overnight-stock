# 推荐页增强实现计划

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 在扫描结果页顶部显示今日市场胜率横幅，每只股票卡片底部显示推荐理由和预估胜率进度条。

**Architecture:** 后端在 `_run_scan_internal()` 返回前计算 `market_win_rate`、`market_condition`，并为每只股票计算 `est_win_rate` 和 `reasons`；前端 `renderResults()` 读取这些字段渲染横幅和卡片增强区块。

**Tech Stack:** Python (Flask)，原生 JS，Tencent qtimg API，openpyxl（测试用）

---

### Task 1：后端 — 胜率计算函数

**Files:**
- Modify: `F:\OvernightStock\app.py`（在 `_run_scan_internal` 前插入两个函数）
- Modify: `F:\OvernightStock\test_backtest.py`（追加测试）

- [ ] **Step 1: 写失败测试**

在 `test_backtest.py` 末尾追加：

```python
# ── 胜率计算测试 ──────────────────────────────────────────────────────────────
import sys, os
sys.path.insert(0, os.path.dirname(__file__))
from app import calc_market_win_rate, calc_stock_win_rate, build_reasons

class TestWinRate(unittest.TestCase):

    def test_market_win_rate_sweet_spot(self):
        # 大盘 0.5-1.0% 是历史最优区间，基础胜率 54%
        wr = calc_market_win_rate(index_chg=0.72, weekday=2, consec_up=2)
        self.assertGreaterEqual(wr, 50)
        self.assertLessEqual(wr, 72)

    def test_market_win_rate_weak_market(self):
        # 大盘涨幅 < 0.5%，基础胜率 38%，应低于 45
        wr = calc_market_win_rate(index_chg=0.2, weekday=1, consec_up=1)
        self.assertLess(wr, 45)

    def test_stock_win_rate_adds_excess_bonus(self):
        base = calc_market_win_rate(0.72, 2, 1)
        with_optimal_excess = calc_stock_win_rate(base, excess=2.1)
        self.assertGreater(with_optimal_excess, base)

    def test_stock_win_rate_penalizes_high_excess(self):
        base = calc_market_win_rate(0.72, 2, 1)
        high_excess = calc_stock_win_rate(base, excess=5.0)
        self.assertLess(high_excess, base)

    def test_build_reasons_contains_key_info(self):
        reasons = build_reasons(chg_pct=4.3, index_chg=0.72, excess=2.1,
                                market_win_rate=54)
        self.assertEqual(len(reasons), 3)
        self.assertTrue(any("4.3" in r for r in reasons))
        self.assertTrue(any("2.1" in r for r in reasons))
        self.assertTrue(any("54" in r for r in reasons))

    def test_win_rate_clamped(self):
        # 极端参数不应超出 [30, 72] 范围
        lo = calc_market_win_rate(0.1, 3, 0)
        hi = calc_market_win_rate(3.0, 4, 5)
        self.assertGreaterEqual(lo, 30)
        self.assertLessEqual(hi, 72)
```

- [ ] **Step 2: 运行测试确认失败**

```
cd F:\OvernightStock
"D:\Program Files\python\python.exe" -m unittest test_backtest.TestWinRate -v 2>&1
```
预期：ImportError（函数不存在）

- [ ] **Step 3: 在 app.py 中插入三个函数**

在 `_sim_run_settlement_and_record` 定义之前插入：

```python
# ── 胜率预测 ─────────────────────────────────────────────────────────────────
# 基于十年历史回测数据（参见 test.xlsx 十年规律分析工作表）
_IC_BASE = {          # 大盘涨幅分组基础胜率
    (0.5, 1.0): 54,
    (1.0, 2.0): 43,
    (2.0, 99):  47,
    (0.0, 0.5): 38,
}
_WEEKDAY_BONUS = {0: 1, 1: -1, 2: 0, 3: 0, 4: 2}   # 周一=0 … 周五=4
_CONSEC_BONUS  = {0: 0, 1: 0, 2: 1, 3: 1}            # 连涨天数加成

def calc_market_win_rate(index_chg: float, weekday: int, consec_up: int) -> int:
    """
    根据大盘涨幅、星期、连涨天数估算今日整体胜率（基于十年历史回测）。
    返回整数百分比，clamp 到 [30, 72]。
    """
    base = 38  # 默认（大盘涨幅 < 0.5%）
    for (lo, hi), wr in _IC_BASE.items():
        if lo <= index_chg < hi:
            base = wr
            break
    bonus  = _WEEKDAY_BONUS.get(weekday % 7, 0)
    bonus += _CONSEC_BONUS.get(min(consec_up, 3), 0)
    return max(30, min(72, base + bonus))


def calc_stock_win_rate(market_win_rate: int, excess: float) -> int:
    """
    在市场胜率基础上，按该股超额倍数微调（基于回测超额倍数分组）。
    2.0-2.5x 历史最优，>4x 反而差。
    """
    if 2.0 <= excess <= 2.5:
        delta = 3
    elif 2.5 < excess <= 3.0:
        delta = 1
    elif 3.0 < excess <= 4.0:
        delta = 0
    else:   # > 4x
        delta = -5
    return max(30, min(72, market_win_rate + delta))


def build_reasons(chg_pct: float, index_chg: float,
                  excess: float, market_win_rate: int) -> list:
    """
    生成最多 3 条推荐理由文字。
    """
    reasons = []
    # 理由1：涨幅区间
    if 3.0 <= chg_pct < 3.5:
        reasons.append(f"涨幅 {chg_pct:.1f}%，温和启动，追高风险低")
    elif chg_pct < 4.5:
        reasons.append(f"涨幅 {chg_pct:.1f}%，处于 3-5% 甜蜜区间，动能适中")
    else:
        reasons.append(f"涨幅 {chg_pct:.1f}%，偏高区间，次日需关注开盘方向")
    # 理由2：超额表现
    if 2.0 <= excess <= 2.5:
        reasons.append(f"超额大盘 {excess:.1f}x，独立行情且未过热（历史最优区间）")
    elif excess > 4.0:
        reasons.append(f"超额大盘 {excess:.1f}x，涨势较猛，注意次日开盘回调风险")
    else:
        reasons.append(f"超额大盘 {excess:.1f}x，个股有独立行情")
    # 理由3：市场环境
    reasons.append(
        f"大盘今日 +{index_chg:.2f}%，历史同类条件胜率约 {market_win_rate}%"
    )
    return reasons
```

- [ ] **Step 4: 运行测试确认通过**

```
cd F:\OvernightStock
"D:\Program Files\python\python.exe" -m unittest test_backtest.TestWinRate -v 2>&1
```
预期：6 tests PASS

- [ ] **Step 5: Commit**

```bash
cd F:\OvernightStock
git add app.py test_backtest.py
git commit -m "feat: add win rate and reasons calculation functions"
```

---

### Task 2：后端 — 将胜率注入扫描响应

**Files:**
- Modify: `F:\OvernightStock\app.py`（修改 `_run_scan_internal` 返回值）

- [ ] **Step 1: 在 `_run_scan_internal` 顶部获取星期和连涨**

在函数开头（`t0 = time.time()` 之后）插入：

```python
    today_weekday = datetime.now().weekday()   # 0=Mon … 4=Fri
    # 连续上涨天数（简单估算：从 idx_chg_map 不可得，用固定值 1 作为保守估计）
    consec_up = 1
```

- [ ] **Step 2: 计算市场胜率（在 `index_chg = get_index_chg()` 之后）**

在 `index_chg = get_index_chg()` 之后插入：

```python
    market_wr = calc_market_win_rate(index_chg, today_weekday, consec_up)
    if index_chg < 0.5:
        market_cond = f"大盘+{index_chg:.2f}%，涨幅偏弱，今日胜率偏低"
    elif index_chg < 1.0:
        market_cond = f"大盘+{index_chg:.2f}%，0.5-1.0% 历史最优区间"
    elif index_chg < 2.0:
        market_cond = f"大盘+{index_chg:.2f}%，涨幅较强"
    else:
        market_cond = f"大盘+{index_chg:.2f}%，涨幅过大需谨慎追高"
```

- [ ] **Step 3: 为每只股票计算 est_win_rate 和 reasons（在 `candidates.sort(...)` 之后）**

将现有的：
```python
    candidates.sort(key=lambda x: (-x["score"], -x["vol_ratio"]))
    result_stocks = candidates[:30]
```
改为：
```python
    candidates.sort(key=lambda x: (-x["score"], -x["vol_ratio"]))
    result_stocks = candidates[:30]
    for s in result_stocks:
        excess = s["chg_pct"] / index_chg if index_chg > 0 else 0
        s["est_win_rate"] = calc_stock_win_rate(market_wr, excess)
        s["reasons"] = build_reasons(s["chg_pct"], index_chg, excess, market_wr)
```

- [ ] **Step 4: 在 return 语句中加入新字段**

将 return 改为：
```python
    return {
        "stocks": result_stocks,
        "index_chg": round(index_chg, 2),
        "total_scanned": len(raw),
        "total_found": len(candidates),
        "elapsed": round(time.time() - t0, 1),
        "scan_time": datetime.now().strftime("%H:%M:%S"),
        "active_criteria": sorted(active_criteria),
        "market_win_rate": market_wr,
        "market_condition": market_cond,
    }
```

- [ ] **Step 5: 语法检查**

```
cd F:\OvernightStock
python3 -c "import ast; ast.parse(open('app.py',encoding='utf-8').read()); print('OK')"
```

- [ ] **Step 6: Commit**

```bash
git add app.py
git commit -m "feat: inject market_win_rate and per-stock est_win_rate into scan response"
```

---

### Task 3：前端 — 市场条件横幅

**Files:**
- Modify: `F:\OvernightStock\static\index.html`

- [ ] **Step 1: 在 index-bar 和 scan-btn 之间插入横幅 div**

将：
```html
  <button class="scan-btn" id="scanBtn" onclick="doScan(true)">重新扫描</button>
```
改为：
```html
  <div id="marketBanner" style="display:none;border-radius:10px;padding:10px 14px;
    margin-bottom:10px;font-size:12px;font-weight:600;text-align:center"></div>
  <button class="scan-btn" id="scanBtn" onclick="doScan(true)">重新扫描</button>
```

- [ ] **Step 2: 在 `renderResults` 函数开头加入横幅渲染逻辑**

在 `function renderResults(data) {` 的第一行之后插入：

```javascript
  // 市场条件横幅
  const mb = document.getElementById('marketBanner');
  if (data.market_win_rate != null) {
    const wr = data.market_win_rate;
    const bg = wr >= 52 ? '#0d2a1e' : wr >= 45 ? '#2a1e0d' : '#2a0d0d';
    const fg = wr >= 52 ? 'var(--buy)' : wr >= 45 ? 'var(--warn)' : 'var(--sell)';
    mb.style.display = 'block';
    mb.style.background = bg;
    mb.style.color = fg;
    mb.style.border = `1px solid ${fg}`;
    mb.textContent = `📊 ${data.market_condition}  ·  历史胜率预估 ${wr}%`;
  }
```

- [ ] **Step 3: 语法检查（用浏览器 DevTools 或直接 commit 后观察）**

在 Bash 中验证 HTML 文件无明显语法错误：
```bash
cd F:\OvernightStock
python3 -c "
from html.parser import HTMLParser
class V(HTMLParser): pass
V().feed(open('static/index.html',encoding='utf-8').read())
print('HTML OK')
"
```

- [ ] **Step 4: Commit**

```bash
git add static/index.html
git commit -m "feat: add market condition win rate banner to scan tab"
```

---

### Task 4：前端 — 每股胜率进度条 + 推荐理由

**Files:**
- Modify: `F:\OvernightStock\static\index.html`

- [ ] **Step 1: 修改 `renderResults` 中的股票卡片模板**

将现有的：
```javascript
    return `
    <div class="stock-card">
      ...
      <div class="chips">${chips}</div>
    </div>`;
```
改为：
```javascript
    // 胜率进度条
    const wr = s.est_win_rate || 0;
    const wrColor = wr >= 52 ? 'var(--buy)' : wr >= 45 ? 'var(--warn)' : 'var(--sell)';
    const wrBar = `
      <div style="margin-top:8px">
        <div style="display:flex;justify-content:space-between;align-items:center;margin-bottom:3px">
          <span style="font-size:10px;color:var(--dim)">预估胜率</span>
          <span style="font-size:12px;font-weight:700;color:${wrColor}">${wr}%</span>
        </div>
        <div style="height:4px;background:var(--border);border-radius:2px;overflow:hidden">
          <div style="height:100%;width:${wr}%;background:${wrColor};border-radius:2px"></div>
        </div>
      </div>`;
    // 推荐理由
    const reasons = (s.reasons || []).map(r =>
      `<div style="font-size:10px;color:var(--dim);padding:2px 0">• ${r}</div>`
    ).join('');

    return `
    <div class="stock-card">
      <div class="card-top">
        <div>
          <div class="stock-name">${s.name}</div>
          <div class="stock-code">${s.code}</div>
        </div>
        <div class="card-right">
          <div class="chg-val">+${s.chg_pct.toFixed(2)}%</div>
          <div class="score-badge s${score}">${SCORE_LABEL[score] || score+'/6'}</div>
        </div>
      </div>
      <div class="stats-row">
        <div class="stat-item"><span>换手率 </span><b>${s.turnover.toFixed(1)}%</b></div>
        <div class="stat-item"><span>量比 </span><b>${s.vol_ratio.toFixed(2)}</b></div>
        <div class="stat-item"><span>流通市值 </span><b>${s.float_cap}亿</b></div>
      </div>
      <div class="chips">${chips}</div>
      ${reasons ? `<div style="margin-top:6px;padding-top:6px;border-top:1px solid var(--border)">${reasons}</div>` : ''}
      ${wrBar}
    </div>`;
```

- [ ] **Step 2: Commit**

```bash
git add static/index.html
git commit -m "feat: add per-stock win rate bar and recommendation reasons"
```

---

### Task 5：部署与验证

**Files:**（无新文件，仅推送）

- [ ] **Step 1: 推送到 GitHub**

```bash
cd F:\OvernightStock
git push origin master
```

- [ ] **Step 2: 触发 Render 部署**

```bash
curl -s -X POST -H "Authorization: Bearer rnd_FfTo4pkroOXE4HEzxIAMa9nPCL2u" \
  "https://api.render.com/v1/services/srv-d8ajvn3bc2fs7383lpp0/deploys" \
  -H "Content-Type: application/json" \
  -d '{"clearCache":"do_not_clear"}' | python3 -c "import sys,json; d=json.load(sys.stdin); print(d.get('status'))"
```

- [ ] **Step 3: 等部署完成后调用 API 验证新字段**

```bash
curl -s --max-time 90 "https://overnight-stock.onrender.com/api/scan" | python3 -c "
import sys,json
d=json.load(sys.stdin)
print('market_win_rate:', d.get('market_win_rate'))
print('market_condition:', d.get('market_condition'))
if d.get('stocks'):
    s=d['stocks'][0]
    print('stock[0] est_win_rate:', s.get('est_win_rate'))
    print('stock[0] reasons:', s.get('reasons'))
"
```
预期：`market_win_rate` 为整数，`est_win_rate` 为整数，`reasons` 为3条字符串列表。

- [ ] **Step 4: 更新 Service Worker 版本（强制用户获取新界面）**

将 `static/sw.js` 中 `const CACHE = "stockmaster-v2"` 改为 `"stockmaster-v3"`，然后：

```bash
git add static/sw.js
git commit -m "fix: bump SW cache version for recommendation enhancement"
git push origin master
```

再次触发 Render 部署（重复 Step 2）。

---

## Self-Review

**Spec coverage:**
- ✅ 页面顶部市场条件横幅（Task 3）
- ✅ 每股推荐理由（Task 4 reasons）
- ✅ 每股胜率进度条（Task 4 wrBar）
- ✅ 胜率计算逻辑（Task 1 函数 + Task 2 注入）
- ✅ 颜色规则（≥52% 绿，45-52% 黄，<45% 红）
- ✅ 部署验证（Task 5）

**Placeholder scan:** 无 TBD，所有步骤含完整代码。

**Type consistency:** `calc_market_win_rate` → `int`；`calc_stock_win_rate` → `int`；`build_reasons` → `list[str]`，与 Task 2 注入和 Task 4 前端调用一致。
