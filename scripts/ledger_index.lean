import Loogle

open Lean

private def buildIndex (targets : Array String) (depHash : String)
    (output : System.FilePath) : CoreM Unit := do
  let mut relation : Loogle.NameRel := {}
  let mut trie : Loogle.Find.SuffixTrie := .empty
  for target in targets do
    let name := target.toName
    let ci ← getConstInfo name
    let isTheorem := match ci with | .thmInfo _ => true | _ => false
    unless isTheorem && (`NulliusLedgerResults).isPrefixOf name do
      throwError "not an exported ledger theorem: {name}"
    relation ← (Loogle.Find.addDecl name ci relation).run'
    trie ← (Loogle.Find.SuffixTrie.addDecl name ci trie).run'
  Loogle.pickle output ((depHash, relation, trie) : Loogle.Find.PickledIndex)

unsafe def main (args : List String) : IO UInt32 := do
  let [root, traceFile, output, targetsFile] := args
    | throw <| IO.userError "expected ROOT TRACE INDEX TARGETS"
  let trace ← IO.ofExcept <| Json.parse (← IO.FS.readFile traceFile)
  let depHash ← IO.ofExcept <| trace.getObjValAs? String "depHash"
  let targetsJson ← IO.ofExcept <| Json.parse (← IO.FS.readFile targetsFile)
  let targets ← IO.ofExcept <| fromJson? (α := Array String) targetsJson
  enableInitializersExecution
  let env ← importModules (loadExts := true) #[{module := root.toName}] {}
  let action := buildIndex targets depHash output
  discard <| action.toIO {fileName := "<ledger-index>", fileMap := default} {env}
  IO.println s!"Indexed {targets.size} ledger targets."
  return 0
