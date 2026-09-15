# 从 ModelScope 直连下载 Fun-CosyVoice3-0.5B-2512 模型（跳过非必需文件以节省带宽）
# 用法：conda 基础环境 python scripts/download_model.py
import os
import sys
import time

import requests

MODEL_ID = 'FunAudioLLM/Fun-CosyVoice3-0.5B-2512'
REVISION = 'master'
TARGET_DIR = os.path.join(
    os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
    'third_party_CosyVoice', 'pretrained_models', 'Fun-CosyVoice3-0.5B-2512',
)

# 跳过的文件：RL 变体模型、batch 版语音分词器（推理用单条版）、
# TRT 加速用的 onnx（load_trt=False）、资源图片
SKIP_FILES = {
    'llm.rl.pt',
    'speech_tokenizer_v3.batch.onnx',
    'flow.decoder.estimator.fp32.onnx',
    'asset/dingding.png',
    '.gitattributes',
}


def list_remote_files():
    """枚举模型仓库全部文件。"""
    url = f'https://modelscope.cn/api/v1/models/{MODEL_ID}/repo/files'
    resp = requests.get(url, params={'Recursive': 'true', 'Revision': REVISION}, timeout=30)
    resp.raise_for_status()
    files = resp.json()['Data']['Files']
    return [(f['Path'], f['Size']) for f in files if f['Type'] == 'blob']


def download_file(path, size, retries=5):
    """下载单个文件（支持断点续传，.part 完成后改名）。"""
    dest = os.path.join(TARGET_DIR, path.replace('/', os.sep))
    os.makedirs(os.path.dirname(dest), exist_ok=True)
    if os.path.exists(dest) and os.path.getsize(dest) == size:
        print(f'[跳过] {path} 已存在')
        return
    part = dest + '.part'
    url = f'https://modelscope.cn/api/v1/models/{MODEL_ID}/repo'
    headers = {}
    if os.path.exists(part):
        headers['Range'] = f'bytes={os.path.getsize(part)}-'
    for attempt in range(1, retries + 1):
        try:
            with requests.get(
                url, params={'Revision': REVISION, 'FilePath': path},
                headers=headers, stream=True, timeout=(15, 120),
            ) as r:
                r.raise_for_status()
                mode = 'ab' if headers else 'wb'
                downloaded = os.path.getsize(part) if headers else 0
                with open(part, mode) as f:
                    for chunk in r.iter_content(chunk_size=4 * 1024 * 1024):
                        f.write(chunk)
                        downloaded += len(chunk)
                if downloaded != size:
                    raise RuntimeError(f'大小不匹配: {downloaded}/{size}')
                os.replace(part, dest)
                print(f'[完成] {path} ({size / 1024 / 1024:.1f}MB)')
                return
        except Exception as exc:
            print(f'[重试 {attempt}/{retries}] {path}: {exc}')
            time.sleep(3 * attempt)
    raise RuntimeError(f'下载失败: {path}')


def main():
    """按清单下载全部必需文件。"""
    print(f'目标目录: {TARGET_DIR}')
    files = list_remote_files()
    todo = [(p, s) for p, s in files if p not in SKIP_FILES]
    total = sum(s for _, s in todo)
    print(f'待下载 {len(todo)} 个文件，共 {total / 1024 / 1024:.0f}MB')
    for path, size in todo:
        download_file(path, size)
    print('全部完成')


if __name__ == '__main__':
    sys.exit(main())
