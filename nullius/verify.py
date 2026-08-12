"""The verification pipeline.

A submission is accepted only if it survives, in order:

1. **Static guard** - no kernel-bypass options, no new axioms, no tampering with the audit
   machinery. Cheap, and runs before Lean sees the code.
2. **Elaboration** - it compiles, with no errors and no `sorry` warnings.
3. **Named target** - the declaration the submission claims to prove actually exists.
4. **Axiom audit** - its kernel-level dependencies lie within
   `{propext, Classical.choice, Quot.sound}`.
5. **Vacuity audit** - its hypotheses are not contradictory. A vacuously true theorem is
   valid Lean and worthless evidence.
6. **Triviality audit** - its conclusion is not already provable with the hypotheses
   deleted (a warning by default rather than a hard failure, since some genuine claims
   really are unconditional).

The output is a `Verdict`: a machine-readable record plus a human-readable explanation,
carrying the toolchain and Mathlib revision so a third party can reproduce it.
"""

from __future__ import annotations

import hashlib
import json
import re
import secrets
import time
from dataclasses import asdict, dataclass, field
from typing import Any

from . import guard
from .config import Config
from .repl import ReplError, ReplResponse, ReplTimeout, Session

TRUSTED_AXIOMS = ("propext", "Classical.choice", "Quot.sound")

# Prefix for the alias, seed and canary declarations the auditor injects. The kernel-replay
# command is told to ignore it so the auditor does not re-check its own scaffolding.
_HELPER_PREFIX = "nulliusRef"

_DECL_RE = re.compile(
    r"^\s*(?:@\[[^\]]*\]\s*)*(?:private\s+|protected\s+|noncomputable\s+)*"
    r"(theorem|lemma|def|abbrev|instance|example)\s+"
    r"([A-Za-z_\u03b1-\u03c9][^\s:({\[]*)",
    re.MULTILINE,
)


class Status:
    VERIFIED = "verified"
    REJECTED = "rejected"
    ERROR = "error"


@dataclass
class Check:
    name: str
    passed: bool
    detail: str = ""
    data: dict[str, Any] = field(default_factory=dict)
    fatal: bool = True


