"""从真实设备 JSONL 生成可复算 JSON 和 Markdown 指标。"""

from __future__ import annotations

import argparse
import json
from pathlib import Path

from realsight.evaluation import evaluate_samples, load_jsonl, render_markdown


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("input", type=Path, help="真实设备样本 JSONL")
    parser.add_argument("--json-output", type=Path)
    parser.add_argument("--markdown-output", type=Path)
    args = parser.parse_args()

    report = evaluate_samples(load_jsonl(args.input))
    json_text = json.dumps(report.model_dump(mode="json"), ensure_ascii=False, indent=2)
    markdown = render_markdown(report)
    if args.json_output is not None:
        args.json_output.parent.mkdir(parents=True, exist_ok=True)
        args.json_output.write_text(f"{json_text}\n", encoding="utf-8")
    if args.markdown_output is not None:
        args.markdown_output.parent.mkdir(parents=True, exist_ok=True)
        args.markdown_output.write_text(markdown, encoding="utf-8")
    if args.json_output is None and args.markdown_output is None:
        print(json_text)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
