"""
RealSight 的本地 PaddleOCR 适配器。

文件整体逻辑
------------
本模块把 PaddleOCR 3.x 的 ``predict`` 结果转换为第 11 章已经固定的
``RecognitionDocument``。PaddleOCR 返回像素坐标、多边形、文本和识别分数；适配器
读取原图尺寸，将多边形收敛为 0～1 相对矩形，并为每个文字区域生成稳定 ID。模型只在
适配器构造时初始化一次，普通测试可注入假 pipeline，因此不会下载模型。

技术栈与调用流程
----------------
PaddleOCR/ONNX Runtime -> result.json -> rec_texts/rec_scores/rec_polys
    -> TextRegion -> RecognitionDocument -> VisionEvidenceAgent -> Evidence。

边界
----
本模块不解析 USB-C 功率或协议，不控制摄像头，也不在普通安装中强制引入重型 OCR
依赖。只有配置 ``vision.provider=paddleocr`` 时才导入可选包；缺依赖时启动会给出明确
安装命令，而不会退化成伪造的识别结果。
"""

from __future__ import annotations

import hashlib
import importlib.metadata
import time
from collections.abc import Callable, Iterable, Mapping, Sequence
from pathlib import Path
from typing import Any, Protocol, cast

from realsight.vision.evidence_agent import (
    RecognitionDocument,
    RecognitionFailure,
    TextRegion,
)


class _PaddlePipeline(Protocol):
    """PaddleOCR 与测试替身共同满足的最小推理接口。"""

    def predict(self, input_path: str) -> Iterable[Any]:
        """返回一张图片的 OCR Result 迭代器。"""


ImageSizeReader = Callable[[Path], tuple[int, int]]


def _read_image_size(image_path: Path) -> tuple[int, int]:
    """使用 OCR 可选依赖自带的 Pillow 读取尺寸，不引入第二份 OpenCV wheel。"""

    try:
        from PIL import Image  # type: ignore[import-not-found]
    except ImportError as exc:  # pragma: no cover - 只在损坏的可选安装中发生。
        raise RecognitionFailure(
            "Pillow is required to normalize PaddleOCR region coordinates"
        ) from exc
    try:
        with Image.open(image_path) as image:
            width, height = image.size
    except OSError as exc:
        raise RecognitionFailure(f"cannot read OCR image dimensions: {exc}") from exc
    if width <= 0 or height <= 0:
        raise RecognitionFailure("OCR image dimensions must be positive")
    return width, height


def _load_pipeline(*, engine: str, ocr_version: str) -> _PaddlePipeline:
    """延迟加载可选依赖；默认离线课程和 CI 不会导入或下载 OCR 模型。"""

    try:
        from paddleocr import PaddleOCR  # type: ignore[import-not-found]
    except ImportError as exc:
        raise RuntimeError(
            "PaddleOCR is not installed; run `uv sync --extra ocr` before setting "
            "vision.provider=paddleocr"
        ) from exc
    try:
        return cast(
            _PaddlePipeline,
            PaddleOCR(
                engine=engine,
                ocr_version=ocr_version,
                use_doc_orientation_classify=False,
                use_doc_unwarping=False,
                use_textline_orientation=False,
            ),
        )
    except Exception as exc:  # 第三方初始化异常必须带部署上下文。
        raise RuntimeError(
            f"cannot initialize local PaddleOCR pipeline: {exc}"
        ) from exc


