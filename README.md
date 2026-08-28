# 川渝天气自动生成站

GitHub Actions 每天自动抓取**中央气象台（NMC）**官方实况与逐日预报，生成一份自包含的单文件 HTML 天气解读，并通过 **GitHub Pages** 发布。可选接入 Ventusky 多要素图层截图与 LLM 文案润色。

## 出稿流程（全自动）

1. 抓取成都 / 重庆 NMC 实况与未来 4 天预报（`http://www.nmc.cn/rest/weather`）
2. （可选）Playwright 无头浏览器截图 Ventusky 风场 / 降水 / 气温 / 海平面气压图层
3. （可选）调 LLM 对官方数据写一段天气形势解读
4. 渲染自包含 `site/index.html` 并用 `actions/deploy-pages` 发布

## 定时

- 每天 **22:30 UTC（北京 06:30）** 自动生成一版
- 也可在 Actions 页手动 **Run workflow** 随时触发
- 每次发布前会把最新 `site/` 提交回 `main`，作为仓库保活动作，避免 cron 超过 60 天无活动被暂停

## 首次部署（一次性）

1. Fork / 新建仓库并把本目录推送到 `main`
2. 第一次运行工作流时，`configure-pages` 会自动开启 GitHub Pages（无需手动设置）
3. 站点地址：`https://<你的用户名>.github.io/<仓库名>`

## 可选配置（Secrets）

| Secret | 说明 |
|---|---|
| `OPENAI_API_KEY` | 配了即启用 LLM 文案润色 |
| `OPENAI_BASE_URL` | 自定义 API 地址（默认 OpenAI） |
| `OPENAI_MODEL` | 模型名（默认 gpt-4o-mini） |

> 不配任何 Secret 也能完整运行——核心报告完全基于 NMC 官方数据，确定性强。

## 本地测试

```bash
python3 src/generate_weather.py --out /tmp/site     # 核心模式
python3 src/generate_weather.py --ventusky --out /tmp/site
python3 src/generate_weather.py --llm    --out /tmp/site   # 需 OPENAI_API_KEY
```

## 免责声明

本页由脚本依据官方公开预报自动生成，未经人工审核，仅供信息参考。灾害性天气请以属地气象部门发布的预报预警为准。