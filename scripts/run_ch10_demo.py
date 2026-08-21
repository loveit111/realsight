"""
第 10 章一键跨语言联调脚本。

文件整体逻辑
------------
本脚本为初学者把三个必要步骤串起来：若测试视频不存在，先调用 C++ 生成器；再选择
一个空闲本机端口启动 ``realsight-perception-grpc --once``；最后用当前 Python 解释器
运行第 10 章客户端 Demo。它检查服务 ready、客户端退出码、最终 Observation 和服务
正常退出，任何一步失败都会保留 stdout/stderr 供定位。

使用的技术栈
------------
- Python 3.12 subprocess/socket/concurrent.futures/pathlib。
- 已编译的 C++ OpenCV/gRPC 可执行文件。
- ``examples/ch10_observation_stream.py`` 手写 Python 客户端。

调用流程
--------
run_ch10_demo
    -> ensure_video() 调 C++ VideoWriter 工具
    -> choose_port()
    -> Popen(C++ gRPC server --once)
    -> 等待 SERVER_READY
    -> subprocess.run(Python client)
    -> 等待服务优雅退出
    -> 检查输出包含最终 observation JSON。

边界
----
这是本机教学联调器，不是进程守护器、容器编排或生产健康检查。空闲端口在选择与绑定间
仍存在极小竞争窗口；CI 若发生端口抢占应重跑，而不是把这种脚本当服务发现系统。
"""

from __future__ import annotations

import argparse
import json
import os
import socket
import subprocess
import sys
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]


def choose_port() -> int:
    """让操作系统分配一个当前空闲的 loopback TCP 端口。"""

    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as listener:
        listener.bind(("127.0.0.1", 0))
        return int(listener.getsockname()[1])


def ensure_video(generator: Path, video: Path) -> None:
    """测试视频不存在时调用 C++ 工具生成，避免 Python 复制 OpenCV 逻辑。"""

    if video.is_file():
        return
    video.parent.mkdir(parents=True, exist_ok=True)
    completed = subprocess.run(
        [str(generator), str(video), "24"],
        cwd=PROJECT_ROOT,
        capture_output=True,
        text=True,
        encoding="utf-8",
        timeout=20,
        check=False,
    )
    if completed.returncode != 0:
        raise RuntimeError(
            "test video generation failed:\n" + completed.stdout + completed.stderr
        )
    print(completed.stdout.strip())


def main(argv: list[str] | None = None) -> int:
    """启动一个请求生命周期的服务，运行客户端并汇总跨语言验收结果。"""

    parser = argparse.ArgumentParser(description="Run RealSight Chapter 10 demo")
    parser.add_argument(
        "--build-dir",
        type=Path,
        default=PROJECT_ROOT / "build" / "windows-gcc-debug" / "cpp",
    )
    parser.add_argument(
        "--video",
        type=Path,
        default=PROJECT_ROOT / "runtime-data" / "ch10-demo.avi",
    )
    parser.add_argument(
        "--artifacts",
        type=Path,
        default=PROJECT_ROOT / "runtime-data" / "ch10-artifacts",
    )
    parser.add_argument(
        "--verify-cancel",
        action="store_true",
        help="运行显式 Cancel RPC 场景，而不是成功关键帧场景",
    )
    args = parser.parse_args(argv)

    executable_suffix = ".exe" if os.name == "nt" else ""
    server_executable = args.build_dir / (
        "realsight-perception-grpc" + executable_suffix
    )
    generator_executable = args.build_dir / (
        "realsight-generate-video" + executable_suffix
    )
    for executable in (server_executable, generator_executable):
        if not executable.is_file():
            raise FileNotFoundError(f"build Chapter 10 first; missing {executable}")
    ensure_video(generator_executable, args.video)

    port = choose_port()
    address = f"127.0.0.1:{port}"
    server_command = [
        str(server_executable),
        "--video",
        str(args.video),
        "--listen",
        address,
        "--artifacts",
        str(args.artifacts),
        "--max-frames",
        "24",
        "--progress-interval",
        "6",
        "--once",
    ]
    if not args.verify_cancel:
        server_command.append("--no-realtime")
    server = subprocess.Popen(
        server_command,
        cwd=PROJECT_ROOT,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        encoding="utf-8",
    )

    try:
        if server.stdout is None:
            raise RuntimeError("server stdout pipe was not created")
        with ThreadPoolExecutor(max_workers=1) as executor:
            ready_line = executor.submit(server.stdout.readline).result(timeout=10)
        if "SERVER_READY" not in ready_line:
            stderr = server.stderr.read() if server.stderr is not None else ""
            raise RuntimeError(f"server did not become ready: {ready_line}{stderr}")
        print(ready_line.strip())

        client_command = [
            sys.executable,
            str(PROJECT_ROOT / "examples" / "ch10_observation_stream.py"),
            "--address",
            address,
            "--timeout-ms",
            "5000",
        ]
        if args.verify_cancel:
            client_command.append("--cancel-after-source-opened")
        client = subprocess.run(
            client_command,
            cwd=PROJECT_ROOT,
            capture_output=True,
            text=True,
            encoding="utf-8",
            timeout=20,
            check=False,
        )
        print(client.stdout, end="")
        if client.stderr:
            print(client.stderr, file=sys.stderr, end="")
        if client.returncode != 0:
            raise RuntimeError(f"Python client exited with {client.returncode}")

        remaining_stdout, server_stderr = server.communicate(timeout=10)
        if remaining_stdout:
            print(remaining_stdout, end="")
        if server_stderr:
            print(server_stderr, file=sys.stderr, end="")
        if server.returncode != 0:
            raise RuntimeError(f"C++ server exited with {server.returncode}")

        payloads = []
        for line in client.stdout.splitlines():
            try:
                payloads.append(json.loads(line))
            except json.JSONDecodeError:
                continue
        if args.verify_cancel:
            if "CANCEL_DEMO_OK" not in client.stdout:
                raise RuntimeError("client did not confirm CANCELLED status")
            print("CH10_CANCEL_OK cancel_rpc=accepted status=CANCELLED")
        else:
            if not any(item.get("kind") == "observation" for item in payloads):
                raise RuntimeError("client output did not contain a final observation")
            print("CH10_DEMO_OK transport=cpp_to_python artifact=verified")
        return 0
    finally:
        if server.poll() is None:
            server.terminate()
            try:
                server.wait(timeout=5)
            except subprocess.TimeoutExpired:
                server.kill()
                server.wait(timeout=5)


if __name__ == "__main__":
    raise SystemExit(main())
