"""
第 10 章 Python Protobuf/gRPC 代码生成器。

文件整体逻辑
------------
本脚本把唯一协议源 ``contracts/realsight.proto`` 编译成 Python message 类和 gRPC
stub，输出到正式包 ``realsight.generated``。grpcio-tools 默认生成同目录绝对导入，
脚本会机械地改为包内相对导入，并给两个生成文件加上中文总览，方便初学者知道哪些
内容应阅读、哪些内容应交给工具维护。

使用的技术栈
------------
- Python 3.12 pathlib：定位项目根和生成目录。
- grpcio-tools/protoc：生成 ``*_pb2.py`` 与 ``*_pb2_grpc.py``。
- 标准库文本处理：只修正生成器已知的导入行和添加教学说明，不改 descriptor 数据。

调用流程
--------
``uv run python scripts/generate_python_proto.py``
    -> protoc.main(--python_out, --grpc_python_out)
    -> 检查两个输出文件存在
    -> 把 ``import realsight_pb2`` 改为包内导入
    -> 添加生成文件说明
    -> 后续由 ruff/pytest 和真实 gRPC Demo 验证。

边界
----
生成文件不可手工维护；协议变更后应重新运行本脚本。业务校验和 Pydantic 映射必须写在
``realsight.perception.grpc_client``，不能偷偷塞进 descriptor 或 generated stub。
"""

from __future__ import annotations

from pathlib import Path

from grpc_tools import protoc

PROJECT_ROOT = Path(__file__).resolve().parents[1]
CONTRACTS_DIR = PROJECT_ROOT / "contracts"
PROTO_FILE = CONTRACTS_DIR / "realsight.proto"
OUTPUT_DIR = PROJECT_ROOT / "python" / "src" / "realsight" / "generated"

MESSAGE_HEADER = '''"""
自动生成的 Protobuf 消息代码。

文件整体逻辑：由 realsight.proto 生成 Python 消息、枚举和 descriptor。
使用的技术栈：Protocol Buffers Python runtime；内容由 protoc 维护。
调用流程：手写 grpc_client 构造/读取消息 -> 本模块序列化或解析 wire bytes。
阅读边界：不要手改本文件的 descriptor；修改 contracts/realsight.proto 后重新生成。
"""

'''

GRPC_HEADER = '''"""
自动生成的 Python gRPC stub 与服务基类。

文件整体逻辑：由 realsight.proto 生成 PerceptionRuntimeStub 和服务注册函数。
使用的技术栈：grpcio Python runtime；内容由 grpcio-tools 维护。
调用流程：手写 PerceptionClient -> PerceptionRuntimeStub.Observe/Cancel -> C++ 服务。
阅读边界：业务 deadline、校验和 Pydantic 适配位于 grpc_client.py，不手改本文件。
"""

'''


def add_header(path: Path, header: str) -> None:
    """幂等添加教学头部，避免重复运行后不断叠加注释。"""

    text = path.read_text(encoding="utf-8")
    marker = "自动生成的"
    if marker not in text[:500]:
        path.write_text(header + text, encoding="utf-8")


def main() -> int:
    """执行代码生成，并对包内导入与学习头部做确定性后处理。"""

    OUTPUT_DIR.mkdir(parents=True, exist_ok=True)
    exit_code = protoc.main(
        [
            "grpc_tools.protoc",
            f"-I{CONTRACTS_DIR}",
            f"--python_out={OUTPUT_DIR}",
            f"--pyi_out={OUTPUT_DIR}",
            f"--grpc_python_out={OUTPUT_DIR}",
            str(PROTO_FILE),
        ]
    )
    if exit_code != 0:
        raise RuntimeError(f"grpc_tools.protoc failed with exit code {exit_code}")

    message_path = OUTPUT_DIR / "realsight_pb2.py"
    message_stub_path = OUTPUT_DIR / "realsight_pb2.pyi"
    grpc_path = OUTPUT_DIR / "realsight_pb2_grpc.py"
    if not all(path.is_file() for path in (message_path, message_stub_path, grpc_path)):
        raise RuntimeError("protoc did not create the expected Python files")

    grpc_text = grpc_path.read_text(encoding="utf-8")
    grpc_text = grpc_text.replace(
        "import realsight_pb2 as realsight__pb2",
        "from . import realsight_pb2 as realsight__pb2",
    )
    grpc_path.write_text(grpc_text, encoding="utf-8")
    add_header(message_path, MESSAGE_HEADER)
    add_header(message_stub_path, MESSAGE_HEADER)
    add_header(grpc_path, GRPC_HEADER)
    print(f"generated Python gRPC modules in {OUTPUT_DIR}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
