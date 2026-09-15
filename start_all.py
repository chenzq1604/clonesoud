"""
一键启动全部本地服务（分离进程，不受会话生命周期影响）

启动清单：
1. 后端 FastAPI（av-clone 环境，端口 8000）
2. CosyVoice 推理服务（cosyvoice 环境，端口 9880）
3. ComfyUI（comfyui 环境，端口 8188）
4. 前端 Vite 开发服务器（端口 5173 起）

日志统一写入各服务目录下的 *.log 文件，便于排查。
已监听的端口自动跳过，支持重复执行。
"""

import os
import socket
import subprocess
import time

ROOT = r"d:\source\trae\clonempeg"

# 服务定义：名称 / 端口 / 命令 / 工作目录 / 环境变量
SERVICES = [
    {
        "name": "后端 FastAPI",
        "port": 8000,
        "cmd": [
            r"D:\ProgramData\anaconda3\envs\av-clone\python.exe",
            "-m", "uvicorn", "app.main:app",
            "--host", "127.0.0.1", "--port", "8000",
        ],
        "cwd": rf"{ROOT}\backend",
        "env": {},
        "log": rf"{ROOT}\backend\uvicorn.log",
    },
    {
        "name": "CosyVoice 推理",
        "port": 9880,
        "cmd": [
            r"D:\ProgramData\anaconda3\envs\cosyvoice\python.exe",
            "server.py", "--host", "127.0.0.1", "--port", "9880",
        ],
        "cwd": rf"{ROOT}\cosyvoice_service",
        "env": {},
        "log": rf"{ROOT}\cosyvoice_service\server.log",
    },
    {
        "name": "ComfyUI",
        "port": 8188,
        "cmd": [
            r"D:\ProgramData\anaconda3\envs\comfyui\python.exe",
            "main.py", "--listen", "127.0.0.1", "--port", "8188",
        ],
        "cwd": rf"{ROOT}\third_party_ComfyUI",
        # 项目内 pip 依赖目录需通过 PYTHONPATH 生效
        "env": {"PYTHONPATH": rf"{ROOT}\third_party_ComfyUI\.pydeps"},
        "log": rf"{ROOT}\third_party_ComfyUI\comfyui.log",
    },
    {
        "name": "前端 Vite",
        "port": 5173,
        "cmd": ["cmd", "/c", "npm", "run", "dev"],
        "cwd": rf"{ROOT}\frontend",
        "env": {},
        "log": rf"{ROOT}\frontend\vite.log",
    },
]


def port_listening(port: int) -> bool:
    """
    用原生 socket 探测端口是否监听（IPv4 与 IPv6 都探测）。

    注意不能用 httpx 探测：本机连接关闭端口表现为超时（ConnectTimeout），
    会被误判为端口存活；且 vite 默认绑定 IPv6 回环 [::1]，仅测 IPv4 会漏检。
    """
    for family, addr in ((socket.AF_INET, ("127.0.0.1", port)),
                         (socket.AF_INET6, ("::1", port))):
        s = socket.socket(family)
        s.settimeout(1.5)
        try:
            s.connect(addr)
            return True
        except Exception:
            continue
        finally:
            s.close()
    return False


def wait_ready(port: int, timeout: float, service: dict) -> bool:
    """轮询等待服务就绪；ComfyUI 冷启动加载模型需 1~2 分钟。"""
    start = time.time()
    while time.time() - start < timeout:
        if port_listening(port):
            return True
        time.sleep(2)
    return False


def main() -> None:
    started = []
    skipped = []
    for svc in SERVICES:
        if port_listening(svc["port"]):
            skipped.append(svc["name"])
            print(f"[跳过] {svc['name']} 端口 {svc['port']} 已在运行")
            continue

        log_file = open(svc["log"], "ab")
        # 必须继承完整系统环境（PATH/SYSTEMROOT 等），再追加服务专属变量；
        # 传空环境会导致 npm 找不到、Python 加密/IOCP 初始化失败
        env = {**os.environ, **svc["env"]}
        flags = (
            subprocess.DETACHED_PROCESS
            | subprocess.CREATE_NEW_PROCESS_GROUP
            | subprocess.CREATE_NO_WINDOW
        )
        proc = subprocess.Popen(
            svc["cmd"],
            cwd=svc["cwd"],
            stdin=subprocess.DEVNULL,
            stdout=log_file,
            stderr=subprocess.STDOUT,
            env=env,
            creationflags=flags,
        )
        # 冷启动超时：ComfyUI 需加载大模型，放宽到 150 秒；其余 60 秒
        timeout = 150 if svc["port"] == 8188 else 60
        if wait_ready(svc["port"], timeout, svc):
            started.append(svc["name"])
            print(f"[启动] {svc['name']} 端口 {svc['port']} pid={proc.pid}")
        else:
            print(f"[失败] {svc['name']} 端口 {svc['port']} 超时未就绪，日志: {svc['log']}")

    print(f"\n完成：新启动 {len(started)} 个，已运行跳过 {len(skipped)} 个")
    print("访问前端: http://127.0.0.1:5173")


if __name__ == "__main__":
    main()
