"""Region/account resolution for the evaluation tooling.

`us-east-1` was hardcoded or defaulted throughout this package while every deployed
environment is us-west-2. That fails SILENTLY: a CloudWatch log group simply does not
exist in the wrong region, so span collection returns nothing and a run reports "no
low-scoring sessions" rather than an error. Replacing one literal with another would
keep the same class of bug, so coordinates are read from the CDK config the deployment
actually uses.
"""
import os
import re
from pathlib import Path

import pytest

from .deployment import (ENV_CONFIG_PATH, available_envs, deployment_account,
                         deployment_config, deployment_region)

PKG = Path(__file__).resolve().parent


class TestParsing:
    def test_reads_every_environment_from_the_cdk_config(self):
        envs = available_envs()
        for expected in ("dev", "staging", "sandbox", "prod"):
            assert expected in envs

    def test_regions_and_accounts_match_the_cdk_config(self):
        text = ENV_CONFIG_PATH.read_text()
        for env in available_envs():
            cfg = deployment_config(env)
            # the account must appear beside that env's declaration
            block = text[text.index(f"  {env}: {{"):][:2000]
            assert f'region: "{cfg["region"]}"' in block
            if cfg["account"]:
                assert f'account: "{cfg["account"]}"' in block

    def test_staging_resolves_to_its_configured_account(self):
        cfg = deployment_config("staging")
        assert re.fullmatch(r"\d{12}", cfg["account"]), cfg["account"]
        assert cfg["region"] == "us-west-2"

    def test_unknown_env_is_an_error_not_a_guess(self):
        with pytest.raises(KeyError):
            deployment_config("nope")


class TestPrecedence:
    def test_explicit_argument_wins(self):
        assert deployment_region("ap-south-1") == "ap-south-1"

    def test_eval_region_overrides_the_config(self, monkeypatch):
        monkeypatch.setenv("EVAL_REGION", "eu-west-1")
        assert deployment_region() == "eu-west-1"

    def test_generic_aws_region_is_ignored(self, monkeypatch):
        """The one that caused the bug: an unrelated shell default must not win."""
        monkeypatch.setenv("AWS_REGION", "us-east-1")
        monkeypatch.delenv("EVAL_REGION", raising=False)
        assert deployment_region() == "us-west-2"

    def test_deploy_env_selects_the_environment(self, monkeypatch):
        monkeypatch.delenv("EVAL_REGION", raising=False)
        monkeypatch.setenv("DEPLOY_ENV", "prod")
        assert deployment_account() == deployment_config("prod")["account"]
        assert deployment_account() != deployment_config("staging")["account"]


class TestNoLiteralsRemain:
    """Guards the fix: a literal anywhere here reintroduces the silent failure."""

    def test_no_region_literal_in_the_package(self):
        offenders = {}
        for f in sorted(PKG.glob("*.py")):
            if f.name in ("deployment.py",) or f.name.startswith("test_"):
                continue
            hits = re.findall(r'["\']us-(?:east|west)-\d["\']', f.read_text())
            if hits:
                offenders[f.name] = hits
        assert not offenders, f"hardcoded regions: {offenders}"

    def test_no_generic_aws_region_default(self):
        offenders = []
        for f in sorted(PKG.glob("*.py")):
            if f.name.startswith("test_"):
                continue
            if 'environ.get("AWS_REGION"' in f.read_text():
                offenders.append(f.name)
        assert not offenders, f"AWS_REGION consulted in: {offenders}"
