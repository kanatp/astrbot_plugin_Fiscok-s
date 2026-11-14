'''
Venice AI 生图接口：
- 文生图：POST /image/generate（JSON 请求，返回 base64 图片数组）
- 图生图/编辑：POST /image/edit（JSON 请求，image 为参考图，返回原始图片字节）
- mode 决定走哪个端点：edit 只走编辑、generate 只走文生图、
  auto 按有无参考图分发（有图用基础模型 + -edit 走编辑，无图用基础模型走文生图）
'''
import asyncio
import base64
import json
import re
import time
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, Optional

import aiohttp

from astrbot.api import logger

# 生图缓存允许的图片后缀
_CACHE_SUFFIXES = {'.png', '.jpg', '.jpeg', '.webp'}

# Venice 文生图宽高上限
_MAX_SIDE = 1280

_FORMAT_SUFFIX = {'png': '.png', 'jpeg': '.jpg', 'jpg': '.jpg', 'webp': '.webp'}

_CONTENT_TYPE_SUFFIX = {
    'image/png': '.png',
    'image/jpeg': '.jpg',
    'image/webp': '.webp',
    'image/gif': '.gif',
}

_SUFFIX_MIME = {
    '.png': 'image/png',
    '.jpg': 'image/jpeg',
    '.jpeg': 'image/jpeg',
    '.webp': 'image/webp',
    '.gif': 'image/gif',
    '.bmp': 'image/bmp',
}


def _first_image_item(data: Any) -> Optional[Dict[str, Any]]:
    """从各家兼容响应里取出第一条图片结果。"""
    if not isinstance(data, dict):
        return None
    for key in ('data', 'images', 'output'):
        items = data.get(key)
        if not isinstance(items, list) or not items:
            continue
        first = items[0]
        if isinstance(first, str):
            return {'url': first} if first.startswith('http') else {'b64_json': first}
        if isinstance(first, dict):
            return first
    return None


def _suffix_from_content_type(value: Any) -> str:
    return _CONTENT_TYPE_SUFFIX.get(str(value or '').split(';')[0].strip().lower(), '')


def _suffix_from_url(url: str) -> str:
    suffix = Path(str(url).split('?')[0]).suffix.lower()
    return suffix if suffix in _CACHE_SUFFIXES else ''


def _parse_size(size: Any) -> Dict[str, Any]:
    """把 size 解析成 Venice 的 width/height 或 aspect_ratio。"""
    text = str(size or '').strip().lower()
    if not text:
        return {}
    if ':' in text and 'x' not in text:
        return {'aspect_ratio': text}
    match = re.match(r'^(\d+)\s*[x×]\s*(\d+)$', text)
    if not match:
        return {}
    width = max(1, min(_MAX_SIDE, int(match.group(1))))
    height = max(1, min(_MAX_SIDE, int(match.group(2))))
    return {'width': width, 'height': height}


def _cleanup_cache(save_dir: Path, keep: int) -> None:
    """按修改时间保留最近 keep 张生图，删除更早的缓存。"""
    if keep <= 0:
        return
    try:
        files = sorted(
            (
                path for path in save_dir.iterdir()
                if path.is_file() and path.suffix.lower() in _CACHE_SUFFIXES
            ),
            key=lambda path: path.stat().st_mtime,
        )
    except OSError:
        return
    for path in files[:-keep]:
        try:
            path.unlink()
        except OSError:
            pass


def _save_image_bytes(save_dir: Path, raw: bytes, suffix: str, keep: int) -> Optional[Path]:
    """把图片字节写入缓存目录并清理旧文件。"""
    if not raw:
        logger.warning("[Fiscok's][image] 生成结果为空，跳过保存")
        return None
    save_dir.mkdir(parents=True, exist_ok=True)
    path = save_dir / (
        f"gen_{datetime.now():%Y%m%d_%H%M%S}_{int(time.time() * 1000) % 1000:03d}"
        f"{suffix or '.png'}"
    )
    path.write_bytes(raw)
    _cleanup_cache(save_dir, keep)
    logger.info(f"[Fiscok's][image] 生图完成: {path}")
    return path


def _build_prompt(config: Dict[str, Any], prompt: str) -> str:
    prefix = str(config.get('prompt_prefix') or '').strip()
    return f"{prefix}，{prompt}" if prefix else prompt


