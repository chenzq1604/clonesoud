"""
TTS 长文本分段合成回归测试

回归背景：
CosyVoice3 要求合成文本携带 BLANK_PREFIX（"You are a helpful assistant.<|endofprompt|>"），
但该前缀含有 <| 与 |>，会让 CosyVoice 前端的 text_normalize 判定为 ssml 而跳过
前端处理与切分（见 cosyvoice/cli/frontend.py）。结果是整篇长文作为**单次生成**
送入 LLM：模型在超长上下文下提前"想收尾"，却因 min_len（文本 token 数 ×2）
压制 EOS 而被迫继续输出，表现为
  1. 开头对不上原文（像从中间开始）
  2. 中间没有停顿
  3. 后段退化成听不清的内容
实测 964 字仅产出 45 秒（约 21 字/秒），而正常朗读约 4~6 字/秒。

修复后：先用**不带前缀的原文**切分为短片段，再逐段合成并在段间插入静音。

说明：本测试需要本地 CosyVoice 推理服务在线（GPU 推理，单次约 1~2 分钟），
服务或参考录音不可用时自动跳过，避免影响其他环境。
"""

from pathlib import Path

import httpx
import pytest

COSYVOICE_URL = "http://127.0.0.1:9880"
UPLOADS_ROOT = Path(__file__).resolve().parent.parent.parent / "uploads"

# 约 200 字，足以触发多段切分（切分阈值约 80 token/段）
LONG_TEXT = (
    "春江潮水连海平，海上明月共潮生。滟滟随波千万里，何处春江无月明。"
    "江流宛转绕芳甸，月照花林皆似霰。空里流霜不觉飞，汀上白沙看不见。"
    "江天一色无纤尘，皎皎空中孤月轮。江畔何人初见月，江月何年初照人。"
    "人生代代无穷已，江月年年望相似。不知江月待何人，但见长江送流水。"
    "白云一片去悠悠，青枫浦上不胜愁。谁家今夜扁舟子，何处相思明月楼。"
    "可怜楼上月裴回，应照离人妆镜台。"
)


def _service_available() -> bool:
    """CosyVoice 推理服务是否在线且模型已加载。"""
    try:
        resp = httpx.get(f"{COSYVOICE_URL}/health", timeout=3.0)
        return resp.status_code == 200 and resp.json().get("status") == "ok"
    except Exception:
        return False


def _find_reference_audio() -> Path | None:
    """在真实上传目录中查找任意一份克隆参考录音（raw_recording.wav）。"""
    if not UPLOADS_ROOT.exists():
        return None
    for path in sorted(UPLOADS_ROOT.glob("*/raw_recording.wav")):
        if path.stat().st_size > 0:
            return path
    return None


_REFERENCE = _find_reference_audio()

pytestmark = pytest.mark.skipif(
    not _service_available() or _REFERENCE is None,
    reason="需要本地 CosyVoice 推理服务在线且存在参考录音",
)


def _synthesize_long_text(out_dir: Path) -> dict:
    """对长文本发起一次合成，返回服务响应。"""
    payload = {
        "text": LONG_TEXT,
        "ref_audio_path": str(_REFERENCE),
        "output_path": str(out_dir / "long.wav"),
        "speed": 1.0,
    }
    resp = httpx.post(f"{COSYVOICE_URL}/api/tts", json=payload, timeout=1800.0)
    resp.raise_for_status()
    return resp.json()


@pytest.fixture(scope="module")
def long_text_result(tmp_path_factory) -> dict:
    """
    长文本合成结果。

    整模块只实际推理一次（单次耗时约 1~2 分钟），避免重复占用 GPU。
    """
    out_dir = tmp_path_factory.mktemp("tts_long")
    return _synthesize_long_text(out_dir)


def test_long_text_is_split_into_segments(long_text_result):
    """
    长文本应被切分为多段合成，而非整篇作为单次生成。

    这是本次回归的核心断言：修复前服务端没有 segments 概念（恒为单段）。
    """
    assert long_text_result["segments"] >= 2, f"长文本未分段: {long_text_result}"


def test_long_text_speech_rate_is_reasonable(long_text_result):
    """
    产物的语音速率应落在正常朗读区间。

    修复前整段生成会提前收尾，速率高达约 21 字/秒（约 3/4 内容丢失）；
    正常中文朗读约 4~6 字/秒，此处放宽到 2~7 字/秒以确保稳健。
    """
    chars_per_second = len(LONG_TEXT) / long_text_result["duration"]
    assert 2.0 <= chars_per_second <= 7.0, (
        f"语音速率异常: {chars_per_second:.1f} 字/秒 "
        f"(时长 {long_text_result['duration']}s, 文本 {len(LONG_TEXT)} 字)"
    )


def test_segment_count_matches_text_length(long_text_result):
    """
    分段数应与文本长度大致匹配（约每 80 token 一段），避免切分失效或过度切分。
    """
    # 约 200 字按前端规则应切为 2~6 段
    segments = long_text_result["segments"]
    assert 2 <= segments <= 6, f"分段数不合理: {segments}"
