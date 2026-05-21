from astrbot.api.star import Star, Context
from astrbot.api.event import AstrMessageEvent
from astrbot.api.message_components import Plain
from astrbot.api import logger
import aiohttp
import time
import hashlib


class RealtimeSearchPlugin(Star):
    def __init__(self, context: Context):
        super().__init__(context)
        self.session = None
        self.cache = {}

        cfg = {}
        try:
            if hasattr(context, "get_plugin_config"):
                tmp = context.get_plugin_config()
                if isinstance(tmp, dict):
                    cfg = tmp
            elif hasattr(context, "get_config"):
                tmp = context.get_config()
                if isinstance(tmp, dict):
                    cfg = tmp
        except Exception as e:
            logger.warning(f"配置读取失败，使用默认值: {e}")

        self.search_provider = cfg.get("search_provider", "tavily")
        self.api_key = cfg.get("api_key", "")
        self.top_k = int(cfg.get("top_k", 5))
        self.timeout_sec = int(cfg.get("timeout_sec", 12))
        self.cache_ttl_sec = int(cfg.get("cache_ttl_sec", 180))
        self.links_on_request_only = cfg.get("reply_with_links_on_request_only", True)

        logger.info("RealtimeSearchPlugin loaded ✅")

    async def get_session(self):
        if not self.session:
            self.session = aiohttp.ClientSession()
        return self.session

    def _cache_key(self, query: str) -> str:
        return hashlib.md5(query.strip().lower().encode("utf-8")).hexdigest()

    def _get_cache(self, key: str):
        item = self.cache.get(key)
        if not item:
            return None
        if time.time() - item["ts"] > self.cache_ttl_sec:
            return None
        return item["data"]

    def _set_cache(self, key: str, data):
        self.cache[key] = {"ts": time.time(), "data": data}

    async def tavily_search(self, query: str):
        if not self.api_key:
            return {"ok": False, "error": "未配置 API Key（Tavily）"}

        url = "https://api.tavily.com/search"
        payload = {
            "api_key": self.api_key,
            "query": query,
            "max_results": self.top_k,
            "search_depth": "basic"
        }

        try:
            sess = await self.get_session()
            async with sess.post(url, json=payload, timeout=self.timeout_sec) as resp:
                if resp.status != 200:
                    text = await resp.text()
                    return {"ok": False, "error": f"Tavily HTTP {resp.status}: {text[:200]}"}
                data = await resp.json()
                return {"ok": True, "data": data}
        except Exception as e:
            return {"ok": False, "error": f"搜索失败: {e}"}

    def summarize(self, query: str, data: dict, with_links: bool = False) -> str:
        results = data.get("results", []) if isinstance(data, dict) else []
        if not results:
            return f"我刚查了「{query}」，暂时没找到可靠的新结果。"

        lines = [f"我刚实时查了「{query}」，现在看到的重点是："]
        for i, item in enumerate(results[:self.top_k], 1):
            title = (item.get("title") or "无标题").strip()
            content = (item.get("content") or "").strip().replace("\n", " ")
            snippet = content[:80] + ("..." if len(content) > 80 else "")
            line = f"{i}. {title}：{snippet}"
            if with_links:
                url = item.get("url", "")
                if url:
                    line += f"\n   链接：{url}"
            lines.append(line)

        return "\n".join(lines)

    def is_link_request(self, text: str) -> bool:
        t = text.lower()
        keys = ["链接", "网址", "来源", "出处", "给我url", "发我链接", "!链接", "!网址"]
        return any(k in t for k in keys)

    def should_trigger_search(self, text: str) -> bool:
        t = text.lower()
        trigger_words = [
            "现在", "最新", "刚刚", "今日", "热点", "热搜",
            "发生了什么", "新闻", "查一下", "搜一下", "帮我查"
        ]
        return any(k in t for k in trigger_words)

    async def on_message(self, event: AstrMessageEvent):
        try:
            text = str(getattr(event, "message_str", "")).strip()
            if not text:
                return False

            # 触发方式：含“查一下/最新/热点”等关键词
            if not self.should_trigger_search(text):
                return False

            query = text
            key = self._cache_key(query)
            cached = self._get_cache(key)

            if cached is None:
                if self.search_provider == "tavily":
                    ret = await self.tavily_search(query)
                else:
                    ret = {"ok": False, "error": "暂不支持该 provider"}

                if not ret["ok"]:
                    event.set_result([Plain(f"我去网上查了一下，但这次没成功：{ret['error']}")])
                    return True

                cached = ret["data"]
                self._set_cache(key, cached)

            with_links = (not self.links_on_request_only) or self.is_link_request(text)
            reply = self.summarize(query, cached, with_links=with_links)
            event.set_result([Plain(reply)])
            return True

        except Exception as e:
            logger.error(f"on_message error: {e}")
            return False

    async def terminate(self):
        if self.session:
            await self.session.close()
