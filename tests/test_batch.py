"""Tests for batch processing."""

from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path
from typing import Any, Dict, List

from domainkit.batch import BatchError, BatchResult, _run_kwargs_for, load_batch, run_batch, validate_item
from domainkit.config import Settings
from domainkit.workflow import Onboarder, Outcome, Plan, RunReport, StepResult


class LoadBatchTestCase(unittest.TestCase):
    """Tests for load_batch."""

    def test_load_valid_batch(self) -> None:
        with tempfile.NamedTemporaryFile(mode="w", suffix=".json", delete=False, encoding="utf-8") as tmp:
            json.dump({"defaults": {}, "domains": [{"domain": "example.com"}]}, tmp)
            tmp.flush()
            path = Path(tmp.name)
        try:
            data = load_batch(path)
            self.assertIsInstance(data, dict)
            self.assertIn("domains", data)
            self.assertEqual(len(data["domains"]), 1)
        finally:
            path.unlink()

    def test_load_empty_domains_raises(self) -> None:
        with tempfile.NamedTemporaryFile(mode="w", suffix=".json", delete=False, encoding="utf-8") as tmp:
            json.dump({"domains": []}, tmp)
            tmp.flush()
            path = Path(tmp.name)
        try:
            with self.assertRaises(BatchError) as cm:
                load_batch(path)
            self.assertIn("no domains", str(cm.exception))
        finally:
            path.unlink()

    def test_load_missing_domains_key_raises(self) -> None:
        with tempfile.NamedTemporaryFile(mode="w", suffix=".json", delete=False, encoding="utf-8") as tmp:
            json.dump({"defaults": {}}, tmp)
            tmp.flush()
            path = Path(tmp.name)
        try:
            with self.assertRaises(BatchError) as cm:
                load_batch(path)
            self.assertIn("no domains", str(cm.exception))
        finally:
            path.unlink()

    def test_load_non_dict_raises(self) -> None:
        with tempfile.NamedTemporaryFile(mode="w", suffix=".json", delete=False, encoding="utf-8") as tmp:
            json.dump([{"domain": "example.com"}], tmp)
            tmp.flush()
            path = Path(tmp.name)
        try:
            with self.assertRaises(BatchError) as cm:
                load_batch(path)
            self.assertIn("must be a JSON object", str(cm.exception))
        finally:
            path.unlink()


class ValidateItemTestCase(unittest.TestCase):
    """Tests for validate_item."""

    def test_valid_with_origin(self) -> None:
        item = {"domain": "example.com", "create_distribution": True, "origin": "site.s3.amazonaws.com"}
        validate_item(item, {})

    def test_valid_with_distribution_id(self) -> None:
        item = {"domain": "example.com", "create_distribution": True, "distribution_id": "E123"}
        validate_item(item, {})

    def test_missing_domain_raises(self) -> None:
        item = {"create_distribution": True, "origin": "site.s3.amazonaws.com"}
        with self.assertRaises(BatchError) as cm:
            validate_item(item, {})
        self.assertIn("missing required field 'domain'", str(cm.exception))

    def test_create_without_origin_or_id_raises(self) -> None:
        item = {"domain": "example.com", "create_distribution": True}
        with self.assertRaises(BatchError) as cm:
            validate_item(item, {})
        self.assertIn("has no 'origin'", str(cm.exception))

    def test_non_dict_raises(self) -> None:
        with self.assertRaises(BatchError) as cm:
            validate_item("example.com", {})  # type: ignore[arg-type]
        self.assertIn("must be a JSON object", str(cm.exception))


class BatchOriginTestCase(unittest.TestCase):
    def test_origin_object_preserves_domain_and_path(self) -> None:
        kwargs = _run_kwargs_for({
            "origin": {"domain": "assets.s3.amazonaws.com", "path": "website/example.com"},
        })
        self.assertEqual(kwargs["origin"].domain, "assets.s3.amazonaws.com")
        self.assertEqual(kwargs["origin"].path, "/website/example.com")

    def test_origin_object_requires_domain(self) -> None:
        with self.assertRaises(BatchError) as cm:
            _run_kwargs_for({"origin": {"path": "/website/example.com"}})
        self.assertIn("origin object requires 'domain'", str(cm.exception))

    def test_legacy_origin_string_remains_supported(self) -> None:
        kwargs = _run_kwargs_for({"origin": "site.s3.amazonaws.com", "origin_path": "/legacy"})
        self.assertEqual(kwargs["origin"].domain, "site.s3.amazonaws.com")
        self.assertEqual(kwargs["origin"].path, "/legacy")


class FakeOnboarder:
    """Minimal onboarder for testing."""

    def __init__(self, settings: Settings, *, should_fail: bool = False) -> None:
        self.settings = settings
        self.should_fail = should_fail
        self.calls: List[tuple[str, Dict[str, Any]]] = []

    def run(self, domain: str, **kwargs: Any) -> RunReport:
        self.calls.append((domain, kwargs))
        plan = Plan.build(domain, self.settings)
        report = RunReport(plan=plan, dry_run=kwargs.get("dry_run", True))
        if self.should_fail:
            report.add(StepResult("aborted", Outcome.FAILED, "simulated failure"))
        return report


