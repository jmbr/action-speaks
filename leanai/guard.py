"""Static guard: refuse dangerous source before Lean ever sees it.

The axiom audit in `LeanAI.Audit` is the authoritative soundness check, and it defeats
almost everything — but it has two blind spots that only a source-level check can cover:

* `set_option debug.skipKernelTC true` disables the kernel type-check that gives
  `#print axioms` its meaning. A proof admitted this way can have a clean axiom footprint
  and still be garbage.
* Redefining the audit commands themselves (or `LeanAI.Audit.trustedAxioms`) would let the
  submission forge its own verdict, since the audit output is just an info message.

Everything here is a *refusal*, never a rewrite: we do not attempt to sanitise submitted
code, because silently editing a proof is a good way to produce a verdict about a program
nobody wrote.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field


@dataclass(frozen=True)
class Rule:
    name: str
    pattern: re.Pattern[str]
    why: str


def _rx(p: str) -> re.Pattern[str]:
    return re.compile(p, re.MULTILINE)


# Ordered roughly by severity.
RULES: tuple[Rule, ...] = (
    Rule(
        "skip_kernel_typecheck",
        _rx(r"\bdebug\.skipKernelTC\b"),
        "disables the kernel type-checker, which invalidates every downstream guarantee",
    ),
    Rule(
        "unchecked_sorry",
        _rx(r"\bsorryAx\b"),
        "references the `sorry` axiom directly",
    ),
    Rule(
        "axiom_declaration",
        _rx(r"^\s*(?:@\[[^\]]*\]\s*)*(?:private\s+|protected\s+|noncomputable\s+)*axiom\b"),
        "declares a new axiom; a proof may only rest on Mathlib's existing foundations",
    ),
    Rule(
        "native_decide",
        _rx(r"\bnative_decide\b"),
        "trusts the compiler rather than the kernel (adds an `ofReduceBool`-style axiom)",
    ),
    Rule(
        "implemented_by",
        _rx(r"@\[\s*(?:[^\]]*,\s*)?implemented_by\b"),
        "replaces a definition's runtime behaviour with unverified code",
    ),
    Rule(
        "extern",
        _rx(r"@\[\s*(?:[^\]]*,\s*)?extern\b"),
        "binds to external unverified code",
    ),
    Rule(
        "unsafe",
        _rx(r"^\s*(?:@\[[^\]]*\]\s*)*unsafe\b"),
        "`unsafe` declarations bypass termination and soundness checks",
    ),
    Rule(
        "partial_def",
        _rx(r"^\s*(?:@\[[^\]]*\]\s*)*partial\s+def\b"),
        "`partial` definitions are opaque to the kernel",
    ),
    Rule(
        "unsafe_recursion",
        _rx(r"\b(?:unsafeCast|lcCast|lcProof|Classical\.lcProof)\b"),
        "unsafe casts can produce a proof of anything",
    ),
    Rule(
        "early_exit",
        _rx(r"^\s*#exit\b"),
        "`#exit` stops elaboration early, hiding everything after it",
    ),
    Rule(
        "unbounded_heartbeats",
        _rx(r"\bmaxHeartbeats\s+0\b"),
        "removes the elaboration time limit, allowing a submission to hang the verifier",
    ),
    Rule(
        "audit_tampering",
        _rx(r"\b(?:trustedAxioms|elabAuditAxioms|elabAuditVacuity|elabAuditTriviality|"
            r"elabAuditShape|probeTactics|LEANAI_AUDIT)\b"),
        "touches the audit machinery, which would let the submission forge its own verdict",
    ),
    Rule(
        "namespace_hijack",
        _rx(r"^\s*(?:namespace|open)\s+LeanAI\b"),
        "reopens the audit namespace",
    ),
    Rule(
        "logging_forgery",
        _rx(r"\b(?:logInfo|IO\.println|dbg_trace)\b"),
        "can emit text that mimics an audit record",
    ),
    Rule(
        "elab_escape",
        _rx(r"\b(?:elab|macro|macro_rules|syntax|notation|set_option\s+quotPrecheck)\b"),
        "custom syntax/elaboration can subvert later checks; submit plain Lean instead",
    ),
    Rule(
        "meta_execution",
        _rx(r"^\s*#eval\b"),
        "`#eval` runs arbitrary code at elaboration time",
    ),
    Rule(
        "io_access",
        _rx(r"\b(?:IO\.FS|IO\.Process|System\.FilePath|unsafeIO|unsafeBaseIO)\b"),
        "touches the filesystem or spawns processes",
    ),
    Rule(
        "import_in_body",
        _rx(r"^\s*import\b"),
        "imports are fixed by the verifier's prelude and may not be changed per-submission",
    ),
    Rule(
        "linter_disable",
        _rx(r"\bset_option\s+(?:linter\.all|warningAsError)\b"),
        "suppresses diagnostics the verifier relies on",
    ),
)


@dataclass
class GuardViolation:
    rule: str
    why: str
    line_no: int
    line: str

    def render(self) -> str:
        return f"line {self.line_no}: {self.rule} - {self.why}\n    {self.line.strip()}"


@dataclass
class GuardResult:
    ok: bool
    violations: list[GuardViolation] = field(default_factory=list)

    def render(self) -> str:
        return "\n".join(v.render() for v in self.violations)


_LINE_COMMENT = re.compile(r"--.*$", re.MULTILINE)
_BLOCK_COMMENT = re.compile(r"/-.*?-/", re.DOTALL)
_STRING = re.compile(r'"(?:[^"\\]|\\.)*"')


def strip_noise(src: str) -> str:
    """Blank out comments and string literals, preserving line structure.

    Scanning raw source would flag the word "axiom" inside a docstring; scanning stripped
    source keeps line numbers aligned with the original for reporting.
    """

    def blank(m: re.Match[str]) -> str:
        return re.sub(r"[^\n]", " ", m.group(0))

    src = _BLOCK_COMMENT.sub(blank, src)
    src = _LINE_COMMENT.sub(blank, src)
    src = _STRING.sub(blank, src)
    return src


def check(src: str, extra_rules: tuple[Rule, ...] = ()) -> GuardResult:
    """Screen `src`, returning every violation rather than stopping at the first."""
    scrubbed = strip_noise(src)
    lines = scrubbed.splitlines()
    original = src.splitlines()
    violations: list[GuardViolation] = []
    for rule in RULES + extra_rules:
        for m in rule.pattern.finditer(scrubbed):
            line_no = scrubbed.count("\n", 0, m.start()) + 1
            text = original[line_no - 1] if line_no <= len(original) else ""
            violations.append(GuardViolation(rule.name, rule.why, line_no, text))
    violations.sort(key=lambda v: (v.line_no, v.rule))
    # `lines` participates only in bounds checking above; keep the reference explicit.
    del lines
    return GuardResult(ok=not violations, violations=violations)
