"""
第 10 章生成代码包。

文件整体逻辑
------------
这个包只容纳由 ``contracts/realsight.proto`` 生成的 Python message 与 gRPC stub。
业务代码通过 ``realsight.perception.grpc_client`` 访问它们，避免其他模块直接依赖
生成器细节。

使用的技术栈：Protocol Buffers、grpcio、Python 包导入。
调用流程：grpc_client -> generated.realsight_pb2/_grpc -> C++ gRPC 服务。
边界：这里不放业务模型、证据规则、LangGraph 状态或手写网络重试策略。
"""