def _auth_headers(api_key: str) -> Dict[str, str]:
    headers = {'Content-Type': 'application/json'}
    if api_key:
        headers['Authorization'] = f'Bearer {api_key}'
    return headers


async def _request_generate(
    config: Dict[str, Any],
    model: str,
    prompt: str,
    save_dir: Path,
    reference_image: Optional[Path],
) -> Optional[Path]:
    """文生图：POST /image/generate，参考图通过 style_references 传入。"""
    image_format = str(config.get('format') or 'png').strip().lower() or 'png'
    payload: Dict[str, Any] = {
        'model': model,
        'prompt': prompt,
        'format': image_format,
        'variants': 1,
        'return_binary': False,
        'hide_watermark': bool(config.get('hide_watermark', True)),
        'safe_mode': bool(config.get('safe_mode', True)),
    }
    payload.update(_parse_size(config.get('size')))

    negative = str(config.get('negative_prompt') or '').strip()
    if negative:
        payload['negative_prompt'] = negative
    style_preset = str(config.get('style_preset') or '').strip()
    if style_preset:
        payload['style_preset'] = style_preset

    if reference_image is not None and config.get('reference_enabled', True):
        try:
            raw_image = reference_image.read_bytes()
        except OSError as e:
            logger.warning(f"[Fiscok's][image] 读取生图参考图失败，改为纯文生图: {e}")
        else:
            mime = _SUFFIX_MIME.get(reference_image.suffix.lower(), 'image/png')
            strength = float(config.get('reference_strength', 0.8) or 0.8)
            payload['style_references'] = [{
                'image': f"data:{mime};base64,{base64.b64encode(raw_image).decode()}",
                'strength': max(0.0, min(1.0, strength)),
            }]
            logger.info(f"[Fiscok's][image] 生图已携带人设参考图: {reference_image.name}")

    api_base = str(config.get('api_base') or '').strip().rstrip('/')
    timeout = aiohttp.ClientTimeout(total=float(config.get('timeout', 90) or 90))
    try:
        async with aiohttp.ClientSession(timeout=timeout) as session:
            async with session.post(
                f"{api_base}/image/generate",
                json=payload,
                headers=_auth_headers(str(config.get('api_key') or '').strip()),
            ) as response:
                body = await response.text()
                if response.status != 200:
                    logger.warning(
                        f"[Fiscok's][image] 生图接口返回 {response.status}: {body[:300]}"
                    )
                    return None
                data = json.loads(body)

            item = _first_image_item(data)
            if not item:
                logger.warning(f"[Fiscok's][image] 生图接口未返回图片: {str(data)[:300]}")
                return None

            suffix = _FORMAT_SUFFIX.get(image_format, '.png')
            if item.get('b64_json'):
                encoded = str(item['b64_json'])
                # 兼容 data:image/png;base64,xxx 形式的 data URI
                if encoded.startswith('data:'):
                    header, _, encoded = encoded.partition(',')
                    suffix = _suffix_from_content_type(header[5:]) or suffix
                raw = base64.b64decode(encoded)
            elif item.get('url'):
                async with session.get(item['url']) as image_resp:
                    if image_resp.status != 200:
                        logger.warning(
                            f"[Fiscok's][image] 下载生成图片失败: {image_resp.status}"
                        )
                        return None
                    raw = await image_resp.read()
                    suffix = (
                        _suffix_from_content_type(image_resp.headers.get('Content-Type'))
                        or _suffix_from_url(item['url'])
                    )
            else:
                logger.warning("[Fiscok's][image] 生图结果既无 b64_json 也无 url")
                return None

        return _save_image_bytes(
            save_dir, raw, suffix, int(config.get('keep_images', 50) or 0)
        )
    except asyncio.TimeoutError:
        logger.warning(f"[Fiscok's][image] 生图超时（{timeout.total:.0f}s），已跳过")
        return None
    except Exception as e:
        logger.error(f"[Fiscok's][image] 生图失败: {e}", exc_info=True)
        return None


