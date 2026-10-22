"""DAG P1: detect a malformed SFN execution ARN (execution never started).

A workflow submitted with an empty/invalid required input can return the ARN prefix
with no execution name (arn:...:execution: or arn:...:execution:<sm>). That is a failed
submission, not a trackable job — registering it created a phantom that hung forever
(the recognize_faces_in_proxy incident).
"""
from __future__ import annotations

import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))

from jobs.interceptor import is_malformed_sfn_arn  # noqa: E402

ACCT = "100000000002"


def test_execution_prefix_with_no_name_is_malformed():
    assert is_malformed_sfn_arn(f"arn:aws:states:us-west-2:{ACCT}:execution:") is True


def test_execution_with_state_machine_but_no_name_is_malformed():
    assert is_malformed_sfn_arn(
        f"arn:aws:states:us-west-2:{ACCT}:execution:m2cv6-0e0000000002-media-processing"
    ) is True


def test_full_execution_arn_is_valid():
    assert is_malformed_sfn_arn(
        f"arn:aws:states:us-west-2:{ACCT}:execution:m2cv6-0e0000000002-media-processing:cc5b9cb7-e5c2-5018-96a7"
    ) is False


def test_non_sfn_ids_are_never_flagged():
    assert is_malformed_sfn_arn("req_1234abcd") is False          # fal request_id
    assert is_malformed_sfn_arn("cc5b9cb7-e5c2-5018-96a7") is False  # bare workflow id
    assert is_malformed_sfn_arn("arn:aws:s3:::bucket/key") is False  # different service arn
    assert is_malformed_sfn_arn(None) is False
    assert is_malformed_sfn_arn("") is False
    assert is_malformed_sfn_arn(12345) is False
