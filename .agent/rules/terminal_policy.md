---
trigger: always_on
---

# Terminal Execution Policy & Command Deny List

## 1. Terminal Execution Policy
- **Policy**: `Request Review`
- Terminal commands that execute external network calls, file deletions, or package mutations require explicit review before running.

## 2. Prohibited & Denied Commands (Deny List)
The following commands and patterns are strictly blocked:
1. **Raw Data Modification or Deletion**:
   - `rm -rf data/raw*`, `Remove-Item *data/raw*`, `del *data/raw*`, `rmdir *data/raw*`
   - Any shell command attempting to overwrite, move, or delete immutable files under `data/raw*`.
2. **Unapproved Spend & Network Pulls**:
   - `databento download ...` without prior cost check (`metadata.get_cost()`) and user approval.
3. **Environment & Credential Leakage**:
   - `cat .env`, `Get-Content .env`, `printenv`, or printing any API keys.
4. **Frozen Specification Modification**:
   - Modifying `docs/prereg.md`, `docs/acceptance.yaml`, or `docs/SPEC.md` directly via shell scripts.
