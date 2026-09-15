"""
本地视频生成服务（ComfyUI Wan 2.2 5B TI2V）

通过 ComfyUI HTTP API（默认 127.0.0.1:8188）提交文生视频 / 图生视频工作流，
WebSocket 实时回报每段的 KSampler 采样步进度，完成后将产物下载为项目内的 video.mp4。
另提供上传视频的标准化（无损封装优先，失败回退转码）。

工作流与 ComfyUI 官方模板 video_wan2_2_5B_ti2v.json 保持一致：
Wan22ImageToVideoLatent 的 start_image 为可选输入，同一模型同时支持
文生视频（不传首帧）与图生视频（传入首帧）。

长视频（超过单段显存预算上限）采用分段生成策略：
每段按预算内的最大帧数生成，提取上一段尾帧作为下一段首帧续写，
最后用 FFmpeg 无损拼接为完整视频。分段支持断点续传：
失败时已完成的段与进度清单（segments_state.json）保留在项目内，
下次以相同参数重新生成时自动跳过已完成段、从失败段继续。
"""

import asyncio
import json
import logging
import math
import random
import subprocess
import time
import uuid
from pathlib import Path
from typing import Awaitable, Callable, Optional

import httpx
import websockets

from app.config import settings
from app.services.file_store import (
    get_project_dir,
    get_project_file_path,
    get_public_url,
)
from app.services.media_merge import get_media_duration

# 进程级"进行中视频生成任务"注册表（project_id 集合）。
# ComfyUI 清场（interrupt + 清队列）无法区分"上次崩溃的遗留任务"与
# "本后端另一项目正在执行的任务"：项目 A 生成中时，项目 B 若无条件
# 清场会中断 A 的任务。清场前必须确认没有其他活跃任务。
_active_generations: set[str] = set()


def has_active_generations(exclude_project_id: str | None = None) -> bool:
    """查询是否存在进行中的视频生成任务（可排除指定项目）。"""
    if exclude_project_id is None:
        return bool(_active_generations)
    return any(pid != exclude_project_id for pid in _active_generations)

# Wan 2.2 5B TI2V 默认生成规格（模型训练规格，出处：ComfyUI 官方模板
# video_wan2_2_5B_ti2v.json）：1280x704、121 帧、24fps ≈ 5 秒
WAN_WIDTH = 1280
WAN_HEIGHT = 704
WAN_LENGTH = 121
WAN_FPS = 24

# 生成参数可调范围（宽高须为 16 的倍数——VAE 下采样因子约束；
# 帧数须为 4k+1 形式——Wan 潜空间时间压缩约束）
MIN_SIZE = 448
MAX_SIZE = 1280
MIN_FPS = 8
MAX_FPS = 30
MIN_DURATION = 1.0
# 总时长上限：超出单段预算的时长会自动拆分为多段生成（尾帧续写 + 拼接）。
# 长视频（如 3.5 分钟配音）耗时很长（分段数 x 单段推理时间），请量力选择
MAX_DURATION = 300.0
MIN_LENGTH = 9
MAX_LENGTH = 169

# 显存预算（像素·帧总数）：基准为 1280x704x121 满载标定值再打 85 折。
# 实测 121 帧（不打折）会在采样首步把 22GB 显存打满并触发驱动级挂起
# （GPU 100% 卡死、ComfyUI 事件循环无响应），打折后 1280x704 每段
# 101 帧稳定；更低分辨率允许相应更多帧，自动防 OOM
PIXEL_FRAME_BUDGET = int(WAN_WIDTH * WAN_HEIGHT * WAN_LENGTH * 0.85)

# 断点续传清单文件名（存于项目目录）：
# 记录生成参数与已完成段数，失败后重新生成时据此跳过已完成段
SEGMENTS_STATE_FILE = "segments_state.json"