@dataclass
class Verdict:
    status: str
    target: str | None
    claim: str | None
    checks: list[Check] = field(default_factory=list)
    axioms: list[str] = field(default_factory=list)
    statement: str | None = None
    statement_explicit: str | None = None
    warnings: list[str] = field(default_factory=list)
    lean_messages: str = ""
    elapsed: float = 0.0
    provenance: dict[str, str] = field(default_factory=dict)
    source_sha256: str = ""

    @property
    def verified(self) -> bool:
        return self.status == Status.VERIFIED

    @property
    def failures(self) -> list[Check]:
        return [c for c in self.checks if not c.passed and c.fatal]

    def to_dict(self) -> dict[str, Any]:
        d = asdict(self)
        d["verified"] = self.verified
        return d

    def to_json(self, indent: int | None = 2) -> str:
        return json.dumps(self.to_dict(), indent=indent, ensure_ascii=False)

    def render(self) -> str:
        """A summary written for the agent that submitted the proof."""
        head = {
            Status.VERIFIED: "VERIFIED",
            Status.REJECTED: "REJECTED",
            Status.ERROR: "ERROR",
        }[self.status]
        lines = [f"{head}: {self.target or '(no target)'}"]
        if self.claim:
            lines.append(f"claim: {self.claim}")
        for c in self.checks:
            mark = "pass" if c.passed else ("FAIL" if c.fatal else "warn")
            lines.append(f"  [{mark}] {c.name}" + (f" - {c.detail}" if c.detail else ""))
        if self.axioms:
            lines.append(f"  axioms: {', '.join(self.axioms) or 'none'}")
        if self.statement:
            lines.append(f"  elaborated statement:\n    {self.statement}")
        if self.lean_messages:
            lines.append("  lean output:\n" + _indent(self.lean_messages, 4))
        if self.provenance:
            lines.append(
                f"  toolchain: {self.provenance.get('toolchain')} "
                f"mathlib: {self.provenance.get('mathlib_rev', '')[:12]}"
            )
        lines.append(f"  elapsed: {self.elapsed:.2f}s")
        return "\n".join(lines)

    def feedback(self) -> str:
        """Actionable text to hand back to a model in a repair loop.

        `render` describes what happened; this says what to do about it. Keep it short:
        it is prepended to a retry prompt, where verbosity costs tokens and attention.
        """
        if self.verified:
            return (
                "VERIFIED. Cite this as machine-checked, quoting the elaborated statement "
                f"exactly:\n  {self.statement}\n"
                "Check that this statement really is the claim you meant to make."
            )

        advice: list[str] = []
        for c in self.checks:
            if c.passed or not c.fatal:
                continue
            if c.name == "static_guard":
                advice.append(
                    "Your submission uses constructs that are not allowed "
                    f"({c.data.get('violations')}). Remove them and prove the claim "
                    "honestly; `sorry`, new `axiom`s and `native_decide` are never accepted."
                )
            elif c.name == "elaboration":
                advice.append(
                    "The proof does not compile. Fix the errors below. If a tactic failed, "
                    "try `lean_find_proof` on the failing goal instead of guessing a lemma "
                    f"name.\n{_indent(self.lean_messages, 2)}"
                )
            elif c.name == "no_sorry":
                advice.append(
                    "The proof is incomplete (`sorry`). Either finish it or state plainly "
                    "that you could not prove the claim."
                )
            elif c.name == "target_declared":
                advice.append(
                    f"No such declaration to audit: {c.detail}. Name the theorem you want "
                    "checked, and put it last."
                )
            elif c.name == "audit_integrity":
                advice.append(
                    "The submission interfered with the verifier. Submit a plain proof."
                )
            elif c.name == "kernel_replay":
                advice.append(
                    "Lean's kernel rejected declarations that the elaborator accepted, which "
                    "means the environment was manipulated or a kernel check was skipped. "
                    "Submit a plain proof that type-checks normally."
                )
            elif c.name == "trusted_axioms":
                advice.append(
                    f"The proof depends on untrusted axioms ({c.detail}). Only "
                    "propext, Classical.choice and Quot.sound are permitted."
                )
            elif c.name == "not_vacuous":
                advice.append(
                    "Your hypotheses CONTRADICT each other, so the theorem is vacuously "
                    "true and proves nothing. Do not try to fix the proof - fix the "
                    "STATEMENT so that its hypotheses can actually be satisfied."
                )
            elif c.name == "hypotheses_used":
                advice.append(
                    "The conclusion holds without your hypotheses, so the statement is "
                    "weaker than the claim suggests. Strengthen the conclusion, or drop the "
                    "hypotheses and claim the stronger unconditional result."
                )
            elif c.name == "statement_sorry_free":
                advice.append("The statement itself contains `sorry`. Write it out fully.")
            elif c.name == "is_theorem":
                advice.append(f"{c.detail}. Submit a theorem, not a definition.")
            else:
                advice.append(f"{c.name}: {c.detail}")

        if self.status == Status.ERROR:
            advice.append("The verifier itself failed; retry, or simplify the submission.")
        return "NOT VERIFIED. Do not claim this is proved.\n" + "\n".join(
            f"- {a}" for a in advice
        )


def _indent(text: str, n: int) -> str:
    pad = " " * n
    return "\n".join(pad + ln for ln in text.splitlines())


def find_declarations(src: str) -> list[tuple[str, str]]:
    """All `(kind, name)` pairs declared in `src`, in source order."""
    return [(m.group(1), m.group(2)) for m in _DECL_RE.finditer(guard.strip_noise(src))]


def infer_target(src: str) -> str | None:
    """The last theorem-like declaration, which is conventionally the claim being proved."""
    decls = find_declarations(src)
    for kind, name in reversed(decls):
        if kind in ("theorem", "lemma"):
            return name
    return decls[-1][1] if decls else None


