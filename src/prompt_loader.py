"""
Prompt 模板加载器

支持从 Markdown 文件加载 Prompt, 用 {{变量}} 占位符替换
"""

import re
from pathlib import Path
from typing import Dict, Any, Optional


# 占位符正则: {{ variable_name }}
PLACEHOLDER_PATTERN = re.compile(r'\{\{\s*(\w+)\s*\}\}')


class PromptLoader:
    """Prompt 模板加载器"""

    def __init__(self, prompts_dir: Path):
        self.prompts_dir = Path(prompts_dir)

    def load(self, name: str) -> str:
        """
        加载指定名称的 Prompt 模板

        Args:
            name: 模板名 (不含扩展名), 如 "stage2_search_keywords"

        Returns:
            模板内容
        """
        # 尝试多个扩展名
        for ext in [".md", ".txt", ".prompt"]:
            path = self.prompts_dir / f"{name}{ext}"
            if path.exists():
                return path.read_text(encoding="utf-8")

        # 列出可用的模板
        available = self.list_templates()
        raise FileNotFoundError(
            f"Prompt 模板 '{name}' 未找到. "
            f"可用模板: {available}"
        )

    def render(self, name: str, **variables: Any) -> str:
        """
        加载并渲染模板

        Args:
            name: 模板名
            **variables: 变量键值对

        Returns:
            渲染后的字符串
        """
        template = self.load(name)
        return self._render_string(template, variables)

    def _render_string(self, template: str, variables: Dict[str, Any]) -> str:
        """渲染字符串中的占位符"""
        def replace(match):
            key = match.group(1)
            if key not in variables:
                raise KeyError(
                    f"模板需要变量 '{key}', 但未提供. "
                    f"已提供: {list(variables.keys())}"
                )
            value = variables[key]
            if value is None:
                return ""
            return str(value)

        return PLACEHOLDER_PATTERN.sub(replace, template)

    def list_templates(self) -> list:
        """列出所有可用的模板"""
        if not self.prompts_dir.exists():
            return []
        files = []
        for ext in [".md", ".txt", ".prompt"]:
            files.extend(self.prompts_dir.glob(f"*{ext}"))
        return sorted([f.stem for f in files])


# ============================================================
# 便捷函数
# ============================================================

_default_loader: Optional[PromptLoader] = None


def get_prompt_loader(prompts_dir: Optional[Path] = None) -> PromptLoader:
    """获取默认加载器"""
    global _default_loader
    if _default_loader is None:
        if prompts_dir is None:
            prompts_dir = Path(__file__).parent.parent / "prompts"
        _default_loader = PromptLoader(prompts_dir)
    return _default_loader


if __name__ == "__main__":
    loader = PromptLoader(Path(__file__).parent.parent / "prompts")
    print("可用模板:", loader.list_templates())