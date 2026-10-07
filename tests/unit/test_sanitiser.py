"""ADR-S03 sanitiser. A23: secrets, PII and source code never leave the server."""

import pytest

from prayog_steward.rag.sanitiser import Sanitiser, excerpt, extract_error_facts, library_module

S = Sanitiser()

# A realistic failing-run log with planted secrets, PII and user code (A23).
A23_LOG = """\
Epoch 3/10: 100%|##########| 500/500 [01:02<00:00]
wandb: Logging into https://alice:hunter2@wandb.example.org with WANDB_API_KEY=9f2a7c1e4b8d6f3a5e7c9b1d2f4a6c8e0b3d5f7a
export ANTHROPIC_API_KEY=sk-ant-api03-AbCdEfGhIjKlMnOpQrStUvWxYz0123456789
HF_TOKEN=hf_AbCdEfGhIjKlMnOpQrStUvWxYz01234567
aws key AKIAIOSFODNN7EXAMPLE  Authorization: Bearer eyJhbGciOiJIUzI1NiJ9.eyJzdWIiOiIxMjM0NTY3ODkwIn0.dQw4w9WgXcQkZp
db: postgresql://trainer:s3cr3tP4ss@10.0.3.17:5432/expts  password = "correct horse"
contact alice.sharma@iitk.ac.in or +91 98765 43210 for access
Traceback (most recent call last):
  File "/home/alice/thesis/secret_model/train.py", line 42, in step
    loss = model(batch, secret_sauce=compute_mix(alpha=0.37))
           ^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^
  File "/opt/venv/lib/python3.11/site-packages/torch/nn/modules/module.py", line 1511, in _wrapped_call_impl
    return self._call_impl(*args, **kwargs)
torch.cuda.OutOfMemoryError: CUDA out of memory. Tried to allocate 2.00 GiB
"""

PLANTED = [
    "hunter2",
    "9f2a7c1e4b8d6f3a5e7c9b1d2f4a6c8e0b3d5f7a",
    "sk-ant-api03",
    "hf_AbCdEf",
    "AKIAIOSFODNN7EXAMPLE",
    "eyJhbGciOiJIUzI1NiJ9",
    "s3cr3tP4ss",
    "correct horse",
    "alice.sharma@iitk.ac.in",
    "98765 43210",
    "10.0.3.17",
    "/home/alice",
    "secret_model",
    "secret_sauce",
    "compute_mix",
]


@pytest.mark.parametrize("secret", PLANTED)
def test_a23_planted_secret_pii_and_code_are_removed(secret: str) -> None:
    assert secret not in S.sanitise(A23_LOG).text


def test_a23_diagnostic_content_survives() -> None:
    out = S.sanitise(A23_LOG).text
    assert "torch.cuda.OutOfMemoryError: CUDA out of memory. Tried to allocate 2.00 GiB" in out
    assert 'File "train.py", line 42, in step' in out
    assert 'File "torch/nn/modules/module.py", line 1511' in out
    assert "postgresql://[REDACTED:CREDENTIALS]@" in out
    assert "Epoch 3/10" in out


def test_already_redacted_values_are_not_redacted_twice() -> None:
    out = S.sanitise("ANTHROPIC_API_KEY=sk-ant-api03-AbCdEfGhIjKlMnOpQrSt0123").text
    assert out == "ANTHROPIC_API_KEY=[REDACTED:API_KEY]"


def test_redactions_are_counted_by_category() -> None:
    result = S.sanitise(A23_LOG)
    assert result.redactions["SOURCE_CODE"] == 1
    assert result.redactions["EMAIL"] == 1
    assert result.redacted >= 10


@pytest.mark.parametrize(
    "text",
    [
        "max_tokens=512 tokenizer=bert-base-uncased token_id=3",
        "commit 3f9a1c2b7d4e5f60718293a4b5c6d7e8f9012345",  # git SHA: pure hex
        "lr=3e-4 batch_size=256 step 12000 loss 0.4183",
        "image sha256:9b2d5c1f0a3e4d7c8b6a5f4e3d2c1b0a9f8e7d6c5b4a3f2e1d0c9b8a7f6e5d4c",
    ],
)
def test_harmless_training_text_is_kept(text: str) -> None:
    assert S.sanitise(text).text == text


def test_private_key_blocks_are_removed_whole() -> None:
    key = "-----BEGIN RSA PRIVATE KEY-----\nMIIEow\nIBAAKC\n-----END RSA PRIVATE KEY-----"
    out = S.sanitise(f"before\n{key}\nafter").text
    assert out == "before\n[REDACTED:PRIVATE_KEY]\nafter"


def test_ansi_and_progress_bar_rewrites_are_collapsed() -> None:
    assert S.sanitise("\x1b[32mok\x1b[0m\r50%\r100%").text == "100%"


def test_very_long_lines_are_truncated() -> None:
    out = Sanitiser(max_line_chars=10).sanitise("x" * 50).text
    assert out == "x" * 10 + " [...]"


def test_error_facts_feed_search_and_failure_signature() -> None:
    facts = extract_error_facts(A23_LOG, env_pins=("torch==2.3.1", "numpy==1.26.4", "junk"))
    assert facts.exception_class == "torch.cuda.OutOfMemoryError"
    assert facts.top_library_module == "torch.nn.modules.module"
    assert facts.packages == (("numpy", "1.26.4"), ("torch", "2.3.1"))
    assert facts.failure_signature == "torch.cuda.OutOfMemoryError@torch.nn.modules"


def test_error_facts_never_carry_user_text() -> None:
    facts = extract_error_facts("my secret plan failed\nnot an exception line")
    assert facts == extract_error_facts("")
    assert facts.failure_signature is None


def test_library_module_only_for_installed_packages() -> None:
    assert library_module(r"C:\venv\Lib\site-packages\numpy\core\_methods.py") == (
        "numpy.core._methods"
    )
    assert library_module("/home/alice/proj/train.py") is None


def test_excerpt_keeps_the_last_traceback() -> None:
    log = "noise\n" * 5000 + "Traceback (most recent call last)\n  boom\nValueError: x"
    out = excerpt(log, 200)
    assert out.endswith("ValueError: x")
    assert "Traceback (most recent call last)" in out
    assert len(out) <= 210