class RunBatchTestCase(unittest.TestCase):
    """Tests for run_batch."""

    def test_run_single_domain(self) -> None:
        with tempfile.NamedTemporaryFile(mode="w", suffix=".json", delete=False, encoding="utf-8") as tmp:
            json.dump({
                "defaults": {"gitlab_project": "group/repo"},
                "domains": [{"domain": "example.com"}]
            }, tmp)
            tmp.flush()
            path = Path(tmp.name)

        try:
            def factory(settings: Settings) -> Onboarder:
                return FakeOnboarder(settings)

            result = run_batch(path, onboarder_factory=factory, dry_run=True)
            self.assertTrue(result.ok)
            self.assertEqual(len(result.reports), 1)
            self.assertEqual(len(result.failures), 0)
        finally:
            path.unlink()

    def test_run_multiple_domains(self) -> None:
        with tempfile.NamedTemporaryFile(mode="w", suffix=".json", delete=False, encoding="utf-8") as tmp:
            json.dump({
                "domains": [
                    {"domain": "one.com"},
                    {"domain": "two.com"},
                    {"domain": "three.com"},
                ]
            }, tmp)
            tmp.flush()
            path = Path(tmp.name)

        try:
            calls: List[str] = []

            def factory(settings: Settings) -> Onboarder:
                def run(domain: str, **kwargs: Any) -> RunReport:
                    calls.append(domain)
                    plan = Plan.build(domain, settings)
                    return RunReport(plan=plan, dry_run=True)
                onb = FakeOnboarder(settings)
                onb.run = run  # type: ignore[method-assign]
                return onb  # type: ignore[return-value]

            result = run_batch(path, onboarder_factory=factory, dry_run=False)
            self.assertEqual(calls, ["one.com", "two.com", "three.com"])
            self.assertEqual(len(result.reports), 3)
        finally:
            path.unlink()

    def test_single_failure_does_not_stop_batch(self) -> None:
        with tempfile.NamedTemporaryFile(mode="w", suffix=".json", delete=False, encoding="utf-8") as tmp:
            json.dump({
                "domains": [
                    {"domain": "good.com"},
                    {"domain": "bad.com"},
                    {"domain": "also-good.com"},
                ]
            }, tmp)
            tmp.flush()
            path = Path(tmp.name)

        try:
            def factory(settings: Settings) -> Onboarder:
                def run(domain: str, **kwargs: Any) -> RunReport:
                    plan = Plan.build(domain, settings)
                    report = RunReport(plan=plan, dry_run=True)
                    if domain == "bad.com":
                        report.add(StepResult("aborted", Outcome.FAILED, "bad domain"))
                    return report
                onb = FakeOnboarder(settings)
                onb.run = run  # type: ignore[method-assign]
                return onb  # type: ignore[return-value]

            result = run_batch(path, onboarder_factory=factory)
            self.assertEqual(len(result.reports), 3)
            self.assertEqual(len(result.failures), 1)
            self.assertEqual(result.failures[0][0], "bad.com")
        finally:
            path.unlink()

    def test_validation_failure_continues_batch(self) -> None:
        with tempfile.NamedTemporaryFile(mode="w", suffix=".json", delete=False, encoding="utf-8") as tmp:
            json.dump({
                "domains": [
                    {"domain": "valid.com", "origin": "site.s3.amazonaws.com"},
                    {"domain": "invalid.com", "create_distribution": True},
                    {"domain": "also-valid.com"},
                ]
            }, tmp)
            tmp.flush()
            path = Path(tmp.name)

        try:
            def factory(settings: Settings) -> Onboarder:
                return FakeOnboarder(settings)

            logs: List[str] = []
            result = run_batch(path, onboarder_factory=factory, log=logs.append)

            self.assertEqual(len(result.reports), 2)
            self.assertEqual(len(result.failures), 1)
            self.assertEqual(result.failures[0][0], "invalid.com")
            self.assertIn("has no 'origin'", result.failures[0][1])
        finally:
            path.unlink()

    def test_result_to_dict(self) -> None:
        result = BatchResult()
        settings = Settings(gitlab_project="group/repo")
        plan = Plan.build("example.com", settings)
        result.reports.append(RunReport(plan=plan, dry_run=True))
        result.failures.append(("bad.com", "something broke"))

        d = result.to_dict()
        self.assertFalse(d["ok"])
        self.assertEqual(d["total"], 2)
        self.assertEqual(d["succeeded"], 1)
        self.assertEqual(d["failed"], 1)
        self.assertEqual(len(d["reports"]), 1)
        self.assertEqual(len(d["failures"]), 1)
        self.assertEqual(d["failures"][0]["domain"], "bad.com")
