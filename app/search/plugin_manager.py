"""F12: 插件式音源架构 — 动态发现、注册、聚合多音源

架构设计：
1. 插件目录 app/search/plugins/ 下每个 .py 文件实现一个 SearchProvider
2. PluginManager 启动时扫描插件目录，importlib 动态加载
3. config 配置启用的音源列表，未启用的插件不加载
4. 聚合搜索自动包含所有已启用的音源

插件示例结构：
    from app.search.base import SearchProvider, SearchResult, MusicFormat
    class QQMusicProvider(SearchProvider):
        @property
        def name(self) -> str:
            return "qqmusic"
        async def search(self, keyword, ...):
            ...
"""

import importlib
import logging
import os
from pathlib import Path
from typing import Optional

from app.search.base import SearchProvider, SearchResult

logger = logging.getLogger("musicnest.plugins")


class PluginManager:
    """F12: 音源插件管理器

    动态发现、加载、注册音源插件。
    """

    def __init__(self, plugins_dir: str = ""):
        self._plugins_dir = plugins_dir or str(Path(__file__).parent / "plugins")
        self._providers: dict[str, SearchProvider] = {}  # name -> provider instance
        self._enabled: set[str] = set()  # 启用的音源名称

    def discover(self) -> list[str]:
        """扫描插件目录，返回可用的插件名称列表"""
        available = []
        plugins_path = Path(self._plugins_dir)
        if not plugins_path.exists():
            return available

        for py_file in plugins_path.glob("*.py"):
            if py_file.name.startswith("_"):
                continue
            plugin_name = py_file.stem  # 文件名（不含.py）
            available.append(plugin_name)
        return available

    def load_plugin(self, plugin_name: str) -> bool:
        """加载指定插件

        Args:
            plugin_name: 插件文件名（不含 .py）

        Returns:
            True 至少加载一个 provider，False 失败
        """
        try:
            # 动态导入插件模块
            module_path = f"app.search.plugins.{plugin_name}"
            module = importlib.import_module(module_path)

            # 查找模块中所有 SearchProvider 子类（一个插件可定义多个 Provider）
            loaded_any = False
            for attr_name in dir(module):
                attr = getattr(module, attr_name)
                if (isinstance(attr, type)
                        and issubclass(attr, SearchProvider)
                        and attr is not SearchProvider):
                    # 实例化 provider
                    try:
                        provider = attr()
                        self._providers[provider.name] = provider
                        logger.info(f"[Plugins] 加载插件成功: {plugin_name} -> {provider.name}")
                        loaded_any = True
                    except Exception as e:
                        logger.warning(f"[Plugins] 插件实例化失败: {plugin_name} err={e}")

            if not loaded_any:
                logger.warning(f"[Plugins] 插件 {plugin_name} 未找到可用的 SearchProvider 子类")
                return False
            return True
        except Exception as e:
            logger.warning(f"[Plugins] 加载插件失败: {plugin_name} err={e}")
            return False

    def load_all(self, enabled_list: Optional[list[str]] = None) -> dict[str, SearchProvider]:
        """加载所有可用插件或指定列表

        Args:
            enabled_list: 启用的插件名列表，None 表示加载全部

        Returns:
            {name: provider} 字典
        """
        available = self.discover()
        if not available:
            logger.info("[Plugins] 插件目录为空或不存在")
            return {}

        to_load = enabled_list if enabled_list is not None else available
        for plugin_name in to_load:
            if plugin_name in available:
                self.load_plugin(plugin_name)
            else:
                logger.warning(f"[Plugins] 插件 {plugin_name} 不在可用列表中: {available}")

        logger.info(f"[Plugins] 已加载 {len(self._providers)} 个音源: {list(self._providers.keys())}")
        return self._providers

    def get_provider(self, name: str) -> Optional[SearchProvider]:
        """获取指定音源的 provider"""
        return self._providers.get(name)

    def get_all_providers(self) -> dict[str, SearchProvider]:
        """获取所有已加载的 provider"""
        return self._providers

    def unload_plugin(self, name: str) -> None:
        """卸载插件"""
        if name in self._providers:
            del self._providers[name]
            logger.info(f"[Plugins] 已卸载: {name}")


# 全局插件管理器实例
plugin_manager = PluginManager()


async def search_with_plugins(keyword: str, limit: int = 10, timeout: float = 10.0) -> list[SearchResult]:
    """F12: 使用所有已加载的插件并发搜索

    合并所有音源结果，按相关度排序
    """
    import asyncio

    providers = plugin_manager.get_all_providers()
    if not providers:
        return []

    async def _safe_search(provider: SearchProvider) -> list[SearchResult]:
        try:
            # 不同 provider 的 search 签名可能不同，用 try 包裹
            import inspect
            sig = inspect.signature(provider.search)
            params = sig.parameters
            kwargs = {"keyword": keyword, "limit": limit}
            if "timeout" in params:
                kwargs["timeout"] = timeout
            if "skip_formats" in params:
                kwargs["skip_formats"] = True
            return await provider.search(**kwargs)
        except Exception as e:
            logger.warning(f"[Plugins] {provider.name} 搜索失败: {e}")
            return []

    tasks = [_safe_search(p) for p in providers.values()]
    results = await asyncio.gather(*tasks)

    # 合并所有结果
    merged = []
    for r in results:
        merged.extend(r)

    return merged[:limit]