async def _request_edit(
    config: Dict[str, Any],
    model: str,
    prompt: str,
    save_dir: Path,
    reference_image: Path,
) -> Optional[Path]:
    """图生图/编辑：POST /image/edit，参考图放在 image 字段，返回原始图片字节。"""
    try:
        raw_image = reference_image.read_bytes()
    except OSError as e:
        logger.warning(f"[Fiscok's][image] 读取编辑参考图失败，跳过: {e}")
        return None

    mime = _SUFFIX_MIME.get(reference_image.suffix.lower(), 'image/png')
    payload: Dict[str, Any] = {
        'model': model,
        'prompt': prompt,
        'image': f"data:{mime};base64,{base64.b64encode(raw_image).decode()}",
        'safe_mode': bool(config.get('safe_mode', True)),
    }
    # 编辑模型对 aspect_ratio / resolution 有取值约束，未配置时不传，交给模型默认值
    aspect_ratio = str(config.get('edit_aspect_ratio') or '').strip()
    if aspect_ratio:
        payload['aspect_ratio'] = aspect_ratio
    resolution = str(config.get('edit_resolution') or '').strip()
    if resolution:
        payload['resolution'] = resolution

    api_base = str(config.get('api_base') or '').strip().rstrip('/')
    timeout = aiohttp.ClientTimeout(total=float(config.get('timeout', 90) or 90))
    logger.info(f"[Fiscok's][image] 使用编辑接口生图，参考图: {reference_image.name}")
    try:
        async with aiohttp.ClientSession(timeout=timeout) as session:
            async with session.post(
                f"{api_base}/image/edit",
                json=payload,
                headers=_auth_headers(str(config.get('api_key') or '').strip()),
            ) as response:
                if response.status != 200:
                    body = await response.text()
                    logger.warning(
                        f"[Fiscok's][image] 编辑接口返回 {response.status}: {body[:300]}"
                    )
                    return None
                raw = await response.read()
                suffix = _suffix_from_content_type(response.headers.get('Content-Type'))

        return _save_image_bytes(
            save_dir, raw, suffix or '.png', int(config.get('keep_images', 50) or 0)
        )
    except asyncio.TimeoutError:
        logger.warning(f"[Fiscok's][image] 编辑生图超时（{timeout.total:.0f}s），已跳过")
        return None
    except Exception as e:
        logger.error(f"[Fiscok's][image] 编辑生图失败: {e}", exc_info=True)
        return None


async def generate_image(
    config: Dict[str, Any],
    prompt: str,
    save_dir: Path,
    reference_image: Optional[Path] = None,
) -> Optional[Path]:
    """
    调用生图接口并保存到本地，成功返回图片路径。

    配置项：api_base / api_key / model / mode / size / edit_aspect_ratio /
            edit_resolution / prompt_prefix / timeout / keep_images /
            reference_enabled / reference_image / reference_strength
    model 只需填基础模型 ID（如 seedream-v5-5），mode=auto（默认）时按有无参考图分发：
    有参考图 → 模型名加 -edit 走 /image/edit；没有 → 基础模型走 /image/generate。
    """
    api_base = str(config.get('api_base') or '').strip().rstrip('/')
    model = str(config.get('model') or '').strip()
    prompt = str(prompt or '').strip()
    if not api_base or not model or not prompt:
        logger.warning("[Fiscok's][image] 生图配置不完整（api_base/model/画面描述为空），跳过")
        return None

    prompt = _build_prompt(config, prompt)
    mode = str(config.get('mode') or 'auto').strip().lower()
    if mode not in ('auto', 'edit', 'generate'):
        mode = 'auto'

    # 兼容直接填了 xxx-edit 的配置：统一还原成基础模型 ID
    base_model = model[:-len('-edit')] if model.lower().endswith('-edit') else model
    edit_model = f"{base_model}-edit"

    has_reference = reference_image is not None and bool(config.get('reference_enabled', True))
    if mode == 'edit':
        if not has_reference:
            logger.warning("[Fiscok's][image] 生图模式为 edit 但没有可用参考图，跳过")
            return None
        return await _request_edit(config, edit_model, prompt, save_dir, reference_image)
    if mode == 'generate':
        return await _request_generate(config, base_model, prompt, save_dir, reference_image)

    # auto：有参考图走编辑模型，否则用基础模型文生图
    if has_reference:
        return await _request_edit(config, edit_model, prompt, save_dir, reference_image)
    return await _request_generate(config, base_model, prompt, save_dir, reference_image)
