# Demo 数据与测试 Query

这组数据用于验证从异构原始数据、字段映射、统一事实表，到联表分析和图表生成的完整链路。所有问题都可以直接粘贴到 **Analyze** 页面；参考 SQL 用于核对 Text-to-SQL 结果，不要求模型逐字生成相同语句。

## 数据集清单

| 原始文件 | 行数 | 目标表 / 用途 | 主要差异 |
| --- | ---: | --- | --- |
| `vendor-a-results.csv` | 350 | `fact_test_results` 输入 | 标准 CSV，`sample_no`、`test_item`、`cost` |
| `bluepeak-q2.xlsx` | 300 | `fact_test_results` 输入 | 供应商别名字段，`OK/NG` 需要映射为 `PASS/FAIL` |
| `slack-lab-updates.txt` | 42 条消息 | `fact_test_results` 输入 | 非结构化 Slack 文本，需要实体和字段抽取 |
| `vendor-contracts.csv` | 6 | `dim_vendor_contracts` | 合同单价、SLA、质量目标 |
| `project-budgets.xlsx` | 10 | `dim_project_budgets` | 项目负责人、优先级、批准预算 |
| `material-quality-targets.csv` | 5 | `dim_material_standards` | 材料族、失败率目标、平均成本上限和风险等级 |

## 基础聚合

### 1. 供应商综合表现

测试问题：

> 按供应商比较检测数量、总成本、通过率和平均周转天数，按总成本从高到低排序。

```sql
SELECT
  vendor,
  count(*) AS test_count,
  round(sum(cost_usd), 2) AS total_cost_usd,
  round(100.0 * avg(CASE WHEN result = 'PASS' THEN 1 ELSE 0 END), 2) AS pass_rate_pct,
  round(avg(turnaround_days), 2) AS avg_turnaround_days
FROM fact_test_results
GROUP BY vendor
ORDER BY total_cost_usd DESC;
```

### 2. 月度成本趋势

测试问题：

> 展示每个月的检测总成本趋势，并指出成本最高的月份。

```sql
SELECT
  strftime(submitted_date, '%Y-%m') AS month,
  round(sum(cost_usd), 2) AS total_cost_usd
FROM fact_test_results
GROUP BY month
ORDER BY month;
```

### 3. 材料质量风险

测试问题：

> 哪些材料的失败率最高？至少有 20 次检测才纳入比较。

```sql
SELECT
  material,
  count(*) AS test_count,
  round(100.0 * avg(CASE WHEN result = 'FAIL' THEN 1 ELSE 0 END), 2) AS failure_rate_pct
FROM fact_test_results
GROUP BY material
HAVING count(*) >= 20
ORDER BY failure_rate_pct DESC;
```

## 合同联表分析

### 4. 实际单价与合同单价偏差

测试问题：

> 哪些供应商的实际平均检测成本超过合同单价？给出差额并按超支最多排序。

```sql
WITH actual AS (
  SELECT vendor, round(avg(cost_usd), 2) AS actual_avg_cost_usd
  FROM fact_test_results
  GROUP BY vendor
)
SELECT
  a.vendor,
  a.actual_avg_cost_usd,
  c.contracted_cost_usd,
  round(a.actual_avg_cost_usd - c.contracted_cost_usd, 2) AS cost_variance_usd
FROM actual a
JOIN dim_vendor_contracts c USING (vendor)
WHERE a.actual_avg_cost_usd > c.contracted_cost_usd
ORDER BY cost_variance_usd DESC;
```

### 5. SLA 履约

测试问题：

> 比较各供应商的实际平均周转天数和合同 SLA，找出超出 SLA 的供应商。

```sql
SELECT
  f.vendor,
  round(avg(f.turnaround_days), 2) AS actual_turnaround_days,
  c.sla_days,
  round(avg(f.turnaround_days) - c.sla_days, 2) AS sla_variance_days
FROM fact_test_results f
JOIN dim_vendor_contracts c USING (vendor)
GROUP BY f.vendor, c.sla_days
HAVING avg(f.turnaround_days) > c.sla_days
ORDER BY sla_variance_days DESC;
```

