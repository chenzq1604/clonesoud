# 生成 CosyVoice3 内置音色的预置参考音频
#
# Fun-CosyVoice3-0.5B 为零样本基座模型，不带预置说话人（无 spk2info.pt）。
# 本脚本用 edge-tts（微软神经网络人声）生成若干不同音色的参考音频
# （16kHz 单声道 WAV），写入 builtin_voices/ 目录并生成 manifest.json，
# 供推理服务作为“内置音色”使用（cross-lingual 零样本合成）。
#
# 用法（一次性准备，无需重复执行）：
#   python cosyvoice_service\make_builtin_voices.py [--proxy http://127.0.0.1:7890]

import argparse
import asyncio
import json
import subprocess
import sys
from pathlib import Path

import edge_tts

# 输出目录（与 server.py 的 BUILTIN_DIR 一致）
OUT_DIR = Path(__file__).resolve().parent / "builtin_voices"

# 参考朗读文本：约 15 秒的自然中文，用于提取音色特征
REFERENCE_TEXT = (
    "大家好，欢迎收看今天的节目。在这里，我们每天都会分享一些有趣的故事"
    "和实用的知识，希望能为您的生活带来一点帮助和启发。感谢您的收看，"
    "我们下期再见。"
)

# 内置音色清单：id 用于接口标识，name 展示给用户，voice 为 edge-tts 音色
BUILTIN_VOICES = [
    {"id": "xiaoxiao", "name": "晓晓（女声·温柔）", "voice": "zh-CN-XiaoxiaoNeural"},
    {"id": "xiaoyi", "name": "晓伊（女声·甜美）", "voice": "zh-CN-XiaoyiNeural"},
    {"id": "yunxi", "name": "云希（男声·阳光）", "voice": "zh-CN-YunxiNeural"},
    {"id": "yunjian", "name": "云健（男声·浑厚）", "voice": "zh-CN-YunjianNeural"},
    {"id": "yunyang", "name": "云扬（男声·播音）", "voice": "zh-CN-YunyangNeural"},
]


async def synthesize_one(item: dict, proxy: str | None) -> Path:
    """用 edge-tts 生成单个音色的参考音频并转为 16kHz 单声道 WAV。"""
    wav_path = OUT_DIR / f"{item['id']}.wav"
    if wav_path.exists():
        print(f"[跳过] {wav_path.name} 已存在")
        return wav_path

    mp3_path = OUT_DIR / f"{item['id']}.mp3"
    communicate = edge_tts.Communicate(REFERENCE_TEXT, item["voice"], proxy=proxy)
    await communicate.save(str(mp3_path))

    # 统一转换为 16kHz 单声道 PCM WAV（CosyVoice 参考音频格式）
    subprocess.run(
        [
            "ffmpeg", "-y", "-i", str(mp3_path),
            "-ar", "16000", "-ac", "1",
            "-acodec", "pcm_s16le", str(wav_path),
        ],
        stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, check=True,
    )
    mp3_path.unlink(missing_ok=True)
    print(f"[完成] {item['name']} -> {wav_path.name}")
    return wav_path


async def main_async(proxy: str | None) -> None:
    """生成全部内置音色参考音频与 manifest.json。"""
    OUT_DIR.mkdir(parents=True, exist_ok=True)
    for item in BUILTIN_VOICES:
        await synthesize_one(item, proxy)

    manifest = {
        "voices": [
            {"id": item["id"], "name": item["name"], "file": f"{item['id']}.wav"}
            for item in BUILTIN_VOICES
        ]
    }
    manifest_path = OUT_DIR / "manifest.json"
    manifest_path.write_text(
        json.dumps(manifest, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    print(f"[完成] 清单已写入 {manifest_path}")


def main() -> None:
    """解析代理参数并执行生成。"""
    parser = argparse.ArgumentParser(description="生成内置音色参考音频")
    parser.add_argument("--proxy", default=None, help="HTTP 代理地址（访问微软服务）")
    args = parser.parse_args()
    asyncio.run(main_async(args.proxy))


if __name__ == "__main__":
    sys.exit(main())
