# Cookie 巡检页 · 研发实施计划

- 日期：2026-10-28
- 前置文档：[Cookie巡检页与UI升级设计文档](Cookie巡检页与UI升级设计文档.md)（能力全景、探针方案、视觉规范的"为什么"都在那边，本文只讲"怎么做"）
- 分 6 个阶段（0-5），每阶段可独立验收、独立提交，**阶段②结束即可用**（字段校验+矩阵先行，真探针后置）

---

## 阶段 0 · 数据层与常量定义（半天）

> 目标：不动任何页面，先把"巡检需要的数据"准备好。本阶段无 UI，纯后端。

### 0.1 新表：巡检记录

`src/db.py` 的 `SCHEMA` 字符串中追加（项目用 `CREATE TABLE IF NOT EXISTS`，SQLite 无迁移负担，直接加 DDL 即可）：

```sql
CREATE TABLE IF NOT EXISTS cookie_probe_records (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    platform_key TEXT NOT NULL,          -- 对应 PLATFORM_COOKIE_ALIASES 的键（douyin/yuanbao/...）
    status TEXT NOT NULL,                -- not_configured/incomplete/healthy/stale/probe_failed
    trigger_type TEXT NOT NULL,          -- manual/passive/auto/field_check
    detail TEXT,                         -- JSON：判定信号、HTTP 状态、耗时、增益对照结果
    duration_ms INTEGER,
    probed_at TEXT NOT NULL              -- 北京时间 ISO，与 request_logs 同风格
);
CREATE INDEX IF NOT EXISTS idx_probe_platform_time
    ON cookie_probe_records(platform_key, probed_at DESC);
```

### 0.2 新模块 `src/utils/cookie_registry.py`（巡检页的单一事实源）

把散落在 docs 里的能力门控知识**代码化**，后续页面、探针、文档都从这里读：

```python
# 每个平台一条注册项
COOKIE_REGISTRY = {
    "douyin": {
        "platform_name": "抖音",
        "env_keys": ["DOUYIN_COOKIE", "DY_COOKIE"],          # 复用 cookie_manager.PLATFORM_COOKIE_ALIASES
        "required_fields": ["sessionid_ss", "passport_csrf_token", "odin_tt"],
        "optional_fields": ["UIFID"],                         # 或独立环境变量 DOUYIN_UIFID
        "severity": "critical",          # critical(缺失=能力消失) / enhanced(降质) / fallback(兜底)
        "capabilities": [                # 覆盖矩阵的"能力"列
            {"name": "视频", "needs_cookie": False},
            {"name": "图文", "needs_cookie": False},
            {"name": "实况动轨", "needs_cookie": True},
            {"name": "放映厅/短剧", "needs_cookie": True},
        ],
        "canary_url_setting": "canary_douyin",   # 金丝雀链接存 system_settings，页面可改
        "passive_error_codes": ["COOKIE_REQUIRED"],  # 被动巡检关注的错误码前缀
    },
    "yuanbao":   {...},   # severity: critical（整平台）
    "pinduoduo": {...},   # severity: critical（多多视频）
    "doubao":    {...},   # enhanced
    "xiaohongshu": {...}, # fallback
    "kuaishou":  {...},   # fallback
    "weibo":     {...},   # enhanced
    "jimeng":    {...},   # enhanced
}
```

### 0.3 字段校验器（零网络，保存即反馈）

```python
def check_cookie_fields(platform_key: str, cookie: str) -> dict:
    """解析 Cookie 字符串，返回 {status, missing_fields, detail}。
    status: incomplete(缺必需字段) / ok(字段齐) —— 不发任何网络请求。"""
```

