"""Server 酱微信推送。"""
import requests
import logging

log = logging.getLogger(__name__)


class WeChatNotifier:
    def __init__(self, send_key: str):
        if not send_key:
            raise ValueError("Server 酱 send_key 不能为空")
        self.send_key = send_key
        self.endpoint = f"https://sctapi.ftqq.com/{send_key}.send"

    def send(self, title: str, content: str = "", short: str = None) -> dict:
        """
        发送微信消息。
        title: 标题（会显示在微信通知栏）
        content: 正文（Markdown 格式）
        short: 简短摘要（推送时也显示，避免消息太长）
        """
        data = {"title": title, "desp": content}
        if short:
            data["short"] = short
        try:
            r = requests.post(self.endpoint, data=data, timeout=10)
            result = r.json()
            if result.get("code") == 0:
                log.info(f"微信推送成功: {title}")
            else:
                log.error(f"微信推送失败: {result}")
            return result
        except Exception as e:
            log.error(f"微信推送异常: {e}")
            return {"code": -1, "message": str(e)}
