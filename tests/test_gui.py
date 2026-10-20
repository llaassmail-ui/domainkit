"""GUI helper behavior that does not require a Tk event loop."""

from __future__ import annotations

import unittest

from domainkit.gui import (
    _derive_domain_preview,
    _format_step_summary,
    _load_origin_presets,
    _merge_origin_preset,
    _normalize_origin_preset,
    _origin_preset_label,
    _status_text,
)
from domainkit.workflow import Outcome, StepResult


class OriginPresetTests(unittest.TestCase):
    def test_normalize_origin_preset_keeps_valid_source(self) -> None:
        preset = _normalize_origin_preset(
            {
                "origin": " example-bucket.s3.amazonaws.com ",
                "origin_type": "custom",
                "origin_path": " /website/ruby-rummy.com ",
            }
        )

        self.assertEqual(
            preset,
            {
                "origin": "example-bucket.s3.amazonaws.com",
                "origin_type": "custom",
                "origin_path": "/website/ruby-rummy.com",
            },
        )

    def test_normalize_origin_preset_rejects_blank_origin(self) -> None:
        self.assertEqual(_normalize_origin_preset({"origin": ""}), {})

    def test_load_origin_presets_imports_legacy_single_default(self) -> None:
        presets = _load_origin_presets(
            {
                "origin": "example-bucket.s3.ap-south-1.amazonaws.com",
                "origin_type": "s3-website",
                "origin_path": "/website/example.com",
            }
        )

        self.assertEqual(len(presets), 1)
        self.assertEqual(presets[0]["origin_path"], "/website/example.com")

    def test_merge_origin_preset_moves_duplicate_to_front(self) -> None:
        presets = [
            {"origin": "old.s3.amazonaws.com", "origin_type": "custom", "origin_path": ""},
            {"origin": "example-bucket.s3.amazonaws.com", "origin_type": "s3-website", "origin_path": "/website/a.com"},
        ]

        merged = _merge_origin_preset(
            presets,
            {"origin": "example-bucket.s3.amazonaws.com", "origin_type": "s3-website", "origin_path": "/website/a.com"},
        )

        self.assertEqual(len(merged), 2)
        self.assertEqual(merged[0]["origin"], "example-bucket.s3.amazonaws.com")
        self.assertEqual(merged[1]["origin"], "old.s3.amazonaws.com")

    def test_origin_preset_label_includes_path_and_type(self) -> None:
        label = _origin_preset_label(
            {"origin": "example-bucket.s3.amazonaws.com", "origin_type": "s3-website", "origin_path": "/website/a.com"}
        )

        self.assertEqual(label, "example-bucket.s3.amazonaws.com/website/a.com (s3-website)")


class GuiSummaryTests(unittest.TestCase):
    def test_derive_domain_preview_uses_www_for_apex(self) -> None:
        preview = _derive_domain_preview("Example.com")

        self.assertEqual(preview["domain"], "example.com")
        self.assertEqual(preview["service_domain"], "www.example.com")
        self.assertEqual(preview["branch"], "example.com")
        self.assertEqual(preview["variable_key"], "CLOUDFRONT_EXAMPLE_COM")
        self.assertEqual(preview["certificate"], "www.example.com")

    def test_format_step_summary_translates_common_steps(self) -> None:
        step = StepResult("cloudfront-create", Outcome.PLANNED, "would create distribution")

        self.assertEqual(_format_step_summary(step), ("创建 CloudFront", "将执行", "would create distribution"))

    def test_status_text_with_detail(self) -> None:
        self.assertEqual(_status_text("ok", "cloudfront"), "正常: cloudfront")


if __name__ == "__main__":
    unittest.main()
