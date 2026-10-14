"""The single sanitiser in front of every egress: LLM input, embeddings, web search (ADR-S03).

It removes:
- secrets: private keys, cloud and API keys, tokens, JWTs, bearer credentials,
  ``password=...``-style assignments, credentials in URLs, high-entropy strings;
- PII: e-mail addresses, phone numbers, IP addresses, user names in home-directory paths;
- source code: the source lines Python prints under traceback frames, and user file paths,
  which are cut to their basename. Library frames keep their module path because that is
  what diagnoses dependency problems.

Redactions are replaced by visible placeholders such as ``[REDACTED:API_KEY]`` so the
text stays readable and the model knows something was removed. The sanitiser is
deliberately conservative: a false positive costs a little diagnostic detail, a false
negative leaks a secret (FR-STW-011, 012, NFR-SEC-006).
"""

from __future__ import annotations

import math
import re
from collections import Counter
from dataclasses import dataclass, field

_ANSI = re.compile(r"\x1b\[[0-9;?]*[ -/]*[@-~]")
# NUL and other C0 controls except tab/newline/CR: binary junk in logs, rejected by PostgreSQL.
_CONTROL = re.compile(r"[\x00-\x08\x0b\x0c\x0e-\x1a\x1c-\x1f\x7f]")


@dataclass(frozen=True)
class Rule:
    name: str
    pattern: re.Pattern[str]
    replacement: str  # may reference groups, e.g. r"\1[REDACTED:SECRET]"


def _r(name: str, pattern: str, replacement: str | None = None, flags: int = 0) -> Rule:
    return Rule(name, re.compile(pattern, flags), replacement or f"[REDACTED:{name}]")


# Order matters: multi-line and structured secrets first, generic fallbacks last.
SECRET_RULES: tuple[Rule, ...] = (
    _r(
        "PRIVATE_KEY",
        r"-----BEGIN[A-Z ]*PRIVATE KEY-----.*?(?:-----END[A-Z ]*PRIVATE KEY-----|\Z)",
        flags=re.S,
    ),
    _r("ANTHROPIC_KEY", r"\bsk-ant-[A-Za-z0-9_\-]{16,}", "[REDACTED:API_KEY]"),
    _r("OPENAI_KEY", r"\bsk-(?:proj-)?[A-Za-z0-9_\-]{20,}", "[REDACTED:API_KEY]"),
    _r(
        "GITHUB_TOKEN",
        r"\b(?:gh[pousr]_[A-Za-z0-9]{30,}|github_pat_[A-Za-z0-9_]{30,})",
        "[REDACTED:TOKEN]",
    ),
    _r("HF_TOKEN", r"\bhf_[A-Za-z0-9]{30,}", "[REDACTED:TOKEN]"),
    _r("SLACK_TOKEN", r"\bxox[abposr]-[A-Za-z0-9-]{10,}", "[REDACTED:TOKEN]"),
    _r("AWS_ACCESS_KEY", r"\b(?:AKIA|ASIA)[0-9A-Z]{16}\b", "[REDACTED:API_KEY]"),
    _r("GOOGLE_API_KEY", r"\bAIza[0-9A-Za-z_\-]{35}\b", "[REDACTED:API_KEY]"),
    _r(
        "JWT",
        r"\beyJ[A-Za-z0-9_\-]{8,}\.eyJ[A-Za-z0-9_\-]{8,}\.[A-Za-z0-9_\-]{8,}",
        "[REDACTED:TOKEN]",
    ),
    _r("BEARER", r"(?i)\b(bearer|token)\s+[A-Za-z0-9\-._~+/]{16,}=*", r"\1 [REDACTED:TOKEN]"),
    # scheme://user:password@host -> keep scheme and host
    _r(
        "URL_CREDENTIALS",
        r"\b([a-z][a-z0-9+.\-]*://)[^\s/:@]+:[^\s/@]+@",
        r"\1[REDACTED:CREDENTIALS]@",
        re.I,
    ),
    # key=value / key: value / "key": "value" for secret-looking keys; keep the key name
    _r(
        "ASSIGNMENT",
        r"""(?ix)
        (\b[\w.\-]*(?:password|passwd|pwd|secret
              |token(?!s\b|s_|iz|_?count|_?len|_?id\b)  # not max_tokens, tokenizer, token_id
              |api[_\-]?key|apikey|access[_\-]?key
              |private[_\-]?key|credential|auth[_\-]?key|client[_\-]?secret|session[_\-]?id)
           [\w.\-]*["']?\s*[:=]\s*)
        (?!\[REDACTED)(?:"[^"\n]*"|'[^'\n]*'|[^\s,;}\]]+)""",
        r"\1[REDACTED:SECRET]",
    ),
    _r(
        "QUERY_SECRET",
        r"(?i)([?&](?:token|key|sig|signature|secret|password|auth)=)[^&\s]+",
        r"\1[REDACTED:SECRET]",
    ),
)

