---
description: Show or change nestor-lean settings (presets, floors, saving ratio) — no restart needed
---

Run the command below and show the user its output verbatim.

With no arguments it prints every setting, its value, and where that value came
from. To change something, pass a setting and a value — for example
`preset aggressive`, or `bash_min_chars 500`. Add `--project` before the pair to
write to `./.nestor-lean.json` instead of the machine-wide config, or `--reset`
to clear it.

Arguments given by the user: $ARGUMENTS

```
python "${CLAUDE_PLUGIN_ROOT}/hooks/configcmd.py" $ARGUMENTS
```
