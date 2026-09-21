# CHANGELOG

<!-- version list -->

## v0.2.0 (2026-09-21)

### Bug Fixes

- Include FloatLib in verdict provenance
  ([`fa96447`](http://localhost:3000/jmbr/action-speaks-louder-than-words/commit/fa964470171f09e83315035ae4b1a3b752161b65))

- Judge library support by branch as well as tag
  ([`bb1ee06`](http://localhost:3000/jmbr/action-speaks-louder-than-words/commit/bb1ee06a41ceb9c631cc5ef28fcbd1e5e28c24b2))

### Build System

- Cut releases with python-semantic-release
  ([`d218b3f`](http://localhost:3000/jmbr/action-speaks-louder-than-words/commit/d218b3fcd1858f303397161f159f81a044ad2f34))

### Documentation

- Ask for shorter commit messages and comments
  ([`73a009e`](http://localhost:3000/jmbr/action-speaks-louder-than-words/commit/73a009eea34d4915b15108a572b6054bae4207f4))

- Split README into focused guides
  ([`3092995`](http://localhost:3000/jmbr/action-speaks-louder-than-words/commit/3092995a16af9c86585a4eceafec54100a5bc894))

### Features

- Move to Lean v4.34.0 and add FloatLib
  ([`1a43f4f`](http://localhost:3000/jmbr/action-speaks-louder-than-words/commit/1a43f4fea7390746121109f4f9a4ffe0ee6ea94e))

- Rename nullius to action-speaks
  ([`9cbe6aa`](http://localhost:3000/jmbr/action-speaks-louder-than-words/commit/9cbe6aa19992565d293f1383f39d1176a8a4fa81))

### Refactoring

- Drop the skill wrapper for the installed command
  ([`a2692c9`](http://localhost:3000/jmbr/action-speaks-louder-than-words/commit/a2692c9622d4e28d3dd3e11445367ceddd4d3263))

- Port the installer to Python
  ([`e50950f`](http://localhost:3000/jmbr/action-speaks-louder-than-words/commit/e50950fd2971adbaa96d487800fd01180fe86eb7))

### Breaking Changes

- The skill runs `action-speaks`. Callers of skills/action-speaks/scripts/action-speaks should use
  it directly, and `verify "CLAIM" < proof.lean` becomes `verify - -c "CLAIM"`.


## v0.1.0 (2026-09-20)

- Initial Release
