#!/usr/bin/env python3
"""
Tests for the build script's merge logic and source loading.

Run with:  python -m pytest scripts/test_build.py -v
"""

import json
import textwrap
from pathlib import Path
from unittest.mock import patch

import pytest

from build import merge_rules, rule_key, load_sources


class TestMergeRules:
    def test_empty_input(self):
        assert merge_rules([]) == []

    def test_no_duplicates(self):
        rules = [
            {"domain_suffix": ["example.com"]},
            {"domain_suffix": ["example.org"]},
        ]
        result = merge_rules(rules)
        assert len(result) == 2

    def test_exact_duplicates_removed(self):
        rule = {"domain_suffix": ["example.com"]}
        result = merge_rules([rule, rule, rule])
        assert len(result) == 1
        assert result[0] == rule

    def test_order_independent_dedup(self):
        """Two dicts with the same keys but different insertion order are the same rule."""
        r1 = {"domain_suffix": ["a.com"], "domain": ["b.com"]}
        r2 = {"domain": ["b.com"], "domain_suffix": ["a.com"]}
        result = merge_rules([r1, r2])
        assert len(result) == 1

    def test_different_rules_preserved(self):
        r1 = {"domain_suffix": ["a.com"]}
        r2 = {"ip_cidr": ["1.1.1.1/32"]}
        result = merge_rules([r1, r2])
        assert len(result) == 2

    def test_preserves_order_of_first_occurrence(self):
        r1 = {"domain_suffix": ["first.com"]}
        r2 = {"domain_suffix": ["second.com"]}
        r3 = {"domain_suffix": ["first.com"]}
        result = merge_rules([r1, r2, r3])
        assert len(result) == 2
        assert result[0] == r1
        assert result[1] == r2

    def test_complex_rules(self):
        rules = [
            {"domain_suffix": ["a.com", "b.com"], "domain_keyword": ["test"]},
            {"domain_suffix": ["a.com", "b.com"], "domain_keyword": ["test"]},
            {"domain_suffix": ["c.com"]},
        ]
        result = merge_rules(rules)
        assert len(result) == 2

    def test_merged_json_is_valid(self):
        rules = [
            {"domain_suffix": ["example.com"]},
            {"ip_cidr": ["10.0.0.0/8"]},
        ]
        merged = merge_rules(rules)
        output = {"version": 3, "rules": merged}
        serialized = json.dumps(output)
        parsed = json.loads(serialized)
        assert parsed["version"] == 3
        assert len(parsed["rules"]) == 2


class TestRuleKey:
    def test_deterministic(self):
        rule = {"b": 2, "a": 1}
        assert rule_key(rule) == rule_key(rule)

    def test_order_independent(self):
        assert rule_key({"a": 1, "b": 2}) == rule_key({"b": 2, "a": 1})


class TestLoadSources:
    def test_loads_from_yaml(self, tmp_path):
        yaml_content = textwrap.dedent("""\
            sources:
              - name: test1
                url: https://example.com/test1.srs
              - name: test2
                url: https://example.com/test2.srs
        """)
        sources_dir = tmp_path / "sources"
        sources_dir.mkdir()
        (sources_dir / "geosite.yaml").write_text(yaml_content, encoding="utf-8")

        with patch("build.SOURCES_DIR", sources_dir):
            sources = load_sources("geosite")
        assert len(sources) == 2
        assert sources[0].name == "test1"
        assert sources[0].category == "geosite"
        assert sources[1].name == "test2"

    def test_missing_file_returns_empty(self, tmp_path):
        with patch("build.SOURCES_DIR", tmp_path):
            sources = load_sources("nonexistent")
        assert sources == []
