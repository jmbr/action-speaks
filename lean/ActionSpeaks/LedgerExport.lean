import ActionSpeaks.Audit

open Lean Elab Command

namespace ActionSpeaks.LedgerExport

initialize savedEnvironment : IO.Ref (Option Environment) ← IO.mkRef none

syntax "#ledger_begin" : command

elab_rules : command
  | `(#ledger_begin) => do savedEnvironment.set (some (← getEnv))

private partial def renameExpr (names : NameMap Name) (e : Expr) : Expr :=
  e.replace fun e => match e with
    | .const n ls => (names.find? n).map fun n' => .const n' ls
    | .proj n i x => (names.find? n).map fun n' => .proj n' i (renameExpr names x)
    | _ => none

private partial def dependencies (env base : Environment) (n : Name)
    (seen : NameSet) (ordered : Array ConstantInfo) :
    Except String (NameSet × Array ConstantInfo) := do
  if base.contains n || seen.contains n then return (seen, ordered)
  let some ci := env.find? n | throw s!"missing declaration {n}"
  let value ← match ci with
    | .thmInfo v => pure v.value
    | .defnInfo v => pure v.value
    | .opaqueInfo v => pure v.value
    | _ => throw s!"unsupported local declaration {n}: only theorem/def/opaque dependencies can be exported"
  let mut seen := seen.insert n
  let mut ordered := ordered
  for dep in (ci.type.getUsedConstantsAsSet ++ value.getUsedConstantsAsSet).toList do
    let result ← dependencies env base dep seen ordered
    seen := result.1
    ordered := result.2
  return (seen, ordered.push ci)

private def renameDecl (names : NameMap Name) (ci : ConstantInfo) : Declaration :=
  let n := (names.find? ci.name).getD ci.name
  let ty := renameExpr names ci.type
  match ci with
    | .thmInfo v => .thmDecl { v with name := n, type := ty, value := renameExpr names v.value }
    | .defnInfo v => .defnDecl { v with name := n, type := ty, value := renameExpr names v.value }
    | .opaqueInfo v => .opaqueDecl { v with name := n, type := ty, value := renameExpr names v.value }
    | _ => ci.toDeclaration!

syntax "#ledger_export " str str : command

elab_rules : command
  | `(#ledger_export $target:str $key:str) => do
    let some base ← savedEnvironment.get | throwError "missing #ledger_begin"
    let env ← getEnv
    let targetSyntax ← match Parser.runParserCategory env `term target.getString with
      | .ok stx => pure stx
      | .error e => throwError "invalid target identifier: {e}"
    unless targetSyntax.isIdent do throwError "target must be a declaration name"
    let name ← liftCoreM <| realizeGlobalConstNoOverload targetSyntax
    let ci ← getConstInfo name
    unless ci.isThm do throwError "ledger target is not a theorem"
    let decls ← match dependencies env base name {} #[] with
      | .ok (_, ds) => pure ds
      | .error e => throwError "{e}"
    let stem := Name.str `ActionSpeaksLedgerEntries key.getString
    let resultName := Name.str (Name.str `ActionSpeaksLedgerResults key.getString) name.getString!
    let mut names : NameMap Name := {}
    for d in decls do
      names := names.insert d.name (if d.name == name then resultName else stem ++ d.name)
    -- Rename elaborated terms, not source text, and kernel-check each copied declaration.
    setEnv base
    for d in decls do
      liftCoreM <| addDecl (renameDecl names d)
    if base.contains name then
      liftCoreM <| addDecl (.thmDecl {
        name := resultName, levelParams := ci.levelParams, type := ci.type,
        value := mkConst name (ci.levelParams.map Level.param) })
    let axioms ← liftCoreM <| collectAxioms resultName
    unless axioms.toList.all ActionSpeaks.Audit.trustedAxioms.contains do
      throwError "exported target has untrusted axioms"
    let vacuous ← liftTermElabM do
      ActionSpeaks.Audit.probe (← ActionSpeaks.Audit.vacuityGoal resultName)
    if let some witness := vacuous then
      throwError "exported statement has contradictory hypotheses ({witness})"
    let result ← getConstInfo resultName
    let statement ← liftTermElabM <| PrettyPrinter.ppExpr result.type
    unless !result.type.hasSorry do throwError "exported statement contains sorry"
    addDeclarationRangesFromSyntax resultName (← getRef)
    let record := Json.mkObj [
      ("name", toJson resultName.toString), ("statement", toJson statement.pretty),
      ("axioms", toJson (axioms.toList.map Name.toString))]
    logInfo m!"ACTION_SPEAKS_LEDGER_EXPORT {record.compress}"

end ActionSpeaks.LedgerExport
