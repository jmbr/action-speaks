import Mathlib

/-!
# LeanAI.Audit — machine-checkable auditing of agent-produced theorems

`lake build`-ing a file only answers "does it elaborate?". For an agent that is trying to
*back up a claim*, that is the least interesting question. This module adds the checks that
catch the two ways an agent's "proof" can be worthless:

1. **Unsound / dishonest proof** — `sorry`, a freshly declared `axiom`, `native_decide`.
   Caught by `#audit_axioms`, which compares the declaration's axiom footprint against the
   trusted set `{propext, Classical.choice, Quot.sound}`.

2. **Sound proof of the wrong statement** — the autoformalization gap. A theorem whose
   hypotheses are contradictory is *vacuously* true and proves nothing; a theorem whose
   conclusion holds without its hypotheses is weaker than advertised. Caught by
   `#audit_vacuity` and `#audit_triviality`.

Each command emits one `info` message of the form `LEANAI_AUDIT {json}` so the Python driver
can parse results instead of scraping the pretty-printer.
-/

open Lean Elab Command Term Meta Tactic

namespace LeanAI.Audit

/-- Axioms a legitimately-proved classical theorem may depend on. Anything else — `sorryAx`,
`Lean.ofReduceBool` (from `native_decide`), or a user-declared `axiom` — is disqualifying. -/
def trustedAxioms : List Name := [``propext, ``Classical.choice, ``Quot.sound]

private def jstr (s : String) : String := (Json.str s).compress

private def jnames (ns : List Name) : String :=
  "[" ++ String.intercalate "," (ns.map fun n => jstr n.toString) ++ "]"

private def jbool (b : Bool) : String := if b then "true" else "false"

private def emit (payload : String) : CommandElabM Unit :=
  logInfo m!"LEANAI_AUDIT {payload}"

/-- The tactic battery used for vacuity/triviality probes. Each entry must be a *finishing*
tactic: it either closes the goal or fails. Entries are parenthesised because
`runParserCategory ... \`tactic` accepts a single tactic, and `(a; b)` is one tactic whereas
`a; b` is not. Each begins with `intros` because the probe goals are `∀`/`→` chains. -/
def probeTactics : List String :=
  ["(intros; omega)",
   "(intros; simp_all)",
   "(intros; contradiction)",
   "(intros; decide)",
   "(intros; norm_num)",
   "(intros; tauto)",
   "(intros; linarith)",
   "(intros; nlinarith)",
   "(intros; aesop)",
   "(intros; simp_all <;> omega)",
   "(intros; exact absurd ‹_› (by decide))"]

/-- Attempt to discharge `goal` with the tactic script `tacStr`, in a sandbox that cannot
modify the environment and whose diagnostics are discarded. Returns `true` only for a
complete, `sorry`-free, metavariable-free proof term. `maxHeartbeats` is bounded so a probe
cannot hang the driver. -/
def tryTactic (goal : Expr) (tacStr : String) (heartbeats : Nat := 200000) :
    TermElabM Bool := do
  let env ← getEnv
  match Parser.runParserCategory env `tactic tacStr with
  | .error _ => return false
  | .ok tacStx =>
    -- Probes are diagnostics, not part of the artefact: never let their warnings or errors
    -- leak into the message log the driver parses.
    let savedLog ← Core.getMessageLog
    let result ←
      try
        withoutModifyingEnv <| withoutModifyingState <| withOptions
            (fun o => ((o.insert `maxHeartbeats (.ofNat heartbeats)).insert `maxRecDepth
              (.ofNat 512)).insert `linter.unusedTactic (.ofBool false)) do
          -- `withoutErrToSorry` is essential: otherwise a failed elaboration silently becomes
          -- `sorryAx` and the probe would report a bogus success.
          Term.withoutErrToSorry do
            let mv ← mkFreshExprMVar goal
            let remaining ← Tactic.run mv.mvarId! (Tactic.evalTactic tacStx)
            unless remaining.isEmpty do return false
            Term.synthesizeSyntheticMVarsNoPostponing
            let e ← instantiateMVars mv
            return !e.hasSorry && !e.hasExprMVar
      catch _ => pure false
    Core.setMessageLog savedLog
    return result

/-- Run the whole probe battery, returning the first tactic that closes `goal`. -/
def probe (goal : Expr) : TermElabM (Option String) := do
  for tac in probeTactics do
    if ← tryTactic goal tac then return some tac
  return none

/-- `∀ binders, False` — the goal asserting that `n`'s hypotheses are jointly contradictory. -/
def vacuityGoal (n : Name) : MetaM Expr := do
  let ci ← getConstInfo n
  forallTelescopeReducing ci.type fun xs _ => mkForallFVars xs (mkConst ``False)

/-- The conclusion of `n` with its propositional hypotheses removed, i.e. the goal asserting
the conclusion holds *without* the stated assumptions. `none` when there are no droppable
hypotheses, or when dropping them would leave the goal ill-formed (dependent binders). -/
def trivialityGoal (n : Name) : MetaM (Option (Expr × Nat)) := do
  let ci ← getConstInfo n
  forallTelescopeReducing ci.type fun xs concl => do
    let mut keep : Array Expr := #[]
    let mut dropped := 0
    for x in xs do
      let ty ← inferType x
      let tyIsProp ← isProp ty
      if tyIsProp && !concl.containsFVar x.fvarId! then
        dropped := dropped + 1
      else
        keep := keep.push x
    if dropped == 0 then return none
    let g ← mkForallFVars keep concl
    -- Any fvar surviving abstraction came from a dropped binder: the statement is dependent
    -- and cannot be meaningfully stripped.
    if g.hasFVar || g.hasLooseBVars then return none
    return some (g, dropped)