### 6. 质量目标达成

测试问题：

> 哪些供应商没有达到合同约定的质量目标？显示实际通过率和目标通过率。

```sql
SELECT
  f.vendor,
  round(100.0 * avg(CASE WHEN f.result = 'PASS' THEN 1 ELSE 0 END), 2) AS actual_pass_rate_pct,
  round(100.0 * c.quality_target_pct, 2) AS target_pass_rate_pct
FROM fact_test_results f
JOIN dim_vendor_contracts c USING (vendor)
GROUP BY f.vendor, c.quality_target_pct
HAVING avg(CASE WHEN f.result = 'PASS' THEN 1 ELSE 0 END) < c.quality_target_pct
ORDER BY actual_pass_rate_pct;
```

## 项目预算联表分析

### 7. 项目预算消耗

测试问题：

> 每个项目已经花了多少检测费用，占批准预算的百分比是多少？按预算消耗率从高到低排序。

```sql
WITH spend AS (
  SELECT project, round(sum(cost_usd), 2) AS actual_spend_usd
  FROM fact_test_results
  GROUP BY project
)
SELECT
  s.project,
  b.owner,
  b.priority,
  s.actual_spend_usd,
  b.approved_budget_usd,
  round(100.0 * s.actual_spend_usd / b.approved_budget_usd, 2) AS budget_burn_pct
FROM spend s
JOIN dim_project_budgets b USING (project)
ORDER BY budget_burn_pct DESC;
```

### 8. 超预算项目

测试问题：

> 哪些项目已经超预算？显示负责人、优先级和超支金额。

```sql
SELECT
  f.project,
  b.owner,
  b.priority,
  round(sum(f.cost_usd) - b.approved_budget_usd, 2) AS overspend_usd
FROM fact_test_results f
JOIN dim_project_budgets b USING (project)
GROUP BY f.project, b.owner, b.priority, b.approved_budget_usd
HAVING sum(f.cost_usd) > b.approved_budget_usd
ORDER BY overspend_usd DESC;
```

### 9. 高优先级项目质量风险

测试问题：

> 高优先级项目中，哪个项目失败率最高？同时显示项目负责人。

```sql
SELECT
  f.project,
  b.owner,
  round(100.0 * avg(CASE WHEN f.result = 'FAIL' THEN 1 ELSE 0 END), 2) AS failure_rate_pct
FROM fact_test_results f
JOIN dim_project_budgets b USING (project)
WHERE b.priority = 'High'
GROUP BY f.project, b.owner
ORDER BY failure_rate_pct DESC;
```

## 材料标准联表分析

### 10. 材料质量与成本目标偏差

测试问题：

> 按材料比较实际失败率与目标失败率，并列出失败率超标或实际平均成本超过成本上限的材料，同时显示材料族和风险等级。

```sql
SELECT
  material,
  material_family,
  risk_tier,
  round(100.0 * avg(CASE WHEN result = 'FAIL' THEN 1 ELSE 0 END), 2) AS actual_failure_pct,
  target_failure_pct,
  round(avg(cost_usd), 2) AS actual_avg_cost_usd,
  max_avg_cost_usd
FROM vw_laboratory_analysis
GROUP BY material, material_family, risk_tier, target_failure_pct, max_avg_cost_usd
HAVING actual_failure_pct > target_failure_pct
    OR actual_avg_cost_usd > max_avg_cost_usd
ORDER BY actual_failure_pct - target_failure_pct DESC;
```

## 澄清与安全边界

### 11. 非数据问题

测试问题：

> 你好，你能做什么？

预期行为：只返回简短能力说明或引导用户提出数据问题，不执行 SQL，也不展示 KPI、空表或空图表占位组件。

### 12. 信息不足

测试问题：

> 帮我看看哪个最好。

预期行为：要求用户澄清“最好”指成本、通过率、周转速度还是合同履约，不执行 SQL。

### 13. 安全拦截

测试输入：

```sql
DROP TABLE fact_test_results;
```

预期行为：SQLGuard 拒绝执行；仅允许访问已批准的分析表和 `vw_laboratory_analysis` 的单条只读查询。
