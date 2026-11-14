'''
人设图（立绘）挂载：
- 合并「配置里显式挂载的图片」与「人设图目录里的图片」
- 给每张图一个稳定的 id 与描述，供提示词清单与模型占位符匹配
'''
from pathlib import Path
from typing import Dict, List, Optional

from astrbot.api import logger

# 支持的人设图后缀
IMAGE_SUFFIXES = {'.png', '.jpg', '.jpeg', '.webp', '.gif', '.bmp'}


def resolve_image_root(data_root: Path, config: Dict) -> Path:
    """人设图目录；image_dir 支持相对插件数据目录或绝对路径。"""
    raw = str(config.get('image_dir') or 'persona_images').strip()
    path = Path(raw)
    return path if path.is_absolute() else Path(data_root) / path


def _unique_id(stem: str, images: List[Dict]) -> str:
    """生成不重复的图 id（重名时追加序号）。"""
    base = stem.strip() or 'image'
    used = {item['id'] for item in images}
    if base not in used:
        return base
    index = 2
    while f"{base}_{index}" in used:
        index += 1
    return f"{base}_{index}"


def list_persona_images(data_root: Path, config: Dict) -> List[Dict]:
    """
    列出可用人设图。

    显式挂载（config.images）优先，目录扫描补充其余图片；
    返回 [{"id": str, "path": str, "description": str}]。
    """
    root = resolve_image_root(data_root, config)
    images: List[Dict] = []
    seen = set()

    def add(path: Path, description: str = '') -> None:
        try:
            if not path.is_file():
                logger.warning(f"[Fiscok's][image] 人设图不存在，已跳过: {path}")
                return
            resolved = str(path.resolve())
        except OSError:
            return
        if resolved in seen:
            return
        seen.add(resolved)
        images.append({
            'id': _unique_id(path.stem, images),
            'path': resolved,
            'description': (description or path.stem).strip(),
        })

    for item in config.get('images') or []:
        if isinstance(item, str):
            file, description = item, ''
        elif isinstance(item, dict):
            file = str(item.get('file') or '').strip()
            description = str(item.get('description') or '').strip()
        else:
            continue
        if not file:
            continue
        path = Path(file)
        if not path.is_absolute():
            candidate = root / file
            path = candidate if candidate.exists() else Path(data_root) / file
        add(path, description)

    if root.is_dir():
        for path in sorted(root.iterdir()):
            if path.is_file() and path.suffix.lower() in IMAGE_SUFFIXES:
                add(path)

    return images


def resolve_persona_image(images: List[Dict], key: str) -> Optional[Dict]:
    """按 id / 文件名 / 描述匹配模型给出的图标识。"""
    key = str(key or '').strip()
    if not key:
        return None
    low = key.lower()

    for image in images:
        if image['id'].lower() == low:
            return image
    for image in images:
        path = Path(image['path'])
        if path.name.lower() == low or path.stem.lower() == low:
            return image
    for image in images:
        description = image['description'].lower()
        if low in description or description in low:
            return image
    return None


def format_image_list(images: List[Dict], max_items: int = 20) -> str:
    """把图片清单格式化为注入提示词的多行文本。"""
    limit = max(1, int(max_items or 20))
    return "\n".join(
        f"- id={image['id']}：{image['description']}" for image in images[:limit]
    )


def resolve_reference_image(
    data_root: Path,
    persona_config: Dict,
    gen_config: Dict,
) -> Optional[Path]:
    """
    选出生图参考图：优先 image_gen_config.reference_image，否则用人设图第一张。
    """
    raw = str(gen_config.get('reference_image') or '').strip()
    if raw:
        path = Path(raw)
        if not path.is_absolute():
            root = resolve_image_root(data_root, persona_config)
            candidate = root / raw
            path = candidate if candidate.exists() else Path(data_root) / raw
        if path.is_file():
            return path
        logger.warning(f"[Fiscok's][image] 生图参考图不存在: {path}")
        return None

    images = list_persona_images(data_root, persona_config)
    if not images:
        return None
    return Path(images[0]['path'])
