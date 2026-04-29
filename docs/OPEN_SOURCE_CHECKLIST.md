# Open Source Checklist

发布到 GitHub 前请逐项确认：

- [ ] `config/config.yaml` 不存在或没有真实 key。
- [ ] `data-cache/stock.db` 没有进入仓库。
- [ ] `secrets/`、`logs/`、`venv/`、`backups/` 没有进入仓库。
- [ ] 没有真实持仓、交易记录、聊天记录和研报原文缓存。
- [ ] 已选择许可证。
- [ ] README 中保留“非投资建议”免责声明。
- [ ] 如果仓库公开，先轮换曾经暴露过的 API key。

推荐第一次发布为 public repo，但不要公开真实部署地址、真实配置和真实数据。
