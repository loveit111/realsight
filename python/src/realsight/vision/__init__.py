"""
RealSight 视觉证据包的公共入口。

文件整体逻辑
------------
第 11 章在这里把一张已通过质量筛选的 Observation 转成可追溯 Evidence。公共入口只
暴露“文字区域、识别文档、证据缺口、视觉证据 Agent”这些稳定概念，调用者不必知道
正则归一化或识别后端放在哪个内部文件。

使用的技术栈
------------
- Python 3.12 类型标注和 ``Protocol``，隔离可替换的 OCR/VLM 后端。
- Pydantic v2 领域契约，保证视觉输出不能绕过 Evidence 的来源与置信度约束。

调用流程
--------
LangGraph/服务层 -> VisionEvidenceAgent.extract()
    -> TextRecognizer（真实 OCR 或教学替身）
    -> RecognitionDocument
    -> Evidence / EvidenceGap
    -> 第 12 章规则与第 13 章主工作流。

边界
----
本包不读取连续视频、不控制 gRPC、不保存 checkpoint，也不判定 USB-C 是否兼容；它只
处理已经落盘的单张关键帧及其文本证据。
"""

from .evidence_agent import (
    EvidenceGap,
    RecognitionDocument,
    RecognitionFailure,
    TextRecognizer,
    TextRegion,
    VisionEvidenceAgent,
    VisionExtraction,
)
from .paddle_ocr import PaddleOcrTextRecognizer

__all__ = [
    "EvidenceGap",
    "RecognitionDocument",
    "RecognitionFailure",
    "PaddleOcrTextRecognizer",
    "TextRecognizer",
    "TextRegion",
    "VisionEvidenceAgent",
    "VisionExtraction",
]