PII_RULES: tuple[Rule, ...] = (
    _r("EMAIL", r"\b[A-Za-z0-9._%+\-]+@[A-Za-z0-9.\-]+\.[A-Za-z]{2,}\b"),
    _r("PHONE", r"(?<![\w.])\+\d{1,3}[\s\-]?\(?\d{2,5}\)?[\s\-]?\d{3,5}[\s\-]?\d{3,5}(?![\w.])"),
    _r("PHONE", r"(?i)\b(?:phone|mobile|tel|contact)\b\W{0,3}\d[\d\s\-]{8,}\d"),
    _r("IP_ADDRESS", r"\b(?:(?:25[0-5]|2[0-4]\d|1?\d?\d)\.){3}(?:25[0-5]|2[0-4]\d|1?\d?\d)\b"),
    # user names in home directories: keep the structure, drop the name
    _r("HOME_PATH", r"(?i)(/home/|/Users/|[A-Z]:\\Users\\)[^/\\\s\"']+", r"\1[USER]"),
)

_TB_FRAME = re.compile(r'^(?P<indent>\s*)File "(?P<path>[^"]+)", line (?P<line>\d+)(?P<rest>.*)$')
_CARET_LINE = re.compile(r"^\s*[\^~\-]+\s*$")
_LIB_MARKERS = ("site-packages", "dist-packages", "/lib/python", "\\lib\\python", "<frozen")
_HIGH_ENTROPY = re.compile(r"(?<![\w/\-.])[A-Za-z0-9+/_\-]{32,}={0,2}(?![\w/\-.])")


def _is_library_path(path: str) -> bool:
    return any(marker in path for marker in _LIB_MARKERS)


def library_module(path: str) -> str | None:
    """``.../site-packages/torch/nn/modules/module.py`` -> ``torch.nn.modules.module``."""
    norm = path.replace("\\", "/")
    for marker in ("site-packages/", "dist-packages/"):
        if marker in norm:
            tail = norm.split(marker, 1)[1]
            return tail.removesuffix(".py").replace("/", ".")
    return None


def _shorten_path(path: str) -> str:
    """Library frames keep their module path; user files keep only the basename."""
    if module := library_module(path):
        return module.replace(".", "/") + ".py"
    if _is_library_path(path):
        return path.replace("\\", "/").rsplit("/", 1)[-1]
    return path.replace("\\", "/").rsplit("/", 1)[-1]


def _shannon(s: str) -> float:
    counts = Counter(s)
    return -sum(c / len(s) * math.log2(c / len(s)) for c in counts.values())


def _looks_secret(token: str) -> bool:
    """Mixed-class, high-entropy strings. Pure hex (git SHAs, digests) is left alone."""
    if re.fullmatch(r"[0-9a-fA-F]+", token):
        return False
    classes = sum(bool(re.search(p, token)) for p in (r"[a-z]", r"[A-Z]", r"[0-9]"))
    return classes >= 3 and _shannon(token) >= 4.0


@dataclass(frozen=True)
class Sanitised:
    text: str
    redactions: Counter[str] = field(default_factory=Counter)

    @property
    def redacted(self) -> int:
        return sum(self.redactions.values())


