# 闲鱼 (Xianyu) 逆向解析指南

本篇详细记录阿里巴巴旗下 **闲鱼** 二手商品与分享内容的图文及视频解析方案。

---

## 1. 平台特征与支持能力

* **平台标识**：`闲鱼`
* **支持媒体类型**：原画商品图集 (JPEG/PNG) / 封面 / 宝贝完整标题与详情文案 / 标价 / 卖家昵称 / 视频直链
* **常见链接形态**：
  * 口令/短链：`https://e.tb.cn/h.87fj9SNrqHW8kfC?tk=y1E5Tb4wkGd`、`https://m.tb.cn/h.jabJ2N9?tk=R4rQTJFs4aG`
  * 闲鱼 Web/H5 链接：`https://www.goofish.com/item?itemId=834469366646`、`https://h5.m.goofish.com/item?id=...`
  * 商品长链：`https://2.taobao.com/item.htm?id=123456789`、`https://item.taobao.com/item.htm?id=...`
* **Cookie 依赖**：🟢 免配置，无需任何 Cookie 登录态。

---

## 2. 核心逆向流程

1. **短链展开与 itemId 提取**：
   * 请求淘口令或短链（`m.tb.cn` / `e.tb.cn`），从 HTML 内联跳转变量 `var url = '...'` 或查询参数中提取目标商品 `itemId` / `id` 及初始标价。
2. **移动端 H5 MTOP 动态令牌签名提取**：
   * 采用闲鱼官方 H5 端 MTOP 接口：`mtop.taobao.idle.awesome.detail` (AppKey: `12574478`)。
   * **令牌握手**：先向网关发起初始化请求获取 `_m_h5_tk` Token。
   * **动态签名计算**：使用 `md5(token + "&" + t + "&" + appKey + "&" + data)` 计算合法 `sign`。
   * **媒体与元数据解析**：
     * **高清图集**：解析 `itemDO.imageInfos` 提取所有原图直链（由 `img.alicdn.com` 托管，自动清洗缩略裁剪参数，免防盗链且支持 CORS 跨域展示）。
     * **封面**：默认提取相册首图作为主图封面。
     * **标题与文案**：提取 `itemDO.title` 与 `itemDO.desc`。
     * **卖家昵称**：优先提取 `flowData.floating` 浮动组件中的未脱敏昵称，降级提取 `sellerDO.nick`。
     * **视频直链**：若商品包含介绍视频，自动递归提取视频直链。
   * **状态感知**：若宝贝已删除或下架（`FAIL_BIZ_ITEM_DEL_NOT_FOUND`），自动设置 `ITEM_NOT_FOUND` 业务提示。

---

## 3. 测试与验证

* **单元测试**：[tests/test_xianyu_parser.py](file:///Users/leo/Projects/media-parser/tests/test_xianyu_parser.py)
* **执行命令**：`python3 -m unittest tests/test_xianyu_parser.py`
