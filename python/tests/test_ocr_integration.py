"""显式启用的真实 PaddleOCR 模型冒烟；普通 CI 不下载依赖或模型。"""

from __future__ import annotations

import os
from pathlib import Path

import pytest
from realsight.vision import PaddleOcrTextRecognizer


@pytest.mark.integration
def test_real_paddleocr_pipeline_on_operator_supplied_image() -> None:
    raw_path = os.environ.get("REALSIGHT_OCR_TEST_IMAGE")
    if raw_path is None:
        pytest.skip("set REALSIGHT_OCR_TEST_IMAGE to run the real OCR integration")
    image_path = Path(raw_path).expanduser().resolve()
    if not image_path.is_file():
        pytest.fail(f"REALSIGHT_OCR_TEST_IMAGE does not exist: {image_path}")

    recognizer = PaddleOcrTextRecognizer()
    try:
        document = recognizer.recognize(
            image_path,
            observation_id="ocr-integration-operator-image",
        )
    finally:
        recognizer.close()

    assert document.observation_id == "ocr-integration-operator-image"
    assert document.provider_metadata["provider"] == "paddleocr"
    assert document.provider_metadata["engine"] == "onnxruntime"
    inference_ms = document.provider_metadata["inference_ms"]
    assert isinstance(inference_ms, (int, float))
    assert inference_ms >= 0.0