class PaddleOcrTextRecognizer:
    """将本地 PaddleOCR 结果适配为 RealSight 的严格文字区域契约。"""

    def __init__(
        self,
        *,
        engine: str = "onnxruntime",
        ocr_version: str = "PP-OCRv6",
        pipeline: _PaddlePipeline | None = None,
        image_size_reader: ImageSizeReader | None = None,
        package_version: str | None = None,
    ) -> None:
        if not engine.strip() or not ocr_version.strip():
            raise ValueError("OCR engine and version must not be empty")
        self._engine = engine
        self._ocr_version = ocr_version
        self._pipeline = (
            pipeline
            if pipeline is not None
            else _load_pipeline(engine=engine, ocr_version=ocr_version)
        )
        self._image_size_reader = image_size_reader or _read_image_size
        self._package_version = package_version or self._installed_version()

    @staticmethod
    def _installed_version() -> str:
        try:
            return importlib.metadata.version("paddleocr")
        except importlib.metadata.PackageNotFoundError:
            # 只有注入测试 pipeline 时可能没有安装真实发行包。
            return "injected-test-pipeline"

    def recognize(
        self,
        image_path: Path,
        *,
        observation_id: str,
    ) -> RecognitionDocument:
        """执行一次本地推理，并拒绝结构不完整或坐标退化的第三方结果。"""

        if not image_path.is_file():
            raise RecognitionFailure(f"OCR image does not exist: {image_path}")
        width, height = self._image_size_reader(image_path)
        if width <= 0 or height <= 0:
            raise RecognitionFailure("OCR image dimensions must be positive")

        started = time.perf_counter()
        try:
            results = tuple(self._pipeline.predict(str(image_path)))
        except Exception as exc:
            raise RecognitionFailure(f"PaddleOCR inference failed: {exc}") from exc
        elapsed_ms = round((time.perf_counter() - started) * 1_000, 3)

        regions: list[TextRegion] = []
        for page_index, result in enumerate(results):
            payload = self._result_payload(result)
            texts = self._sequence(payload.get("rec_texts"), "rec_texts")
            scores = self._sequence(payload.get("rec_scores"), "rec_scores")
            polygons = self._sequence(payload.get("rec_polys"), "rec_polys")
            if not (len(texts) == len(scores) == len(polygons)):
                raise RecognitionFailure(
                    "PaddleOCR rec_texts, rec_scores and rec_polys lengths differ"
                )
            for line_index, (text, score, polygon) in enumerate(
                zip(texts, scores, polygons, strict=True)
            ):
                if not isinstance(text, str):
                    raise RecognitionFailure("PaddleOCR rec_texts must contain strings")
                normalized_text = text.strip()
                if not normalized_text:
                    continue
                if len(normalized_text) > 1_024:
                    raise RecognitionFailure(
                        "PaddleOCR returned an overlong text region"
                    )
                confidence = self._unit_float(score, "rec_scores")
                left, top, right, bottom = self._relative_bounds(
                    polygon, width=width, height=height
                )
                digest = hashlib.sha256(
                    (
                        f"{observation_id}|{page_index}|{line_index}|"
                        f"{normalized_text}|{left:.6f}|{top:.6f}|{right:.6f}|{bottom:.6f}"
                    ).encode()
                ).hexdigest()[:16]
                regions.append(
                    TextRegion(
                        region_id=f"ocr-{digest}",
                        text=normalized_text,
                        ocr_confidence=confidence,
                        left=left,
                        top=top,
                        right=right,
                        bottom=bottom,
                    )
                )

        return RecognitionDocument(
            observation_id=observation_id,
            regions=tuple(regions),
            provider_metadata={
                "provider": "paddleocr",
                "package_version": self._package_version,
                "ocr_version": self._ocr_version,
                "engine": self._engine,
                "inference_ms": elapsed_ms,
                "image_width": width,
                "image_height": height,
            },
        )

    @staticmethod
    def _result_payload(result: Any) -> Mapping[str, Any]:
        raw = getattr(result, "json", result)
        if callable(raw):
            raw = raw()
        if not isinstance(raw, Mapping):
            raise RecognitionFailure("PaddleOCR result.json must be a mapping")
        nested = raw.get("res", raw)
        if not isinstance(nested, Mapping):
            raise RecognitionFailure("PaddleOCR result res must be a mapping")
        return nested

    @staticmethod
    def _sequence(value: Any, field_name: str) -> Sequence[Any]:
        if hasattr(value, "tolist"):
            value = value.tolist()
        if not isinstance(value, (list, tuple)):
            raise RecognitionFailure(f"PaddleOCR {field_name} must be a sequence")
        return value

    @staticmethod
    def _unit_float(value: Any, field_name: str) -> float:
        if isinstance(value, bool) or not isinstance(value, (int, float)):
            raise RecognitionFailure(f"PaddleOCR {field_name} must contain numbers")
        converted = float(value)
        if not 0.0 <= converted <= 1.0:
            raise RecognitionFailure(
                f"PaddleOCR {field_name} must be between zero and one"
            )
        return converted

    @classmethod
    def _relative_bounds(
        cls,
        polygon: Any,
        *,
        width: int,
        height: int,
    ) -> tuple[float, float, float, float]:
        points = cls._sequence(polygon, "rec_polys item")
        if len(points) < 3:
            raise RecognitionFailure("PaddleOCR polygon requires at least three points")
        xs: list[float] = []
        ys: list[float] = []
        for point in points:
            coordinates = cls._sequence(point, "rec_polys point")
            if len(coordinates) != 2:
                raise RecognitionFailure("PaddleOCR polygon points require x and y")
            x, y = coordinates
            if (
                isinstance(x, bool)
                or isinstance(y, bool)
                or not isinstance(x, (int, float))
                or not isinstance(y, (int, float))
            ):
                raise RecognitionFailure(
                    "PaddleOCR polygon coordinates must be numeric"
                )
            xs.append(float(x))
            ys.append(float(y))
        left = max(0.0, min(1.0, min(xs) / width))
        top = max(0.0, min(1.0, min(ys) / height))
        right = max(0.0, min(1.0, max(xs) / width))
        bottom = max(0.0, min(1.0, max(ys) / height))
        if right <= left or bottom <= top:
            raise RecognitionFailure("PaddleOCR polygon collapses after normalization")
        return left, top, right, bottom

    def close(self) -> None:
        """尽力释放未来 pipeline 可能公开的资源；当前实现通常是无操作。"""

        close = getattr(self._pipeline, "close", None)
        if callable(close):
            close()


__all__ = ["PaddleOcrTextRecognizer"]