class Sanitiser:
    def __init__(self, max_line_chars: int = 2_000) -> None:
        self.max_line_chars = max_line_chars

    def sanitise(self, raw: str) -> Sanitised:
        counts: Counter[str] = Counter()
        text = _CONTROL.sub("", _ANSI.sub("", raw)).replace("\r\n", "\n")
        # Progress bars rewrite the line with \r; keep only the last state.
        text = "\n".join(line.rsplit("\r", 1)[-1] for line in text.split("\n"))
        text = self._strip_source(text, counts)
        for rule in SECRET_RULES + PII_RULES:
            text, n = rule.pattern.subn(rule.replacement, text)
            if n:
                counts[rule.name] += n

        def _entropy(match: re.Match[str]) -> str:
            if _looks_secret(match.group(0)):
                counts["HIGH_ENTROPY"] += 1
                return "[REDACTED:HIGH_ENTROPY]"
            return match.group(0)

        text = _HIGH_ENTROPY.sub(_entropy, text)
        lines = [
            line if len(line) <= self.max_line_chars else line[: self.max_line_chars] + " [...]"
            for line in text.split("\n")
        ]
        return Sanitised("\n".join(lines), counts)

    @staticmethod
    def _strip_source(text: str, counts: Counter[str]) -> str:
        """Drop the source lines Python prints under each traceback frame (they are the
        user's code) and the caret markers under them; shorten frame paths."""
        out: list[str] = []
        lines = text.split("\n")
        i = 0
        while i < len(lines):
            frame = _TB_FRAME.match(lines[i])
            if frame:
                path = frame.group("path")
                out.append(
                    f'{frame.group("indent")}File "{_shorten_path(path)}", '
                    f"line {frame.group('line')}{frame.group('rest')}"
                )
                i += 1
                # The next indented, non-frame line(s) are source and markers.
                while i < len(lines):
                    nxt = lines[i]
                    if _TB_FRAME.match(nxt) or not nxt.startswith(" ") or not nxt.strip():
                        break
                    if not _is_library_path(path):
                        counts["SOURCE_CODE"] += 0 if _CARET_LINE.match(nxt) else 1
                    i += 1
                continue
            out.append(lines[i])
            i += 1
        return "\n".join(out)


# ---------------------------------------------------------------- facts for search and S2

_EXC_LINE = re.compile(
    r"^(?P<cls>(?:[A-Za-z_][\w]*\.)*[A-Z]\w*(?:Error|Exception|Exit|Interrupt|Fault|Warning))"
    r"(?::|$)"
)
_PKG_PIN = re.compile(r"\b([A-Za-z][A-Za-z0-9_.\-]*)\s*(?:==|@)\s*v?(\d+(?:\.\d+){1,3}[\w.+\-]*)")


@dataclass(frozen=True)
class ErrorFacts:
    """The only things allowed into a web-search query (ADR-S06)."""

    exception_class: str | None
    top_library_module: str | None
    packages: tuple[tuple[str, str], ...]

    @property
    def failure_signature(self) -> str | None:
        """``torch.cuda.OutOfMemoryError@torch.cuda.memory`` (ADR-S05)."""
        if self.exception_class is None:
            return None
        if self.top_library_module is None:
            return self.exception_class
        return f"{self.exception_class}@{'.'.join(self.top_library_module.split('.')[:3])}"


def extract_error_facts(raw: str, env_pins: tuple[str, ...] = ()) -> ErrorFacts:
    """Pull the final exception class, the innermost library frame and pinned packages.

    Works on raw or sanitised text; nothing it returns can carry user content: the class
    must look like a Python exception name, the module comes from site-packages paths
    only, and packages must match ``name==version``.
    """
    exception_class: str | None = None
    top_module: str | None = None
    for line in raw.splitlines():
        stripped = line.strip()
        if frame := _TB_FRAME.match(line):
            module = library_module(frame.group("path"))
            if module:
                top_module = module
        elif match := _EXC_LINE.match(stripped):
            exception_class = match.group("cls")
    packages: dict[str, str] = {}
    for pin in env_pins:
        if m := _PKG_PIN.search(pin):
            packages.setdefault(m.group(1).lower(), m.group(2))
    return ErrorFacts(exception_class, top_module, tuple(sorted(packages.items())))


def excerpt(text: str, max_chars: int) -> str:
    """Keep what diagnoses failures: the last traceback block and the tail of the log."""
    if len(text) <= max_chars:
        return text
    idx = text.rfind("Traceback (most recent call last)")
    if idx != -1 and len(text) - idx <= max_chars:
        head_budget = max_chars - (len(text) - idx)
        head = text[max(0, idx - head_budget) : idx]
        return ("[...]\n" if idx - head_budget > 0 else "") + head + text[idx:]
    return "[...]\n" + text[-max_chars:]