要点：
- 复用 `clean_cookie_quotes` 处理引号；用 `http.cookies.SimpleCookie` 容错解析（解析失败的片段跳过而不是报错）
- 抖音特例：`UIFID` 允许来自 Cookie、`DOUYIN_UIFID` 环境变量、或 256 位裸串三种来源（[douyin_parser.py:196-225](../src/parsers/douyin_parser.py#L196) 已有此逻辑，抽出来复用而不是复制）
- **在现有保存流程里挂上**：[admin.py](../src/web/admin.py) settings 保存分支中，凡是 `cookie_*` 键，保存后立刻跑 `check_cookie_fields`，把结果写一条 `trigger_type='field_check'` 的探针记录。这样用户保存的瞬间，巡检页数据已更新。

### 验收
- [ ] 启动后 `cookie_probe_records` 表自动创建
- [ ] 后台保存一个缺 `sessionid_ss` 的抖音 Cookie → 表里出现 `incomplete` 记录
- [ ] `python -m pytest tests/test_cookie_manager.py` 仍全绿（不破坏现有逻辑），新增注册表与字段校验的单测

---

## 阶段 1 · 管理台质感层校准（半天，可与阶段 0 并行）

> 目标：设计文档"质感层"5 条里的 1、2、5 条落地。这一阶段做完，全管理台观感提升，且是阶段②画面的地基。

### 1.1 `console.css` 增加 token 层（文件顶部统一声明）

```css
:root {
  /* 文字三层（全部 ≥4.5:1） */
  --ink: #111111; --ink-muted: #5f6368; --ink-faint: #6b6b6b;
  /* 边线三阶 */
  --line-card: #e8e8e8; --line-row: #f2f2f2; --line-control: #d1d5db;
  /* 状态 pill 三组（设计文档 §2.5 的色值） */
  --pill-ok-bg: #dcfae5;    --pill-ok-ink: #04250e;
  --pill-bad-bg: #ffedea;   --pill-bad-ink: #831c1e;
  --pill-warn-bg: #fffbeb;  --pill-warn-ink: #92400e;
  /* 反馈动效 */
  --ease-out: cubic-bezier(0.16, 1, 0.3, 1);
}
.num { font-variant-numeric: tabular-nums; }
```

### 1.2 字阶收敛

管理台现有模板中的字号审计后收敛为 4 级（`text-xs`→12、`text-sm`→14、`text-base`→16、`text-2xl`→24）。**只改 class，不动结构**：用全局搜索把 `text-xl`、`text-lg`、`text-3xl` 归并到最近档位。逐文件过 `templates/admin/*.html`（8 个文件）。

### 1.3 Lucide 图标 sprite

- 写 `scripts/build_icon_sprite.py`（构建期脚本，不入运行时）：从 lucide 官方 SVG 集挑选管理台用到的约 30 个图标，合并输出 `static/icons/lucide.svg`（symbol + id 格式）
- `base_console.html` 末尾内联引入一次，模板中 `<svg class="size-4"><use href="#i-settings"/></svg>`
- stroke 1.5px、`currentColor`，替换导航与卡片标题处的现有图标
- ⚠️ 只换图标不换布局；`static/icons.svg`（旧 favicon 类）不动

### 1.4 反馈动效基建（写入 console.css，全管理台共用）

```css
/* 行 hover：1px 轮廓外扩（transform/opacity only） */
.row-hover { transition: box-shadow .15s var(--ease-out); }
.row-hover:hover { box-shadow: 0 0 0 1px var(--line-control); border-radius: 6px; }
/* 骨架：扁平灰条，保持行高，无 shimmer */
@keyframes skeleton-pulse { 50% { opacity: .55; } }
.skeleton { background: #e5e7eb; border-radius: 4px; animation: skeleton-pulse 1.6s ease-in-out infinite; }
@media (prefers-reduced-motion: reduce) {
  .row-hover, .skeleton { transition: none; animation: none; }
}
```

### 验收
- [ ] `grep -rn "text-xl\|text-lg\|text-3xl" templates/admin/` 结果为 0（或逐条有保留理由）
- [ ] 三个宽度（1440/768/390）截图对比无布局破坏
- [ ] 所有数字列/统计位带 `.num`

---

## 阶段 2 · 巡检页骨架 + overview API（1 天）

> 目标：页面可看、可导航，零网络探针数据全量呈现。**本阶段结束巡检页就是"可用状态"**。

### 2.1 后端路由（`src/web/admin.py` 追加，跟随现有 `@bp.get(..., endpoint=...)` + `@admin_required` 风格）

```
GET /admin/cookies          → 渲染 templates/admin/cookies.html（endpoint="cookies"）
GET /admin/api/cookies/overview   → JSON，供页面轮询/刷新
POST /admin/api/cookies/field-check  → 对单平台重跑字段校验
```

### 2.2 `overview` 响应契约（前端只消费这一个接口）

```json
{
  "succ": true,
  "data": {
    "generated_at": "2026-10-28 14:30:00",
    "summary": { "healthy": 4, "stale": 1, "not_configured": 2, "enhanced": 2 },
    "finding": "抖音实况已降级 6 天",            // one finding：按 severity 排序取第一条异常
    "items": [{
      "platform_key": "douyin",
      "platform_name": "抖音",
      "severity": "critical",
      "source": "database",                     // database / env / none
      "status": "stale",                        // 阶段②恒为 not_configured/healthy(字段齐)/incomplete
      "capabilities": [{"name": "实况动轨", "needs_cookie": true, "active": false}, ...],
      "field_check": {"missing": ["UIFID"]},
      "last_probe": {"status": "stale", "probed_at": "...", "detail": "..."},
      "passive_24h": {"total": 12, "cookie_required": 3},   // 见 2.3
      "canary_url": null
    }]
  }
}
```

### 2.3 被动巡检查询（阶段②就接入，成本≈0）

```sql
SELECT COUNT(*) AS total,
       SUM(CASE WHEN error_code LIKE '%COOKIE_REQUIRED%' THEN 1 ELSE 0 END) AS cookie_required
FROM request_logs
WHERE platform = ? AND created_at >= datetime('now', '-1 day')
```

`request_logs` 已有 `idx_logs_created_at` 索引（[db.py:59](../src/db.py#L59)）。规则：`cookie_required ≥ 3 且占比 > 50%` → 该平台被动标记"疑似失效"，写入一条 `trigger_type='passive'` 记录（**去重**：同平台同日只写一条）。

### 2.4 页面 `templates/admin/cookies.html`

按设计文档 §2.2 线框实现，复用 `base_console.html`，`active_nav='cookies'`。区块自上而下：

1. **标题行**：h1「Cookie 凭据巡检」24px + freshness stamp（数据取 `generated_at` 与最近探针时间）+ 主按钮「全部重新巡检」（近黑 #1d1f20，阶段②先只触发全平台字段校验）
2. **状态总览**：一个容器 4 格 hairline 分隔，值 24px `.num`，标签 12px
3. **明细矩阵**：表头 `平台 / 来源 / 覆盖能力 / 状态 / 最近验证 / 操作`；行 48px；状态 pill 三组色；覆盖能力列 `视频 ✓ · 图文 ✓ · 实况 ✗` 文字化（needs_cookie 且 inactive 的能力是视觉重点）
4. **排序**：severity（critical→enhanced→fallback）→ 状态（异常在前）
5. **巡检历史**：阶段②先留区块框架 + "暂无探针记录" 空态（保留框架和标题的一句话空态，不是空盒子）

导航项插在「平台开关」与「调用日志」之间（`base_console.html` 管理台 nav 区）。

### 验收
- [ ] 无 Cookie 配置的全新部署：页面显示 2 个"未配置强制项"置顶 + 副行 one finding 正确
- [ ] 配好视频号 Cookie 保存 → 无需刷新，重进页面状态变"字段齐/healthy"
- [ ] 390px 宽下矩阵退化为双行列表（第一行平台+状态，第二行能力+操作），无横向滚动
- [ ] `tests/test_management.py` 风格新增 `tests/test_cookie_overview.py`

---

## 阶段 3 · 真实探针：双跑对照法（1-2 天，核心阶段）

> 目标：阶段②的"字段齐"升级为"真的能用"。这是本功能的技术核心。

### 3.1 探针执行器 `src/utils/cookie_prober.py`

```python
def probe_platform(platform_key: str, with_cookie: bool) -> dict:
    """跑一次金丝雀解析。with_cookie=False 时临时屏蔽 cookie_{platform} 设置。
    返回 {ok, signal, duration_ms, detail}"""
```

实现要点：
- **复用解析链路**：直接调 `ParserFactory.create_parser` + `_fetch_with_retry` 同款取数逻辑（从 [parse.py](../src/api/parse.py) 提取公共函数，或最小化：探针内自建 parser 实例取 `image_list/video_url`）。**不要**走 HTTP 自调用（避免占用 worker 与限流额度）
- **强制无 Cookie 的实现**：`get_platform_cookie` 优先读 DB —— 探针跑"无 Cookie"分支时用一个 context manager `patched_settings(cookie_{key}=None)` 临时遮蔽即可，不必改 cookie_manager
- **金丝雀链接**：从 `system_settings[canary_{platform}]` 读；为空时用内置默认（抖音用测试集实况链接、豆包/小红书等用稳定热门分享链），页面可改
- **超时 60s**，探针全程打 `logger.info`，失败不抛异常只返回结构化结果

### 3.2 判定矩阵（双跑对照）

| 带 Cookie | 无 Cookie | 判定 | detail 示例 |
|---|---|---|---|
| ✅ 成功 | ✅ 成功 | healthy（增益型平台标 enhanced-active） | "两路均成功" |
| ✅ 成功 | ❌ 失败/降级 | **healthy**（增益明确） | "Cookie 增益：无 Cookie 时实况缺失" |
| ❌ 失败 | ✅ 成功 | **stale**（失效实锤） | "Cookie 反而失败 → 凭据已失效" |
| ❌ 失败 | ❌ 失败 | probe_failed（链接/网络问题，不怪 Cookie） | "金丝雀可能已失效，请更换" |
| ✅ 成功 | ✅ 成功但两者无差异 | healthy（该链接无法体现增益，提示可换金丝雀） | "建议更换为能体现差异的样例" |

平台特化信号（写入 detail）：
- 抖音：以 `live_photo_url` 是否存在为主信号（对应判据见 [douyin.md](../docs/parsers/douyin.md) Cookie 依赖节）
- 豆包：以是否拿到原画（非水印预览）为主信号
- 视频号/拼多多：错误码命中 `*_COOKIE_REQUIRED` 即 stale
- 小红书：302 → `/login` 即 stale

### 3.3 探针 API 与防抖

```
POST /admin/api/cookies/probe    body {platform} → 同步执行（60s 上限），写记录，返回结果
POST /admin/api/cookies/probe-all → 串行跑全平台，平台间 sleep 2s（降低风控压力）
POST /admin/api/cookies/canary   body {platform, url} → 保存金丝雀（校验 URL 归属平台域名）
```

防抖：同平台 5 分钟内的探针直接返回最近记录（`probed_at` 判断），避免管理员连点狂刷触发平台风控。

### 3.4 自动巡检（轻量守护线程）

- `create_app` 时启动 daemon 线程（沿用 `ProxyManager.start()` 的线程模式），每 6 小时一轮：**只做字段校验 + 被动信号查询**，不做真探针（保护风控配额）
- 结果写 `trigger_type='auto'` 记录

### 验收
- [ ] 用一个故意改坏的抖音 Cookie：探针返回 stale，detail 含对照说明
- [ ] 用有效 Cookie：返回 healthy，detail 标注增益
- [ ] 连点"验证"5 次 → 只有第 1 次真实执行
- [ ] 探针期间页面行内显示骨架条，完成后 pill 翻转，无布局跳动

---

## 阶段 4 · 行内编辑 + 打磨（半天）

1. **行内配置**：明细行「配置」→ 行内展开 textarea（复用 [settings.html](../templates/admin/settings.html) 的样式类）→ 保存走现有 `POST /admin/settings`（AJAX 分支已存在）→ 成功后自动触发字段校验 + 提示"建议运行验证"
2. **金丝雀管理**：探针失败(detail 含"更换建议")的行，操作区出现「更换金丝雀」入口
3. **View Transitions**：`base_console.html` 加 `@view-transition { navigation: auto }`（渐进增强，Firefox 优雅降级）
4. **键盘与语义**：状态 pill 配 `aria-label`、探针按钮可 Tab 聚焦、骨架区 `aria-busy="true"`

### 验收
- [ ] 从巡检页配置抖音 Cookie → 保存 → 字段校验即时反馈 → 点验证 → 全流程 3 次点击内完成
- [ ] 全键盘可完成"查看 → 配置 → 验证"闭环

---

## 阶段 5 · landing.html 表现层（1 天，独立排期、可砍）

> 与巡检页零耦合。完全照 ShortcutHub 清单的 ④⑤ 步，工具换无框架实现：

1. `IntersectionObserver` 批量入场（`v-motion` 等价物，约 15 行 JS）
2. GSAP CDN + ScrollTrigger：Hero 视差（`yPercent: 30, scrub: true`）
3. split-type 逐字入场（stagger 0.03s，`power3.out`）
4. Lenis 惯性滚动（仅此页，`lerp: 0.1`）
5. 全部包 `prefers-reduced-motion`；GSAP 侧用 `gsap.matchMedia()`
6. 验收：Lighthouse 性能分不因动效下降超过 5 分；reduced-motion 下内容完整可读

---

## 测试与提交策略

| 阶段 | 测试文件 | 提交信息（对齐仓库现有风格） |
|---|---|---|
| 0 | `tests/test_cookie_registry.py` | 新增 Cookie 凭据注册表与字段校验器 |
| 1 | — （视觉） | 统一管理台字阶、边线与图标体系 |
| 2 | `tests/test_cookie_overview.py` | 新增管理台 Cookie 凭据巡检页 |
| 3 | `tests/test_cookie_prober.py`（mock 解析链路，不出网） | 实现 Cookie 双跑对照探针与自动巡检 |
| 4 | 补 `test_cookie_overview.py` | 巡检页行内配置与交互打磨 |
| 5 | — | 门户首页视觉与动效升级 |

每阶段一个 commit，阶段②/③各打一次手动验收（用测试集链接实测一轮，更新 `报告/测试集实测报告` 的方式记录探针判定结果）。

## 全局风险与对策

| 风险 | 对策 |
|---|---|
| 探针消耗平台风控配额（尤其抖音 Argus） | 5 分钟防抖 + 手动/被动触发为主 + 无定时真探针 |
| 金丝雀链接失效导致误报 | 双跑对照自动区分 + probe_failed 状态 + 更换入口 |
| 管理台样式改动破坏现有页面 | 阶段①只动 class 与 CSS 变量，不动 DOM 结构；三宽度截图对比验收 |
| SQLite 并发写（探针线程 vs 请求） | 项目已有 `get_db` 连接管理模式，探针写记录走同一封装；写频极低 |
| Windows 开发机无法直跑 app.py（fcntl） | 沿用本次测试的垫片方式跑本地验收；CI/Docker 不受影响 |
