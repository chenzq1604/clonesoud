# 生成 CosyVoice3 内置音色的试听片段（约 2 秒）
#
# 与 make_builtin_voices.py（生成 15 秒参考音频）不同，本脚本为每个
# 内置音色生成一句约 2 秒的示例语音，用于前端"试听原音"按钮，
# 让用户在合成前了解音色风格。片段通过本地推理服务合成，
# 试听效果即真实合成效果。
#
# 用法（需先启动推理服务 scripts\start_cosyvoice.bat）：
#   python cosyvoice_service\make_builtin_previews.py

import json
import sys
from pathlib import Path

import httpx

# 输出目录（与后端 app/services/tts.py 的 previews 解析一致）
OUT_DIR = Path(__file__).resolve().parent / "builtin_voices" / "previews"

# 本地推理服务地址
BASE_URL = "http://127.0.0.1:9880"

# 各音色的试听文案（未配置的音色使用通用文案）
PREVIEW_TEXTS = {
    "xiaoxiao": "你好，我是晓晓。",
    "xiaoyi": "你好，我是晓伊。",
    "yunxi": "你好，我是云希。",
    "yunjian": "你好，我是云健。",
    "yunyang": "你好，我是云扬。",
}
FALLBACK_TEXT = "你好，欢迎试听这个音色。"


def load_manifest() -> list[dict]:
    """读取内置音色清单，返回 [{'id', 'name', 'file'}, ...]。"""
    manifest_path = OUT_DIR.parent / "manifest.json"
    data = json.loads(manifest_path.read_text(encoding="utf-8"))
    return data.get("voices", [])


def main() -> int:
    """逐个合成内置音色试听片段，已存在的跳过（幂等）。"""
    OUT_DIR.mkdir(parents=True, exist_ok=True)
    voices = load_manifest()
    if not voices:
        print("[错误] 内置音色清单为空，请先运行 make_builtin_voices.py")
        return 1

    failed = 0
    with httpx.Client(timeout=120.0) as client:
        # 先确认推理服务可用
        try:
            health = client.get(f"{BASE_URL}/health")
            health.raise_for_status()
            print(f"[服务] {health.json()}")
        except Exception as exc:
            print(f"[错误] 推理服务不可达: {exc}，请先启动 scripts\\start_cosyvoice.bat")
            return 1

        for item in voices:
            out_path = OUT_DIR / f"{item['id']}.wav"
            if out_path.exists():
                print(f"[跳过] {item['name']} -> {out_path.name} 已存在")
                continue
            text = PREVIEW_TEXTS.get(item["id"], FALLBACK_TEXT)
            resp = client.post(
                f"{BASE_URL}/api/tts",
                json={
                    "text": text,
                    "speaker_id": item["id"],
                    "ref_audio_path": None,
                    "speed": 1.0,
                    "output_path": str(out_path),
                },
            )
            if resp.status_code != 200:
                print(f"[失败] {item['name']}: HTTP {resp.status_code} {resp.text[:200]}")
                failed += 1
                continue
            duration = resp.json().get("duration")
            print(f"[完成] {item['name']} -> {out_path.name} 时长 {duration}s")

    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
