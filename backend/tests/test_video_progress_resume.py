"""
视频分段进度与断点续传的单元测试

覆盖：
1. ComfyUI WebSocket 消息解析（text JSON / binary 两种格式）；
2. 断点续传清单（segments_state.json）的保存与恢复：
   参数匹配续传、参数不匹配重来、段文件损坏回退、无清单全新生成；
3. 失败后临时分段文件保留（不清空）与成功后清理的行为约束。
"""

import json

from app.services.comfyui_video import (
    SEGMENTS_STATE_FILE,
    _cleanup_segment_files,
    _load_resumable_state,
    _parse_ws_message,
    _save_segments_state,
    get_project_file_path,
)
from app.services.file_store import get_project_dir


def _make_params(prompt="测试提示词", **overrides):
    """构造生成参数字典（与 generate_video_with_comfyui 内部一致）。"""
    params = {"prompt": prompt, "width": 1280, "height": 704, "fps": 24, "duration": 30.0}
    params.update(overrides)
    return params


def _fake_segment_file(project_id: str, idx: int, size: int = 100 * 1024) -> None:
    """伪造有效的分段产物文件（默认 100KB，远超有效性阈值）。"""
    seg_path = get_project_file_path(project_id, f"video_seg_{idx}.mp4")
    seg_path.parent.mkdir(parents=True, exist_ok=True)
    seg_path.write_bytes(b"\x00" * size)


# ---------- WebSocket 消息解析 ----------

def test_parse_ws_message_text_json():
    """text JSON 消息：正常解析类型与数据。"""
    msg_type, data = _parse_ws_message('{"type": "progress", "data": {"value": 5, "max": 20}}')
    assert msg_type == "progress"
    assert data == {"value": 5, "max": 20}


def test_parse_ws_message_binary():
    """binary 消息：按 [4B 类型长度][类型][4B 数据长度][JSON] 解析。"""
    payload = json.dumps({"prompt_id": "abc", "value": 3, "max": 10}).encode()
    raw = len(b"progress").to_bytes(4, "big") + b"progress" + len(payload).to_bytes(4, "big") + payload
    msg_type, data = _parse_ws_message(raw)
    assert msg_type == "progress"
    assert data["value"] == 3


def test_parse_ws_message_garbage():
    """无法解析的内容返回空类型，不抛异常。"""
    assert _parse_ws_message("not json")[0] == ""
    assert _parse_ws_message(b"\x00\x01")[0] == ""


# ---------- 断点续传清单 ----------

def test_resume_no_state_file():
    """无清单时返回 0（全新生成）。"""
    assert _load_resumable_state("proj_no_state", _make_params(), 6) == 0


def test_resume_params_match():
    """参数完全匹配且段文件有效时返回已完成的段数。"""
    pid = "proj_resume_ok"
    params = _make_params()
    _fake_segment_file(pid, 0)
    _fake_segment_file(pid, 1)
    _fake_segment_file(pid, 2)
    _save_segments_state(pid, params, 7, 3)
    assert _load_resumable_state(pid, params, 7) == 3


def test_resume_params_mismatch():
    """任一参数变化（提示词/宽高/帧率/时长/段数）都返回 0。"""
    pid = "proj_resume_mismatch"
    params = _make_params()
    _fake_segment_file(pid, 0)
    _save_segments_state(pid, params, 5, 1)
    # 提示词变化
    assert _load_resumable_state(pid, _make_params(prompt="换了提示词"), 5) == 0
    # 分辨率变化
    assert _load_resumable_state(pid, _make_params(width=832, height=480), 5) == 0
    # 时长变化导致段数变化
    assert _load_resumable_state(pid, _make_params(duration=60.0), 11) == 0


def test_resume_corrupted_segment_falls_back():
    """已完成段文件损坏（过小）时，回退到最近的有效前缀。"""
    pid = "proj_resume_corrupt"
    params = _make_params()
    _fake_segment_file(pid, 0)
    _fake_segment_file(pid, 1)
    # 第 3 段是损坏文件（小于 10KB 阈值）
    _fake_segment_file(pid, 2, size=512)
    _save_segments_state(pid, params, 5, 3)
    assert _load_resumable_state(pid, params, 5) == 2


def test_cleanup_removes_state_and_segments():
    """清理函数删除分段、尾帧、拼接清单与续传清单。"""
    pid = "proj_cleanup"
    params = _make_params()
    _fake_segment_file(pid, 0)
    _fake_segment_file(pid, 1)
    tail = get_project_file_path(pid, "seg_tail_1.png")
    tail.parent.mkdir(parents=True, exist_ok=True)
    tail.write_bytes(b"\x89PNG")
    _save_segments_state(pid, params, 5, 2)

    _cleanup_segment_files(pid)

    project_dir = get_project_dir(pid)
    assert not list(project_dir.glob("video_seg_*.mp4"))
    assert not list(project_dir.glob("seg_tail_*.png"))
    assert not (project_dir / SEGMENTS_STATE_FILE).exists()