def align_length(length: int) -> int:
    """
    将帧数对齐到 Wan 要求的 4k+1 形式（如 25/29/.../121/125）。

    Args:
        length: 任意正整数帧数。

    Returns:
        不超过原值的最大 4k+1 帧数（下限 MIN_LENGTH）。
    """
    aligned = 4 * ((max(length, MIN_LENGTH) - 1) // 4) + 1
    return max(MIN_LENGTH, aligned)


def compute_video_length(duration: float, fps: int, width: int, height: int) -> int:
    """
    根据时长、帧率与分辨率计算实际生成帧数。

    帧数 = 时长 x 帧率（间隔帧数），转换为 4k+1 形式（如 120 -> 121）
    后再按显存预算与硬上限钳制：超预算时自动降帧（时长缩短），
    保证任务不 OOM。

    Args:
        duration: 期望时长（秒）。
        fps: 帧率。
        width: 视频宽度（像素）。
        height: 视频高度（像素）。

    Returns:
        实际生成的总帧数（4k+1 形式）。
    """
    interval_frames = max(1, round(duration * fps))
    length = 4 * (interval_frames // 4) + 1
    budget_length = PIXEL_FRAME_BUDGET // (width * height)
    return align_length(min(length, budget_length, MAX_LENGTH))

# 模型文件名（已下载到 ComfyUI models 目录）
WAN_UNET = "wan2.2_ti2v_5B_fp16.safetensors"
WAN_CLIP = "umt5_xxl_fp8_e4m3fn_scaled.safetensors"
WAN_VAE = "wan2.2_vae.safetensors"

# 官方模板默认负向提示词（中文通用负面词）
WAN_NEGATIVE_PROMPT = (
    "色调艳丽，过曝，静态，细节模糊不清，字幕，风格，作品，画作，画面，静止，"
    "整体发灰，最差质量，低质量，JPEG压缩残留，丑陋的，残缺的，多余的手指，"
    "画得不好的手部，画得不好的脸部，畸形的，毁容的，形态畸形的肢体，手指融合，"
    "静止不动的画面，杂乱的背景，三条腿，背景人很多，倒着走"
)

# 采样参数（与官方模板一致：uni_pc / simple / 20 步 / cfg 5 / shift 8）
WAN_STEPS = 20
WAN_CFG = 5.0
WAN_SAMPLER = "uni_pc"
WAN_SCHEDULER = "simple"
WAN_SHIFT = 8

# SaveVideo 输出节点 ID（用于从 history 中定位产物）
SAVE_VIDEO_NODE_ID = "11"


def build_wan_workflow(
    prompt_text: str,
    image_name: str | None,
    filename_prefix: str = "wan",
    width: int = WAN_WIDTH,
    height: int = WAN_HEIGHT,
    length: int = WAN_LENGTH,
    fps: int = WAN_FPS,
) -> dict:
    """
    构造 ComfyUI API 格式的 Wan 2.2 5B TI2V 工作流。

    Args:
        prompt_text: 正向提示词（视频内容描述）。
        image_name: ComfyUI 输入目录中的首帧图片文件名；None 表示文生视频。
        filename_prefix: SaveVideo 输出文件名前缀。
        width: 视频宽度（16 的倍数）。
        height: 视频高度（16 的倍数）。
        length: 总帧数（4k+1 形式）。
        fps: 帧率。

    Returns:
        可直接提交到 ComfyUI /prompt 接口的工作流（以节点 ID 为键）。
    """
    workflow: dict = {
        # 模型加载
        "1": {
            "class_type": "UNETLoader",
            "inputs": {"unet_name": WAN_UNET, "weight_dtype": "default"},
        },
        "2": {
            "class_type": "CLIPLoader",
            "inputs": {"clip_name": WAN_CLIP, "type": "wan", "device": "default"},
        },
        "3": {"class_type": "VAELoader", "inputs": {"vae_name": WAN_VAE}},
        # 流偏移（Wan 系列推荐 shift=8）
        "4": {"class_type": "ModelSamplingSD3", "inputs": {"model": ["1", 0], "shift": WAN_SHIFT}},
        # 正负提示词编码
        "5": {"class_type": "CLIPTextEncode", "inputs": {"clip": ["2", 0], "text": prompt_text}},
        "6": {
            "class_type": "CLIPTextEncode",
            "inputs": {"clip": ["2", 0], "text": WAN_NEGATIVE_PROMPT},
        },
        # 视频潜空间（start_image 可选：传入即图生视频）
        "7": {
            "class_type": "Wan22ImageToVideoLatent",
            "inputs": {
                "vae": ["3", 0],
                "width": width,
                "height": height,
                "length": length,
                "batch_size": 1,
            },
        },
        # 采样
        "8": {
            "class_type": "KSampler",
            "inputs": {
                "model": ["4", 0],
                "positive": ["5", 0],
                "negative": ["6", 0],
                "latent_image": ["7", 0],
                "seed": random.randint(0, 2**31 - 1),
                "steps": WAN_STEPS,
                "cfg": WAN_CFG,
                "sampler_name": WAN_SAMPLER,
                "scheduler": WAN_SCHEDULER,
                "denoise": 1.0,
            },
        },
        # 解码与封装
        "9": {"class_type": "VAEDecode", "inputs": {"samples": ["8", 0], "vae": ["3", 0]}},
        "10": {"class_type": "CreateVideo", "inputs": {"images": ["9", 0], "fps": fps}},
        "11": {
            "class_type": "SaveVideo",
            "inputs": {
                "video": ["10", 0],
                "filename_prefix": filename_prefix,
                "format": "auto",
            },
        },
    }

    if image_name:
        # 图生视频：上传到 ComfyUI input 目录的图片作为首帧
        workflow["0"] = {"class_type": "LoadImage", "inputs": {"image": image_name}}
        workflow["7"]["inputs"]["start_image"] = ["0", 0]

    return workflow


async def check_comfyui_health() -> bool:
    """
    检查 ComfyUI 服务是否可用。

    Returns:
        服务可达返回 True，否则 False。
    """
    try:
        async with httpx.AsyncClient(timeout=5.0, trust_env=False) as client:
            response = await client.get(f"{settings.comfyui_base_url}/system_stats")
            return response.status_code == 200
    except Exception:
        return False


async def clear_comfyui_stale_tasks(
    verify_timeout: float = 20.0,
) -> tuple[int, int]:
    """
    中断 ComfyUI 正在执行的旧任务并清空待执行队列。

    ComfyUI 为单任务串行队列：后端崩溃/被杀后其已提交的旧任务仍会
    占用 GPU 继续执行，新任务只能排队等待——表现为新任务进度一直
    为 0 直到超时。因此每次开始新的视频生成前调用本函数清场。

    发出中断与清队指令后会轮询验证队列确实清空；若运行中任务迟迟
    不退出（ComfyUI 执行线程卡死，常见于 CUDA 挂起），抛出明确
    错误提示重启 ComfyUI，而不是让新任务白白排队超时。

    Args:
        verify_timeout: 清场后等待队列清空的最长秒数。

    Returns:
        (中断的运行中任务数, 清空的排队任务数)。

    Raises:
        RuntimeError: 遗留任务无法中断（ComfyUI 疑似卡死）。
    """
    running = 0
    pending = 0
    async with httpx.AsyncClient(timeout=15.0, trust_env=False) as client:
        # 先查队列现状（仅用于统计与日志）
        try:
            resp = await client.get(f"{settings.comfyui_base_url}/queue")
            if resp.status_code == 200:
                running = len(resp.json().get("queue_running") or [])
                pending = len(resp.json().get("queue_pending") or [])
        except Exception:
            pass
        if running == 0 and pending == 0:
            return 0, 0  # 队列干净，无需清场

        # 中断正在执行的任务（POST /interrupt，无 prompt_id 时中断全部）
        try:
            await client.post(f"{settings.comfyui_base_url}/interrupt", json={})
        except Exception:
            pass
        # 清空待执行队列（此版本为 POST /queue {"clear": true}）
        try:
            await client.post(f"{settings.comfyui_base_url}/queue", json={"clear": True})
        except Exception:
            pass

        # 验证队列已清空：中断信号在采样步间隙生效，通常几秒内退出
        deadline = time.monotonic() + verify_timeout
        while time.monotonic() < deadline:
            await asyncio.sleep(2.0)
            try:
                resp = await client.get(f"{settings.comfyui_base_url}/queue")
                if resp.status_code == 200:
                    q = resp.json()
                    if not (q.get("queue_running") or []) and not (q.get("queue_pending") or []):
                        return running, pending
            except Exception:
                pass
        raise RuntimeError(
            "ComfyUI 遗留任务无法中断（已有任务卡死，疑似 GPU/CUDA 挂起）。"
            "请重启 ComfyUI 后重试：结束占用 8188 端口的进程，"
            "再运行 start_all.py 或原启动命令。"
        )


async def upload_start_image(project_id: str, image_path: Path) -> str:
    """
    将项目首帧图片上传到 ComfyUI 输入目录。

    Args:
        project_id: 项目 ID（用于生成不冲突的上传文件名）。
        image_path: 本地图片路径。

    Returns:
        工作流 LoadImage 节点可引用的文件名（含子目录前缀，如有）。
    """
    upload_name = f"clonempeg_{project_id}_start{image_path.suffix.lower() or '.png'}"
    url = f"{settings.comfyui_base_url}/upload/image"
    async with httpx.AsyncClient(timeout=120.0, trust_env=False) as client:
        with open(image_path, "rb") as f:
            response = await client.post(
                url,
                files={"image": (upload_name, f, "image/png")},
                data={"overwrite": "true"},
            )
    if response.status_code != 200:
        raise RuntimeError(
            f"首帧图片上传 ComfyUI 失败 HTTP {response.status_code}: {response.text[:300]}"
        )
    data = response.json()
    name = data.get("name")
    if not name:
        raise ValueError(f"ComfyUI 上传响应缺少文件名: {response.text[:300]}")
    subfolder = data.get("subfolder") or ""
    return f"{subfolder}/{name}" if subfolder else name


async def submit_workflow(workflow: dict, client_id: str) -> str:
    """
    提交工作流到 ComfyUI 执行队列。

    Args:
        workflow: API 格式工作流。
        client_id: 提交者标识；执行事件（含 progress 消息）将定向
            发送到同 client_id 的 WebSocket 连接。

    Returns:
        任务 ID（prompt_id）。
    """
    payload = {"client_id": client_id, "prompt": workflow}
    async with httpx.AsyncClient(timeout=60.0, trust_env=False) as client:
        response = await client.post(f"{settings.comfyui_base_url}/prompt", json=payload)
    if response.status_code != 200:
        # 400 响应体包含节点校验错误详情（如模型文件缺失）
        raise RuntimeError(
            f"ComfyUI 工作流提交失败 HTTP {response.status_code}: {response.text[:800]}"
        )
    prompt_id = response.json().get("prompt_id")
    if not prompt_id:
        raise ValueError(f"ComfyUI 未返回任务 ID: {response.text[:300]}")
    return prompt_id


def _parse_ws_message(raw) -> tuple[str, dict]:
    """
    解析 ComfyUI WebSocket 消息（兼容 text JSON 与 binary 两种格式）。

    binary 格式为 [4B 类型长度][类型][4B 数据长度][JSON 数据]。

    Args:
        raw: websockets 收到的 str 或 bytes 消息。

    Returns:
        (消息类型, 数据字典) 元组；无法解析时类型为空串。
    """
    if isinstance(raw, (bytes, bytearray)):
        try:
            type_len = int.from_bytes(raw[:4], "big")
            msg_type = raw[4:4 + type_len].decode()
            data_off = 4 + type_len
            data_len = int.from_bytes(raw[data_off:data_off + 4], "big")
            payload = raw[data_off + 4:data_off + 4 + data_len]
            data = json.loads(payload) if payload else {}
            return msg_type, data
        except Exception:
            return "", {}
    try:
        msg = json.loads(raw)
        return str(msg.get("type", "")), msg.get("data") or {}
    except Exception:
        return "", {}


async def _interrupt_comfyui_prompt(prompt_id: str) -> None:
    """
    尽力中断 ComfyUI 上指定任务（后端放弃等待时调用）。

    后端超时只是"不再等待"，不会自动取消 ComfyUI 侧任务；不中断的话
    任务会继续占用 GPU 直到下次生成前的清场。带 prompt_id 的定向
    中断只作用于该任务，不影响其他任务；失败静默（清场逻辑仍会兜底）。
    """
    try:
        async with httpx.AsyncClient(timeout=10.0, trust_env=False) as client:
            await client.post(
                f"{settings.comfyui_base_url}/interrupt", json={"prompt_id": prompt_id}
            )
    except Exception:
        pass


async def _run_workflow_with_progress(
    workflow: dict,
    on_seg_progress: Optional[Callable[[float], Awaitable[None]]],
) -> dict:
    """
    提交工作流并通过 WebSocket 实时监听执行进度，等待任务结束。

    先以 client_id 建立 WS 连接再提交（ComfyUI 将执行事件定向发到该
    连接），收到 progress 消息时回调段内进度（0.0~1.0）。此版本
    ComfyUI 完成时发送 execution_success（带 prompt_id，不再发送
    history 事件），据此转 HTTP 拉取 history 条目；execution_error
    立即抛错。WS 不可用或中途断开时自动回退 HTTP 轮询
    （无段内进度，但任务照常完成）。

    等待超时（后端放弃）时会定向中断该 ComfyUI 任务，避免其脱离
    监管继续占用 GPU。

    Args:
        workflow: API 格式工作流。
        on_seg_progress: 段内进度回调 async fn(progress)（KSampler 步数比例）。

    Returns:
        该任务的 history 条目。
    """
    client_id = uuid.uuid4().hex
    ws_url = f"{settings.comfyui_base_url.replace('http://', 'ws://')}/ws?clientId={client_id}"

    ws = None
    try:
        ws = await asyncio.wait_for(
            websockets.connect(ws_url, open_timeout=10, max_size=2**22),
            timeout=15.0,
        )
    except Exception:
        ws = None  # WS 不可用：回退 HTTP 轮询

    prompt_id: str | None = None
    try:
        prompt_id = await submit_workflow(workflow, client_id)
        if ws is None:
            return await wait_for_history_entry(prompt_id, timeout=settings.comfyui_timeout)

        # WS 监听：progress 回调段内进度；execution_success 结束等待；
        # execution_error 立即失败；长时间无消息或连接异常时转 HTTP 轮询兜底
        deadline = time.monotonic() + settings.comfyui_timeout
        while True:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise TimeoutError(
                    f"ComfyUI 任务 {prompt_id} 超过 {settings.comfyui_timeout:.0f} 秒未完成"
                )
            try:
                raw = await asyncio.wait_for(ws.recv(), timeout=min(remaining, 300.0))
            except (asyncio.TimeoutError, websockets.ConnectionClosed):
                break
            msg_type, data = _parse_ws_message(raw)
            # 事件带 prompt_id 时须匹配（progress 消息格式见
            # ComfyUI main.py: progress = {value, max, prompt_id, node}）
            if data.get("prompt_id") not in (None, prompt_id):
                continue
            if msg_type == "progress":
                value = float(data.get("value", 0))
                max_value = float(data.get("max", 0))
                if on_seg_progress is not None and max_value > 0:
                    await on_seg_progress(max(0.0, min(1.0, value / max_value)))
            elif msg_type == "execution_error":
                raise RuntimeError(
                    f"ComfyUI 执行出错: 节点 {data.get('node_id')} - "
                    f"{data.get('exception_message', '未知错误')}"
                )
            elif msg_type == "execution_success":
                # 完成信号：history 条目经 HTTP 拉取（此版本不再推送 history）
                return await wait_for_history_entry(prompt_id, timeout=60.0)
        # WS 中断：HTTP 轮询兜底，超时取剩余总预算（排队等场景可能仍需很久）
        remaining = max(300.0, deadline - time.monotonic())
        return await wait_for_history_entry(prompt_id, timeout=remaining)
    except TimeoutError:
        # 放弃等待的同时取消 ComfyUI 侧任务，避免其脱离监管继续占用 GPU
        if prompt_id is not None:
            await _interrupt_comfyui_prompt(prompt_id)
        raise
    finally:
        if ws is not None:
            await ws.close()


async def wait_for_history_entry(prompt_id: str, timeout: float, interval: float = 3.0) -> dict:
    """
    轮询 ComfyUI 历史记录直到任务执行结束。

    ComfyUI 在任务完成（成功或失败）后才会把条目写入 /history，
    执行期间查询返回空对象。

    Args:
        prompt_id: 任务 ID。
        timeout: 最长等待秒数。
        interval: 轮询间隔秒数。

    Returns:
        该任务的 history 条目。

    Raises:
        TimeoutError: 超时未完成。
    """
    url = f"{settings.comfyui_base_url}/history/{prompt_id}"
    deadline = time.monotonic() + timeout
    async with httpx.AsyncClient(timeout=30.0, trust_env=False) as client:
        while time.monotonic() < deadline:
            response = await client.get(url)
            if response.status_code == 200:
                entry = response.json().get(prompt_id)
                if entry is not None:
                    return entry
            await asyncio.sleep(interval)
    raise TimeoutError(f"ComfyUI 任务 {prompt_id} 超过 {timeout:.0f} 秒未完成")


def extract_saved_video(entry: dict) -> dict:
    """
    从 history 条目中提取 SaveVideo 节点的产物文件信息。

    Args:
        entry: 单个任务的 history 条目。

    Returns:
        含 filename / subfolder / type 的文件信息字典。

    Raises:
        RuntimeError: 条目中没有视频产物。
    """
    outputs = entry.get("outputs") or {}
    for node_output in outputs.values():
        # SaveVideo 的 UI 输出以 images 键承载（animated 视频）
        for item in node_output.get("images", []):
            if str(item.get("filename", "")).endswith((".mp4", ".webm", ".mkv")):
                return item
    raise RuntimeError(f"ComfyUI 历史记录中未找到生成的视频: {outputs}")


async def download_output_video(project_id: str, file_info: dict, filename: str = "video.mp4") -> str:
    """
    从 ComfyUI 输出目录下载视频到项目内指定文件。

    Args:
        project_id: 项目 ID。
        file_info: history 中的产物文件信息（filename/subfolder/type）。
        filename: 项目内目标文件名（默认 video.mp4；分段生成时为 video_seg_N.mp4）。

    Returns:
        下载文件的对外本地 URL。
    """
    params = {
        "filename": file_info["filename"],
        "subfolder": file_info.get("subfolder") or "",
        "type": file_info.get("type") or "output",
    }
    local_path = get_project_file_path(project_id, filename)
    async with httpx.AsyncClient(timeout=300.0, trust_env=False) as client:
        async with client.stream(
            "GET", f"{settings.comfyui_base_url}/view", params=params
        ) as response:
            if response.status_code != 200:
                raise RuntimeError(
                    f"下载 ComfyUI 产物失败 HTTP {response.status_code}: {file_info}"
                )
            with open(local_path, "wb") as f:
                async for chunk in response.aiter_bytes():
                    f.write(chunk)
    return get_public_url(project_id, filename)


def _generate_placeholder_video(
    project_id: str,
    image_path: Path | None,
    width: int = WAN_WIDTH,
    height: int = WAN_HEIGHT,
    duration: float = 5.0,
    fps: int = WAN_FPS,
) -> str:
    """
    演示模式下用 FFmpeg 生成本地占位视频。

    有首帧图时循环编码该图，否则生成指定画幅的纯色背景。

    Args:
        project_id: 项目 ID。
        image_path: 首帧图片路径（文生视频时为 None）。
        width: 视频宽度（像素）。
        height: 视频高度（像素）。
        duration: 时长（秒）。
        fps: 帧率。

    Returns:
        video.mp4 的对外本地 URL。
    """
    target_path = get_project_file_path(project_id, "video.mp4")

    if image_path is not None and image_path.exists():
        input_args = ["-loop", "1", "-i", str(image_path)]
    else:
        input_args = ["-f", "lavfi", "-i", f"color=c=0x223344:s={width}x{height}"]

    cmd = [
        "ffmpeg", "-y",
        *input_args,
        "-c:v", "libx264",
        "-t", str(duration),
        "-pix_fmt", "yuv420p",
        "-vf", "scale=trunc(iw/2)*2:trunc(ih/2)*2",
        "-r", str(fps),
        str(target_path),
    ]
    result = subprocess.run(
        cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, check=False
    )
    if result.returncode != 0:
        raise RuntimeError(f"生成占位视频失败: {result.stderr}")
    return get_public_url(project_id, "video.mp4")


def _tail_seek_offset(video_path: Path) -> float:
    """计算尾帧提取的回溯偏移秒数（不超过 1 秒，且不超过片长的 90%）。

    Args:
        video_path: 视频文件路径。

    Returns:
        回溯偏移（秒），探测失败时回退 1 秒。
    """
    try:
        duration = get_media_duration(video_path)
    except Exception:
        duration = 0.0
    if duration and duration > 0:
        return max(0.05, min(1.0, duration * 0.9))
    return 1.0


def _extract_last_frame(video_path: Path, output_png: Path) -> Path:
    """
    提取视频的最后一帧保存为 PNG（用于分段续写的首帧）。

    Args:
        video_path: 视频文件路径。
        output_png: 输出 PNG 路径。

    Returns:
        输出 PNG 路径。

    Raises:
        RuntimeError: FFmpeg 提取失败。
    """
    cmd = [
        "ffmpeg", "-y",
        # 从结尾前一段时间开始解码，配合 -update 取最后一帧。
        # 偏移按实际时长动态取（90% 时长且不超过 1 秒）：末段可能不足
        # 1 秒，固定 -sseof -1 会被钳制到文件开头而提取到首帧
        "-sseof", f"-{_tail_seek_offset(video_path):.3f}",
        "-i", str(video_path),
        "-update", "1",
        "-frames:v", "1",
        str(output_png),
    ]
    result = subprocess.run(
        cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, check=False
    )
    if result.returncode != 0 or not output_png.exists():
        raise RuntimeError(f"提取视频尾帧失败: {result.stderr[-500:]}")
    return output_png


def _concat_segments(project_id: str, seg_paths: list[Path]) -> str:
    """
    用 FFmpeg concat 无损拼接分段视频为项目内 video.mp4。

    各分段由同一工作流生成（同编码器/分辨率/帧率），可直接流复制。

    Args:
        project_id: 项目 ID。
        seg_paths: 分段文件路径列表（按顺序）。

    Returns:
        video.mp4 的对外本地 URL。

    Raises:
        RuntimeError: 拼接失败。
    """
    list_file = get_project_file_path(project_id, "concat_list.txt")
    with open(list_file, "w", encoding="utf-8") as f:
        for p in seg_paths:
            # concat 列表用单引号包裹路径，统一正斜杠避免转义问题
            f.write(f"file '{p.as_posix()}'\n")

    target_path = get_project_file_path(project_id, "video.mp4")
    cmd = [
        "ffmpeg", "-y",
        "-f", "concat",
        "-safe", "0",
        "-i", str(list_file),
        "-c", "copy",
        "-movflags", "+faststart",
        str(target_path),
    ]
    result = subprocess.run(
        cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, check=False
    )
    if result.returncode != 0 or not target_path.exists():
        raise RuntimeError(f"拼接分段视频失败: {result.stderr[-500:]}")
    return get_public_url(project_id, "video.mp4")


def _cleanup_segment_files(project_id: str) -> None:
    """删除分段生成的临时文件（含断点续传清单）。

    仅在整体生成成功（video.mp4 已产出）或参数变化需要全新生成时调用；
    失败时保留这些文件以便断点续传。
    """
    project_dir = get_project_dir(project_id)
    patterns = ("video_seg_*.mp4", "seg_tail_*.png", "concat_list.txt", SEGMENTS_STATE_FILE)
    for pattern in patterns:
        for f in project_dir.glob(pattern):
            f.unlink(missing_ok=True)


def _segment_file_valid(path: Path) -> bool:
    """校验分段产物文件有效（存在且非空；正常 mp4 产物远大于该阈值）。"""
    try:
        return path.exists() and path.stat().st_size > 10 * 1024
    except OSError:
        return False


def _load_resumable_state(
    project_id: str,
    params: dict,
    total_segments: int,
) -> int:
    """
    读取断点续传清单，返回可复用的已完成段数。

    仅当清单参数（提示词/宽高/帧率/总时长/总段数）与本次完全一致，
    且已完成段文件均有效时返回该段数；否则返回 0（需全新生成）。

    Args:
        project_id: 项目 ID。
        params: 本次生成参数字典（写入清单用于比对）。
        total_segments: 总段数。

    Returns:
        可续传的已完成段数（0 表示从头生成）。
    """
    state_path = get_project_file_path(project_id, SEGMENTS_STATE_FILE)
    if not state_path.exists():
        return 0
    try:
        state = json.loads(state_path.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError):
        return 0
    if state.get("params") != params or state.get("total_segments") != total_segments:
        return 0
    completed = int(state.get("completed", 0))
    # 从高向低校验：文件损坏时回退到最近的有效前缀
    while completed > 0:
        seg_path = get_project_file_path(project_id, f"video_seg_{completed - 1}.mp4")
        if _segment_file_valid(seg_path):
            break
        completed -= 1
    return max(0, min(completed, total_segments))


def _save_segments_state(project_id: str, params: dict, total_segments: int, completed: int) -> None:
    """写断点续传清单（每完成一段写一次，失败后据此恢复）。"""
    state_path = get_project_file_path(project_id, SEGMENTS_STATE_FILE)
    state = {
        "params": params,
        "total_segments": total_segments,
        "completed": max(0, min(completed, total_segments)),
    }
    state_path.write_text(json.dumps(state, ensure_ascii=False), encoding="utf-8")


async def _generate_single_segment(
    project_id: str,
    prompt: str,
    start_image_path: Path | None,
    width: int,
    height: int,
    fps: int,
    duration: float,
    seg_filename: str,
    seg_index: int,
    total_segments: int,
    on_seg_progress: Optional[Callable[[float], Awaitable[None]]] = None,
) -> Path:
    """
    生成单个分段并下载到项目内指定文件。

    Args:
        project_id: 项目 ID。
        prompt: 视频内容描述（各段保持一致以维持画面连贯）。
        start_image_path: 本段首帧图片路径；None 表示文生（仅第一段）。
        width: 视频宽度。
        height: 视频高度。
        fps: 帧率。
        duration: 本段时长（秒，已按单段预算裁剪）。
        seg_filename: 本段在项目内的文件名（如 video_seg_0.mp4）。
        seg_index: 当前段序号（从 0 开始，用于错误信息）。
        total_segments: 总段数（用于错误信息）。
        on_seg_progress: 段内进度回调 async fn(progress)（0.0~1.0）。

    Returns:
        本段文件的本地路径。

    Raises:
        RuntimeError: 任一环节失败（错误信息带段号）。
    """
    try:
        image_name = None
        if start_image_path is not None:
            image_name = await upload_start_image(project_id, start_image_path)

        length = compute_video_length(duration, fps, width, height)
        workflow = build_wan_workflow(
            prompt,
            image_name,
            filename_prefix=f"wan_{project_id}_seg{seg_index}",
            width=width,
            height=height,
            length=length,
            fps=fps,
        )
        entry = await _run_workflow_with_progress(workflow, on_seg_progress)
        status = entry.get("status") or {}
        status_str = status.get("status_str")
        if status_str != "success":
            messages = status.get("messages") or []
            raise RuntimeError(f"ComfyUI 视频生成失败（{status_str}）: {messages}")

        file_info = extract_saved_video(entry)
        await download_output_video(project_id, file_info, filename=seg_filename)
    except Exception as exc:
        raise RuntimeError(f"第 {seg_index + 1}/{total_segments} 段生成失败: {exc}") from exc

    return get_project_file_path(project_id, seg_filename)


async def generate_video_with_comfyui(
    project_id: str,
    prompt: str,
    image_path: Path | None,
    width: int = WAN_WIDTH,
    height: int = WAN_HEIGHT,
    fps: int = WAN_FPS,
    duration: float = 5.0,
    progress_cb: Optional[Callable[[dict], Awaitable[None]]] = None,
) -> str:
    """
    生成视频主流程（支持长视频自动分段、断点续传与阶段进度）。

    时长在单段预算内时一次生成；超出时拆分为多段：
    每段用上一段尾帧作为首帧续写（文生模式第 1 段后同样转尾帧续写，
    保证画面连贯），最后 FFmpeg 无损拼接。

    断点续传：失败时已完成段与清单保留；再次以相同参数生成时
    自动跳过已完成段，从失败段继续（拼接阶段无需重做已完成段）。

    Args:
        project_id: 项目 ID。
        prompt: 视频内容描述。
        image_path: 首帧图片本地路径；None 表示文生视频。
        width: 视频宽度（16 的倍数，默认 1280）。
        height: 视频高度（16 的倍数，默认 704）。
        fps: 帧率（默认 24）。
        duration: 期望总时长（秒，默认 5，最长 300）。
        progress_cb: 进度回调 async fn(state)，state 为字典：
            {"stage": "generating"/"concat", "done": 已完成段数,
             "total": 总段数, "seg_progress": 当前段内进度 0~1,
             "resumed": 续传起始段数或 0}。

    Returns:
        项目内 video.mp4 的对外本地 URL。
    """
    # 注册活跃任务：供其他项目/路由判断是否可以安全清场 ComfyUI
    _active_generations.add(project_id)
    try:
        return await _generate_video_impl(
            project_id, prompt, image_path,
            width, height, fps, duration, progress_cb,
        )
    finally:
        _active_generations.discard(project_id)


async def _generate_video_impl(
    project_id: str,
    prompt: str,
    image_path: Path | None,
    width: int,
    height: int,
    fps: int,
    duration: float,
    progress_cb: Optional[Callable[[dict], Awaitable[None]]] = None,
) -> str:
    """视频生成主流程实现（由 generate_video_with_comfyui 包装并注册活跃任务）。"""
    # 演示模式：不依赖 ComfyUI，直接生成本地占位视频
    if settings.demo_mode:
        return await asyncio.to_thread(
            _generate_placeholder_video, project_id, image_path, width, height, duration, fps
        )

    if not await check_comfyui_health():
        raise RuntimeError(
            "ComfyUI 服务不可达（http://127.0.0.1:8188）。"
            "请先启动 ComfyUI（conda 环境 comfyui，端口 8188）后再生成视频。"
        )

    # 清场：中断上次运行遗留的旧任务并清空队列。
    # ComfyUI 单任务串行，残留旧任务会让本次任务一直排队（进度卡 0 直至超时）。
    # 若本后端其他项目正在生成（注册表非空），说明队列中的任务并非"遗留"，
    # 跳过清场以免中断他人任务
    if not has_active_generations(exclude_project_id=project_id):
        stale_running, stale_pending = await clear_comfyui_stale_tasks()
        if stale_running or stale_pending:
            logging.getLogger(__name__).warning(
                "已清理 ComfyUI 遗留任务：中断运行中 %d 个，清空排队 %d 个",
                stale_running, stale_pending,
            )

    # 单段最大帧数（预算内）与对应时长
    seg_length = compute_video_length(999, fps, width, height)
    seg_duration = (seg_length - 1) / fps
    total_segments = max(1, math.ceil(duration / seg_duration))

    # 断点续传：参数一致时复用上次失败留下的已完成段
    params = {
        "prompt": prompt,
        "width": width,
        "height": height,
        "fps": fps,
        "duration": duration,
    }
    resumed = _load_resumable_state(project_id, params, total_segments)
    if resumed > 0 and resumed < total_segments:
        # 续传：当前首帧为已完成段的尾帧
        current_image = get_project_file_path(project_id, f"seg_tail_{resumed}.png")
        if not current_image.exists():
            # 尾帧缺失（提取失败/服务重启）：从上一段视频现场重新提取，
            # 避免将已完成的全部 GPU 分段推倒重来
            prev_seg = get_project_file_path(project_id, f"video_seg_{resumed - 1}.mp4")
            try:
                if not _segment_file_valid(prev_seg):
                    raise FileNotFoundError(f"上一段视频文件无效: {prev_seg}")
                await asyncio.to_thread(_extract_last_frame, prev_seg, current_image)
            except Exception:
                logging.getLogger(__name__).warning(
                    "续传尾帧缺失且重新提取失败，退回全新生成", exc_info=True
                )
                resumed = 0
    if resumed == 0:
        _cleanup_segment_files(project_id)  # 清掉不匹配的旧段
        # 删除旧的成功产物：避免失败后 finally 误判"已成功"而清掉本次分段
        get_project_file_path(project_id, "video.mp4").unlink(missing_ok=True)
        current_image = image_path  # 第一段首帧（图生模式）
    _save_segments_state(project_id, params, total_segments, resumed)

    async def _report(
        done: int, seg_progress: float = 0.0, stage: str = "generating"
    ) -> None:
        if progress_cb is not None:
            await progress_cb(
                {
                    "stage": stage,
                    "done": done,
                    "total": total_segments,
                    "seg_progress": round(max(0.0, min(1.0, seg_progress)), 4),
                    "resumed": resumed if stage == "generating" and done == resumed and resumed > 0 else 0,
                }
            )

    await _report(resumed, 0.0)
    try:
        for seg_idx in range(resumed, total_segments):
            # 各段时长：满段用预算上限，末段取剩余时长
            seg_dur = min(seg_duration, duration - seg_idx * seg_duration)
            seg_filename = f"video_seg_{seg_idx}.mp4"

            async def _on_seg_progress(p: float, _idx: int = seg_idx) -> None:
                """段内进度（KSampler 采样步比例）实时上报。"""
                await _report(_idx, p)

            seg_path = await _generate_single_segment(
                project_id, prompt, current_image,
                width, height, fps, seg_dur, seg_filename,
                seg_idx, total_segments,
                on_seg_progress=_on_seg_progress,
            )

            # 先提取尾帧、成功后再写入续传清单：若先写清单后提取失败，
            # 下次续传会因尾帧缺失误判而清掉全部已完成段从头重做
            if seg_idx + 1 < total_segments:
                # 还有后续段：提取本段尾帧作为下一段首帧，保持画面衔接
                tail_png = get_project_file_path(project_id, f"seg_tail_{seg_idx + 1}.png")
                await asyncio.to_thread(_extract_last_frame, seg_path, tail_png)
                current_image = tail_png
                _save_segments_state(project_id, params, total_segments, seg_idx + 1)
                await _report(seg_idx + 1, 0.0)
            else:
                _save_segments_state(project_id, params, total_segments, seg_idx + 1)
                await _report(seg_idx + 1, 1.0)

        # 收集全部分段路径（含续传复用的已完成段）
        seg_paths = [
            get_project_file_path(project_id, f"video_seg_{i}.mp4")
            for i in range(total_segments)
        ]

        if total_segments == 1:
            # 单段：直接重命名产物
            target_path = get_project_file_path(project_id, "video.mp4")
            seg_paths[0].replace(target_path)
            return get_public_url(project_id, "video.mp4")

        # 多段：上报拼接阶段后无损拼接（流复制，耗时秒级）
        await _report(total_segments, 1.0, stage="concat")
        return await asyncio.to_thread(_concat_segments, project_id, seg_paths)
    finally:
        # 仅成功时清理临时文件（video.mp4 已产出）；
        # 失败时保留分段与清单，供下次断点续传
        video_path = get_project_file_path(project_id, "video.mp4")
        if video_path.exists():
            _cleanup_segment_files(project_id)


def normalize_uploaded_video(project_id: str, source_path: Path) -> str:
    """
    将用户上传的本地视频标准化为项目内的 video.mp4。

    优先尝试无损封装（-c copy，速度快）；若封装失败（容器/编码不兼容），
    再回退到转码为 H.264 + AAC，确保后续合并步骤可稳定读取 video.mp4。

    Args:
        project_id: 项目 ID。
        source_path: 用户上传并已落盘的原始视频文件路径。

    Returns:
        标准化后 video.mp4 的对外本地 URL。

    Raises:
        FileNotFoundError: 源文件不存在。
        RuntimeError: 封装与转码均失败。
    """
    if not source_path.exists():
        raise FileNotFoundError(f"上传的视频文件不存在: {source_path}")

    target_path = get_project_file_path(project_id, "video.mp4")

    # 方案一：无损封装为 MP4（不重新编码，速度快、无画质损失）
    remux_cmd = [
        "ffmpeg", "-y",
        "-i", str(source_path),
        "-c", "copy",
        "-movflags", "+faststart",
        str(target_path),
    ]
    remux_result = subprocess.run(
        remux_cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, check=False
    )
    if remux_result.returncode == 0 and target_path.exists() and target_path.stat().st_size > 0:
        return get_public_url(project_id, "video.mp4")

    # 方案二：轨道不兼容时转码为 H.264/AAC，保证 MP4 可被合并步骤直接复制
    transcode_cmd = [
        "ffmpeg", "-y",
        "-i", str(source_path),
        "-c:v", "libx264",
        "-preset", "veryfast",
        "-crf", "20",
        "-pix_fmt", "yuv420p",
        "-c:a", "aac",
        "-b:a", "192k",
        "-movflags", "+faststart",
        str(target_path),
    ]
    transcode_result = subprocess.run(
        transcode_cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, check=False
    )
    if transcode_result.returncode != 0:
        raise RuntimeError(f"视频转码失败: {transcode_result.stderr[:500]}")
    return get_public_url(project_id, "video.mp4")
