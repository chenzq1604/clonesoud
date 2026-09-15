"""
火山引擎文生图服务

调用方舟图片生成接口，下载图片到本地项目目录。
单次请求方舟仅返回 1 张图，故并发发起多次请求凑齐候选组。
"""

import asyncio
import hashlib
import httpx
import re
from pathlib import Path
from urllib.parse import urlparse

from app.config import settings
from app.services.file_store import (
    download_url_to_file,
    get_project_dir,
    get_project_file_path,
    get_public_url,
)

# 默认每次生成的候选图片数量（并发调用方舟接口）
DEFAULT_IMAGE_COUNT = 6


def _generate_demo_image(project_id: str, prompt: str, size: str, index: int) -> str:
    """
    演示模式下生成本地占位图片。

    根据提示词哈希生成固定配色，在图片上绘制提示词摘要，
    避免外部 API 不可用时流程中断。

    Args:
        project_id: 项目 ID。
        prompt: 图片提示词，用于决定占位图配色。
        size: 目标尺寸字符串，如 "2K"、"1024x1024"、"16:9"。
        index: 图片序号。

    Returns:
        本地占位图对外 URL。
    """
    try:
        from PIL import Image, ImageDraw, ImageFont
    except ImportError as exc:
        raise RuntimeError("演示模式需要 Pillow，请安装: pip install Pillow") from exc

    # 解析目标尺寸，默认 1024x1024
    width, height = 1024, 1024
    if "x" in size.lower():
        try:
            parts = size.lower().split("x")
            width = int(parts[0])
            height = int(parts[1])
        except (ValueError, IndexError):
            pass
    elif size == "2K":
        width, height = 1440, 2560
    elif ":" in size:
        try:
            parts = size.split(":")
            ratio_w = int(parts[0])
            ratio_h = int(parts[1])
            base = 1024
            width = base
            height = int(base * ratio_h / ratio_w)
        except (ValueError, IndexError, ZeroDivisionError):
            pass

    # 根据提示词哈希生成稳定的起始色相
    digest = hashlib.md5(prompt.encode("utf-8")).hexdigest()
    hue = int(digest[:2], 16) / 255.0
    saturation = 0.4 + (int(digest[2:4], 16) / 255.0) * 0.4
    value = 0.85

    # HSV 转 RGB 生成渐变配色
    import colorsys

    r1, g1, b1 = colorsys.hsv_to_rgb(hue, saturation, value)
    r2, g2, b2 = colorsys.hsv_to_rgb((hue + 0.15) % 1.0, saturation, value)
    color_top = (int(r1 * 255), int(g1 * 255), int(b1 * 255))
    color_bottom = (int(r2 * 255), int(g2 * 255), int(b2 * 255))

    image = Image.new("RGB", (width, height), color_top)
    draw = ImageDraw.Draw(image)

    # 绘制垂直渐变背景
    for y in range(height):
        ratio = y / height
        r = int(color_top[0] * (1 - ratio) + color_bottom[0] * ratio)
        g = int(color_top[1] * (1 - ratio) + color_bottom[1] * ratio)
        b = int(color_top[2] * (1 - ratio) + color_bottom[2] * ratio)
        draw.line([(0, y), (width, y)], fill=(r, g, b))

    # 绘制文字摘要
    text = f"[DEMO] {prompt[:60]}" if len(prompt) <= 60 else f"[DEMO] {prompt[:57]}..."
    try:
        # 尝试使用默认字体，字号随图片宽度缩放
        font_size = max(24, min(64, width // 20))
        font = ImageFont.truetype("arial.ttf", font_size)
    except OSError:
        font = ImageFont.load_default()

    bbox = draw.textbbox((0, 0), text, font=font)
    text_width = bbox[2] - bbox[0]
    text_height = bbox[3] - bbox[1]
    x = (width - text_width) // 2
    y = (height - text_height) // 2
    draw.text((x, y), text, fill=(255, 255, 255), font=font)

    # 保存图片
    filename = f"image_{index}.png"
    local_path = get_project_file_path(project_id, filename)
    image.save(local_path, "PNG")
    return get_public_url(project_id, filename)


def _get_ark_client(timeout: float = 120.0) -> httpx.AsyncClient:
    """创建已配置代理与鉴权的方舟 API 客户端。

    trust_env=False：仅使用此处显式配置的代理，避免环境变量
    引入第二套代理配置造成冲突。
    """
    proxy = settings.http_proxy if settings.http_proxy else None
    return httpx.AsyncClient(
        timeout=timeout,
        proxy=proxy,
        trust_env=False,
        follow_redirects=True,
        headers={"Authorization": f"Bearer {settings.ark_api_key}"},
    )


async def _create_single_image(client: httpx.AsyncClient, prompt: str, size: str) -> str:
    """
    调用方舟接口生成单张图片并返回其远程 URL。

    Args:
        client: 已配置代理与鉴权的方舟 API 客户端。
        prompt: 图片提示词。
        size: 图片尺寸。

    Returns:
        方舟返回的图片远程 URL。

    Raises:
        RuntimeError: 接口返回非 200 或响应中无图片 URL。
    """
    url = f"{settings.ark_base_url}/images/generations"
    payload = {
        "model": settings.image_model,
        "prompt": prompt,
        "size": size,
        "output_format": "png",
        "response_format": "url",
        "watermark": False,
    }

    response = await client.post(url, json=payload)
    if response.status_code != 200:
        # 透出方舟返回的具体错误（如模型不存在/未开通），
        # 便于前端与日志定位问题
        detail = response.text[:500]
        raise RuntimeError(f"方舟文生图失败 HTTP {response.status_code}: {detail}")

    data = response.json()
    # 兼容数组或单个对象的响应
    raw_items = data.get("data", [])
    if isinstance(raw_items, dict):
        raw_items = [raw_items]
    for item in raw_items:
        remote_url = item.get("url")
        if remote_url:
            return remote_url
    raise RuntimeError(f"方舟响应中无图片 URL: {str(data)[:300]}")


def _cleanup_candidate_images(project_id: str) -> None:
    """
    删除项目目录下旧的候选图片（image_*.png）。

    保留用户已选中的 image.png，避免重新生成导致新旧候选混杂、
    索引错位（/select 路由按文件名扫描候选图）。

    Args:
        project_id: 项目 ID。
    """
    project_dir = get_project_dir(project_id)
    for pattern in ("image_*.png", "cand_tmp_*.png"):
        for old_file in project_dir.glob(pattern):
            old_file.unlink(missing_ok=True)


async def generate_image(
    project_id: str,
    prompt: str,
    size: str = "2K",
    count: int = DEFAULT_IMAGE_COUNT,
) -> list[str]:
    """
    生成一组候选图片并下载到本地项目目录。

    真实模式下方舟单次请求仅返回 1 张图，故并发发起 count 次请求；
    部分请求失败时返回成功的那部分（全部失败才抛错）。
    生成成功后先清理旧候选图再落盘，失败时保留旧图。

    Args:
        project_id: 项目 ID。
        prompt: 图片提示词。
        size: 图片尺寸。
        count: 候选图片数量。

    Returns:
        本地保存的图片 URL 列表。
    """
    # 演示模式：跳过真实 API 调用，生成本地占位图
    if settings.demo_mode:
        _cleanup_candidate_images(project_id)
        return [_generate_demo_image(project_id, prompt, size, idx) for idx in range(count)]

    # 并发生成 count 张（每张一次独立请求，seed 随机）
    async with _get_ark_client() as client:
        results = await asyncio.gather(
            *[_create_single_image(client, prompt, size) for _ in range(count)],
            return_exceptions=True,
        )

    remote_urls = [r for r in results if isinstance(r, str)]
    errors = [r for r in results if isinstance(r, Exception)]
    if not remote_urls:
        raise errors[0] if errors else RuntimeError("方舟文生图失败：未返回任何图片")

    # 全部请求已成功（或至少一张成功）。先下载到临时名，任一张完整
    # 落盘后才清理旧图并连续编号重命名——若先删旧图后下载，
    # 下载全部失败时旧候选图已丢、新图一张都没有
    downloaded: list[Path] = []
    for idx, remote_url in enumerate(remote_urls):
        tmp_path = get_project_file_path(project_id, f"cand_tmp_{idx}.png")
        try:
            await download_url_to_file(remote_url, tmp_path)
            downloaded.append(tmp_path)
        except Exception:
            # 单张下载失败跳过，清理半成品后保留其余
            tmp_path.unlink(missing_ok=True)

    if not downloaded:
        raise RuntimeError("候选图片全部下载失败")

    # 至少一张新图已落盘：清理旧候选图，再连续编号（保证索引与文件名
    # 一一对应，跳号会导致 /select 按索引选中时错位）
    _cleanup_candidate_images(project_id)
    urls = []
    for new_idx, tmp_path in enumerate(downloaded):
        final_path = get_project_file_path(project_id, f"image_{new_idx}.png")
        tmp_path.replace(final_path)
        urls.append(get_public_url(project_id, final_path.name))
    return urls


async def select_image(project_id: str, urls: list[str], selected_index: int) -> str:
    """
    从生成的一组图片中选择一张作为后续视频首帧。

    将选中的图片复制/重命名为 image.png，便于后续流程统一读取。

    Args:
        project_id: 项目 ID。
        urls: 本地生成的图片 URL 列表。
        selected_index: 选中的索引。

    Returns:
        选中图片的本地 URL。
    """
    if selected_index < 0 or selected_index >= len(urls):
        raise ValueError("选中的图片索引无效")

    # 从选中 URL 解析真实文件名（而非按索引反拼 image_{index}.png）：
    # 历史数据或异常场景下索引与文件名可能不对应，按索引拼接
    # 会选错文件或直接 FileNotFoundError
    selected_name = Path(urlparse(urls[selected_index]).path).name
    if not re.fullmatch(r"image_\d+\.png", selected_name):
        raise ValueError(f"非法的候选图片文件名: {selected_name}")
    target_path = get_project_file_path(project_id, "image.png")
    source_path = get_project_file_path(project_id, selected_name)

    if not source_path.exists():
        raise FileNotFoundError(f"源图片不存在: {source_path}")

    target_path.write_bytes(source_path.read_bytes())
    return get_public_url(project_id, "image.png")