/-- `#audit_axioms foo` — report `foo`'s axiom footprint and whether it lies within the
trusted set. This is the authoritative soundness check: it sees through any tactic, macro,
or `set_option` trickery, because it inspects the kernel-level dependencies of the proof term. -/
syntax (name := auditAxioms) "#audit_axioms " ident : command

@[command_elab auditAxioms]
def elabAuditAxioms : CommandElab := fun stx => do
  match stx with
  | `(#audit_axioms $i:ident) => do
    let n ← liftCoreM <| realizeGlobalConstNoOverload i
    let axs ← liftCoreM <| collectAxioms n
    let axl := axs.toList
    let untrusted := axl.filter fun a => !trustedAxioms.contains a
    let usesSorry := axl.any fun a => a == ``sorryAx
    emit s!"\{\"check\":\"axioms\",\"decl\":{jstr n.toString},\"axioms\":{jnames axl},\"untrusted\":{jnames untrusted},\"uses_sorry\":{jbool usesSorry},\"trusted\":{jbool untrusted.isEmpty}}"
  | _ => throwUnsupportedSyntax

/-- `#audit_vacuity foo` — try to derive `False` from `foo`'s hypotheses. On success, `foo`
is vacuously true: it is a valid theorem that carries no information about the claim it was
meant to formalize. This is the single most common way an autoformalized statement is wrong. -/
syntax (name := auditVacuity) "#audit_vacuity " ident : command

@[command_elab auditVacuity]
def elabAuditVacuity : CommandElab := fun stx => do
  match stx with
  | `(#audit_vacuity $i:ident) => do
    let n ← liftCoreM <| realizeGlobalConstNoOverload i
    let goal ← liftTermElabM <| vacuityGoal n
    let res ← liftTermElabM <| probe goal
    match res with
    | some tac =>
      emit s!"\{\"check\":\"vacuity\",\"decl\":{jstr n.toString},\"vacuous\":true,\"witness\":{jstr tac}}"
    | none =>
      emit s!"\{\"check\":\"vacuity\",\"decl\":{jstr n.toString},\"vacuous\":false,\"witness\":null}"
  | _ => throwUnsupportedSyntax

/-- `#audit_triviality foo` — try to prove `foo`'s conclusion after deleting its hypotheses.
On success the hypotheses are inert, which usually means the formalization dropped the
content of the informal claim (e.g. proving `0 ≤ n` for naturals when the claim was about a
genuine constraint). -/
syntax (name := auditTriviality) "#audit_triviality " ident : command

@[command_elab auditTriviality]
def elabAuditTriviality : CommandElab := fun stx => do
  match stx with
  | `(#audit_triviality $i:ident) => do
    let n ← liftCoreM <| realizeGlobalConstNoOverload i
    match ← liftTermElabM (trivialityGoal n) with
    | none =>
      emit s!"\{\"check\":\"triviality\",\"decl\":{jstr n.toString},\"applicable\":false,\"trivial\":false,\"witness\":null,\"dropped\":0}"
    | some (goal, dropped) =>
      let res ← liftTermElabM <| probe goal
      match res with
      | some tac =>
        emit s!"\{\"check\":\"triviality\",\"decl\":{jstr n.toString},\"applicable\":true,\"trivial\":true,\"witness\":{jstr tac},\"dropped\":{dropped}}"
      | none =>
        emit s!"\{\"check\":\"triviality\",\"decl\":{jstr n.toString},\"applicable\":true,\"trivial\":false,\"witness\":null,\"dropped\":{dropped}}"
  | _ => throwUnsupportedSyntax

/-- `#audit_shape foo` — the fully elaborated statement of `foo`, with implicit arguments
made explicit. The driver shows this to the reviewing agent so that back-translation is done
against what Lean *actually* elaborated, not against the surface syntax the author wrote. -/
syntax (name := auditShape) "#audit_shape " ident : command

@[command_elab auditShape]
def elabAuditShape : CommandElab := fun stx => do
  match stx with
  | `(#audit_shape $i:ident) => do
    let n ← liftCoreM <| realizeGlobalConstNoOverload i
    let ci ← liftCoreM <| getConstInfo n
    let (nBinders, stmt, explicitStmt, isThm) ← liftTermElabM do
      -- The readable form is what a human compares against the informal claim; the explicit
      -- form disambiguates coercions and instances when the readable form is suspicious.
      let s ← PrettyPrinter.ppExpr ci.type
      let sx ← withOptions (fun o => o.insert `pp.explicit (.ofBool true)) do
        PrettyPrinter.ppExpr ci.type
      let nb ← forallTelescopeReducing ci.type fun xs _ => pure xs.size
      pure (nb, s.pretty, sx.pretty, ci.isThm)
    emit s!"\{\"check\":\"shape\",\"decl\":{jstr n.toString},\"binders\":{nBinders},\"is_theorem\":{jbool isThm},\"statement\":{jstr stmt},\"statement_explicit\":{jstr explicitStmt},\"statement_has_sorry\":{jbool ci.type.hasSorry}}"
  | _ => throwUnsupportedSyntax

end LeanAI.Audit
