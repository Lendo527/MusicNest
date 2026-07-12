"""F12: 插件目录占位文件

此目录下的每个 .py 文件应实现一个 SearchProvider 子类。
插件会在启动时被 PluginManager 动态发现和加载。

创建新音源插件：
1. 复制 _template.py 为 <音源名>.py
2. 实现 SearchProvider 的所有抽象方法
3. 在 config 中启用该音源
"""
