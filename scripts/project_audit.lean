import ActionSpeaks.Audit
import Lean.Replay

open Lean Elab

deriving instance BEq for Lean.InductiveVal
deriving instance BEq for Lean.QuotKind
deriving instance BEq for Lean.QuotVal

private def compatible (a b : ConstantInfo) : Bool :=
  match a, b with
  | .thmInfo x, .thmInfo y =>
    x.toConstantVal == y.toConstantVal && x.all == y.all
  | .defnInfo x, .defnInfo y => x == y
  | .opaqueInfo x, .opaqueInfo y => x == y
  | .axiomInfo x, .axiomInfo y => x == y
  | .inductInfo x, .inductInfo y => x == y
  | .ctorInfo x, .ctorInfo y => x == y
  | .recInfo x, .recInfo y => x == y
  | .quotInfo x, .quotInfo y => x == y
  | _, _ => false

private def auditTarget (target : String) (nontrivial : Bool)
    (original : Environment) : CoreM Json := do
  let stx ← match Parser.runParserCategory (← getEnv) `term target with
    | .ok s => pure s
    | .error e => throwError "{e}"
  unless stx.isIdent do throwError "target must be a declaration name"
  let name ← realizeGlobalConstNoOverload stx
  let ci ← getConstInfo name
  let axioms ← withEnv original (collectAxioms name)
  let statement ← (PrettyPrinter.ppExpr ci.type).run'
  let explicit ← (withOptions (fun o => o.setBool `pp.explicit true)
    (PrettyPrinter.ppExpr ci.type)).run'
  let probes : MetaM (Option String × Option String) := Term.TermElabM.run' do
    let vacuous ← ActionSpeaks.Audit.probe (← ActionSpeaks.Audit.vacuityGoal name)
    let trivial ← match ← ActionSpeaks.Audit.trivialityGoal name with
      | none => pure none
      | some (goal, _) => ActionSpeaks.Audit.probe goal
    return (vacuous, trivial)
  let (vacuous, trivial) ← probes.run'
  let trusted := axioms.toList.all ActionSpeaks.Audit.trustedAxioms.contains
  let checks := [
    Json.mkObj [("name", toJson "is_theorem"), ("passed", toJson ci.isThm)],
    Json.mkObj [("name", toJson "trusted_axioms"), ("passed", toJson trusted)],
    Json.mkObj [("name", toJson "statement_sorry_free"), ("passed", toJson (!ci.type.hasSorry))],
    Json.mkObj [("name", toJson "not_vacuous"), ("passed", toJson vacuous.isNone),
      ("detail", toJson (vacuous.getD ""))],
    Json.mkObj [("name", toJson "hypotheses_used"), ("passed", toJson trivial.isNone),
      ("fatal", toJson nontrivial), ("detail", toJson (trivial.getD ""))]]
  return Json.mkObj [
    ("target", toJson name.toString), ("statement", toJson statement.pretty),
    ("statement_explicit", toJson explicit.pretty), ("checks", toJson checks),
    ("axioms", toJson (axioms.toList.map Name.toString))]

unsafe def main (args : List String) : IO UInt32 := do
  let [moduleName, target, nontrivial] := args
    | throw <| IO.userError "expected MODULE TARGET REQUIRE_NONTRIVIAL"
  let imports : Array Import := #[
    {module := `Mathlib, importAll := true}, {module := `Physlib, importAll := true},
    {module := `Cslib, importAll := true}, {module := `ActionSpeaks.Audit, importAll := true}]
  enableInitializersExecution
  let base ← importModules (loadExts := true) imports {}
  let basePath ← searchPathRef.get
  if let some projectPath ← IO.getEnv "ACTION_SPEAKS_PROJECT_LEAN_PATH" then
    searchPathRef.set (System.SearchPath.parse projectPath)
  let env ← importModules (loadExts := false)
    #[{module := moduleName.toName, importAll := true}] {}
  searchPathRef.set basePath
  let mut extra : Std.HashMap Name ConstantInfo := {}
  for (name, ci) in env.constants.toList do
    if let some original := base.find? name then
      unless compatible ci original do
        IO.println s!"ACTION_SPEAKS_PROJECT {Json.mkObj [
          ("environment_error", toJson s!"project replaces trusted declaration {name}")] |>.compress}"
        return 0
    else
      extra := extra.insert name ci
  -- Replay whole declaration groups, including inductives and their generated recursors.
  let checked ← try
      Environment.replay extra base
    catch e =>
      IO.println s!"ACTION_SPEAKS_PROJECT {Json.mkObj [
        ("replay_error", toJson e.toString)] |>.compress}"
      return 0
  -- Retain trusted parser/tactic state and expose only the kernel-checked project constants.
  let mut audited := base
  for (name, _) in extra.toList do
    if let some ci := checked.constants.map₂.find? name then
      let addition ← audited.addConstAsync name (.ofConstantInfo ci) (reportExts := false)
      addition.commitConst checked (some ci) (some ci)
      addition.commitCheckEnv checked
      audited := addition.mainEnv
  let (result, _) ← (auditTarget target (nontrivial == "true") env).toIO
    {fileName := "<project-audit>", fileMap := default} {env := audited}
  IO.println s!"ACTION_SPEAKS_PROJECT {result.compress}"
  return 0