class Verifier:
    """Runs submissions through the pipeline against a live REPL session."""

    def __init__(self, session: Session, config: Config | None = None):
        self.session = session
        self.config = config or session.config

    def verify(
        self,
        source: str,
        target: str | None = None,
        claim: str | None = None,
        require_nontrivial: bool = False,
        check_vacuity: bool = True,
        timeout: float | None = None,
    ) -> Verdict:
        """Verify `source`, whose principal declaration is `target`.

        `claim` is the informal statement the proof is meant to support; it is recorded (and
        echoed back for review) but never trusted.
        """
        t0 = time.time()
        v = Verdict(
            status=Status.REJECTED,
            target=target,
            claim=claim,
            provenance=self.config.provenance(),
            source_sha256=hashlib.sha256(source.encode()).hexdigest(),
        )

        # 1. static guard --------------------------------------------------
        g = guard.check(source)
        v.checks.append(
            Check(
                "static_guard",
                g.ok,
                "" if g.ok else f"{len(g.violations)} violation(s):\n{_indent(g.render(), 4)}",
                {"violations": [vv.rule for vv in g.violations]},
            )
        )
        if not g.ok:
            v.elapsed = time.time() - t0
            return v

        # 2. elaboration ---------------------------------------------------
        try:
            resp = self.session.run(source, timeout=timeout)
        except ReplTimeout as exc:
            v.status = Status.ERROR
            v.checks.append(Check("elaboration", False, f"timed out: {exc}"))
            v.elapsed = time.time() - t0
            return v
        except ReplError as exc:
            v.status = Status.ERROR
            v.checks.append(Check("elaboration", False, str(exc)))
            v.elapsed = time.time() - t0
            return v

        v.lean_messages = resp.format_messages()
        if not resp.ok:
            v.checks.append(Check("elaboration", False, "Lean reported errors"))
            v.elapsed = time.time() - t0
            return v
        v.checks.append(Check("elaboration", True))

        # A `sorry` anywhere in the submission is fatal even if the target itself is clean,
        # because the target may depend on a sorried helper.
        sorry_warns = [w for w in resp.warnings if "sorry" in w.lower()]
        v.checks.append(
            Check(
                "no_sorry",
                not sorry_warns and not resp.sorries,
                "; ".join(sorry_warns) if sorry_warns else
                (f"{len(resp.sorries)} open goal(s)" if resp.sorries else ""),
            )
        )
        v.warnings = [w for w in resp.warnings if w not in sorry_warns]
        if sorry_warns or resp.sorries:
            v.elapsed = time.time() - t0
            return v

        # 3. resolve the target -------------------------------------------
        target = target or infer_target(source)
        v.target = target
        if not target:
            v.checks.append(Check("target_declared", False, "no theorem found in submission"))
            v.elapsed = time.time() - t0
            return v

        env = resp.env
        audits = self._audit(target, env, check_vacuity, timeout)
        if audits is None:
            v.status = Status.ERROR
            v.checks.append(Check("audit", False, "audit commands failed to run"))
            v.elapsed = time.time() - t0
            return v
        audit_resp, records, alias, canary = audits

        if audit_resp.errors:
            v.checks.append(
                Check("target_declared", False, "; ".join(audit_resp.errors)[:400])
            )
            v.elapsed = time.time() - t0
            return v
        v.checks.append(Check("target_declared", True, f"`{target}`"))

        by_check = {r.get("check"): r for r in records}
        by_decl = {(r.get("check"), r.get("decl")): r for r in records}

        # 4a. kernel re-check --------------------------------------------------
        # This must precede the axiom audit, because `#print axioms` only means something if
        # the declarations it walks were themselves accepted by the kernel. A declaration
        # inserted with `doCheck := false` reports "does not depend on any axioms" while
        # proving 4 = 5; only replaying it through the kernel exposes that.
        replay = by_check.get("replay")
        if replay is None:
            v.status = Status.ERROR
            v.checks.append(Check("kernel_replay", False, "no replay record returned"))
            v.elapsed = time.time() - t0
            return v
        replay_ok = bool(replay.get("ok"))
        v.checks.append(
            Check(
                "kernel_replay",
                replay_ok,
                ""
                if replay_ok
                else "the kernel rejected declarations the elaborator accepted: "
                + "; ".join(replay.get("failures", []))[:600],
                replay,
            )
        )
        if not replay_ok:
            v.elapsed = time.time() - t0
            return v

        # 4b. tripwire ------------------------------------------------------
        # Replay defeats a forged *environment*, but the audit commands themselves are
        # elaborated in the submission's environment, so a submission could redefine them to
        # report success. The canary is a declaration we inject that provably rests on
        # `sorryAx`; honest audit machinery must report it as untrusted. If it comes back
        # clean, the machinery has been subverted.
        canary_rec = by_decl.get(("axioms", canary))
        canary_ok = bool(canary_rec) and not canary_rec.get("trusted", True) and bool(
            canary_rec.get("uses_sorry")
        )
        v.checks.append(
            Check(
                "audit_integrity",
                canary_ok,
                ""
                if canary_ok
                else "audit machinery did not flag a known-bad canary; the submission "
                "appears to have tampered with the verifier",
                {"canary": canary_rec},
            )
        )
        if not canary_ok:
            v.elapsed = time.time() - t0
            return v

        # 4c. axioms --------------------------------------------------------
        ax = by_decl.get(("axioms", alias))
        if ax is None:
            v.status = Status.ERROR
            v.checks.append(Check("axioms", False, "no axiom record returned"))
            v.elapsed = time.time() - t0
            return v
        v.axioms = list(ax.get("axioms", []))
        untrusted = list(ax.get("untrusted", []))
        v.checks.append(
            Check(
                "trusted_axioms",
                bool(ax.get("trusted")),
                "" if ax.get("trusted") else f"untrusted: {', '.join(untrusted)}",
                ax,
            )
        )

        # 5. shape ---------------------------------------------------------
        shape = by_check.get("shape")
        if shape:
            v.statement = shape.get("statement")
            v.statement_explicit = shape.get("statement_explicit")
            v.checks.append(
                Check(
                    "statement_sorry_free",
                    not shape.get("statement_has_sorry", False),
                    "the statement itself contains `sorry`"
                    if shape.get("statement_has_sorry")
                    else "",
                    {"binders": shape.get("binders")},
                )
            )
            if not shape.get("is_theorem", True):
                v.checks.append(
                    Check(
                        "is_theorem",
                        False,
                        f"`{target}` is a definition, not a proof of a proposition",
                    )
                )

        # 6. vacuity -------------------------------------------------------
        if check_vacuity:
            vac = by_check.get("vacuity")
            if vac is not None:
                vacuous = bool(vac.get("vacuous"))
                v.checks.append(
                    Check(
                        "not_vacuous",
                        not vacuous,
                        (
                            "hypotheses are contradictory, so the theorem is vacuously true "
                            f"and supports no claim (witness: {vac.get('witness')})"
                        )
                        if vacuous
                        else "",
                        vac,
                    )
                )

            # 7. triviality ------------------------------------------------
            triv = by_check.get("triviality")
            if triv is not None and triv.get("applicable"):
                trivial = bool(triv.get("trivial"))
                v.checks.append(
                    Check(
                        "hypotheses_used",
                        not trivial,
                        (
                            f"the conclusion holds without the {triv.get('dropped')} "
                            f"hypotheses (witness: {triv.get('witness')}); the statement is "
                            "weaker than it appears"
                        )
                        if trivial
                        else "",
                        triv,
                        fatal=require_nontrivial,
                    )
                )

        v.status = Status.VERIFIED if not v.failures else Status.REJECTED
        v.elapsed = time.time() - t0
        return v

    def _audit(
        self, target: str, env: int | None, check_vacuity: bool, timeout: float | None
    ) -> tuple[ReplResponse, list[dict[str, Any]], str, str] | None:
        """Run the audit commands in the environment produced by the submission.

        Three precautions make the result hard to forge:

        * **Kernel replay first.** Every declaration the submission added is re-checked by
          the kernel before anything else is believed, because a declaration inserted into
          the environment without a kernel check reports a clean axiom footprint while
          proving something false. Replay runs before the helper declarations below are
          introduced, so it sees exactly the submission's own constants.
        * The target is audited through an **alias with an unpredictable name**, so a
          subverted elaborator cannot special-case the declaration it needs to lie about.
          `def alias := @target` reproduces the target's axiom footprint exactly, since the
          alias depends on it and on nothing else.
        * A **canary** with an indistinguishable name is audited alongside it. The canary is
          built from a `sorry`, so honest machinery must report it untrusted; machinery that
          has been rigged to report success clears the canary too and is caught.
        """
        nonce = secrets.token_hex(6)
        alias = f"{_HELPER_PREFIX}{nonce}a"
        seed = f"{_HELPER_PREFIX}{nonce}b"
        canary = f"{_HELPER_PREFIX}{nonce}c"

        setup = [
            f"theorem {seed} : (2 : Nat) + 2 = 5 := by sorry",
            # `@` matters: without it Lean inserts metavariables for the declaration's
            # leading implicit and instance-implicit binders, and instance resolution gets
            # stuck ("typeclass instance problem is stuck"), so the alias never gets
            # defined and a perfectly good proof is reported as unverifiable. The canary is
            # written the same way so the two remain indistinguishable.
            f"def {alias} := @{target}",
            f"def {canary} := @{seed}",
        ]
        # Randomise the order so position carries no information either.
        audit_pair = [f"#audit_axioms {alias}", f"#audit_axioms {canary}"]
        if secrets.randbelow(2):
            audit_pair.reverse()

        # The replay command is told to ignore our own helpers by name prefix; it runs first
        # regardless, so they do not yet exist.
        cmds = [f"#audit_replay {_HELPER_PREFIX}"] + setup + audit_pair
        cmds += [f"#audit_shape {target}"]
        if check_vacuity:
            cmds += [f"#audit_vacuity {target}", f"#audit_triviality {target}"]
        try:
            resp = self.session.run("\n".join(cmds), env=env, timeout=timeout)
        except ReplError:
            return None
        return resp, resp.audit_records(), alias, canary

    def check_statement(self, statement: str, timeout: float | None = None) -> dict[str, Any]:
        """Elaborate a bare statement (no proof) to confirm it means what it should.

        Used before attempting a proof: it catches type errors and, critically, reports
        whether the hypotheses are already contradictory - which means no proof of it would
        be worth anything.
        """
        name = "nulliusStatementProbe"
        src = f"theorem {name} {statement} := by sorry"
        g = guard.check(src)
        if not g.ok:
            return {"ok": False, "error": "guard", "violations": g.render()}
        try:
            resp = self.session.run(src, timeout=timeout)
        except ReplError as exc:
            return {"ok": False, "error": str(exc)}
        if not resp.ok:
            return {"ok": False, "error": "elaboration", "messages": resp.format_messages()}
        audit = self.session.run(
            f"#audit_shape {name}\n#audit_vacuity {name}", env=resp.env, timeout=timeout
        )
        recs = {r.get("check"): r for r in audit.audit_records()}
        shape = recs.get("shape", {})
        vac = recs.get("vacuity", {})
        return {
            "ok": True,
            "statement": shape.get("statement"),
            "statement_explicit": shape.get("statement_explicit"),
            "binders": shape.get("binders"),
            "vacuous": bool(vac.get("vacuous")),
            "vacuity_witness": vac.get("witness"),
            "open_goals": [s.get("goal") for s in resp.sorries],
        }
