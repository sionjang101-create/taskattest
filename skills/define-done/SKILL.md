---
name: define-done
description: Set up taskattest's completion gate for the current task — write .taskattest/spec.json so the agent's "done" is checked before it can stop. Use when the person says "define done", "set up the gate", "check my work before you stop", "don't let me stop until it passes", "limit what you may change", "only touch src/x", or asks to verify a deliverable with taskattest.
---

# Define done for this task

taskattest checks a definition of done written in `.taskattest/spec.json` at the
project root. With the plugin enabled, the Stop hook runs it every time the agent
tries to finish, and blocks the stop with structured fixes while it fails. Without
a spec the gate does nothing, so this skill is how the gate gets switched on.

## Steps

1. Ask what the deliverable is and what the task may change, unless the person
   already said. Do not guess the scope from what would be convenient.
2. If there are output files, call the `draft_spec` tool on the inputs and
   outputs to get a starting spec, then review every check with the person. A
   draft describes the files as they are, not the task; it carries
   `"draft": true` until a person has reviewed it.
3. If the person named what may change, add a scope check. For a spec in
   `.taskattest/`, `root` is `..`:

   ```json
   {"type": "scope_changes", "name": "only the billing module and its tests",
    "root": "..", "allow": ["src/billing/**", "tests/**"]}
   ```

   Use `deny` for paths that must never change (for example `"**/*.lock"`).
4. Call `describe_checks` if you need a check type that is not above.
5. Write the spec to `.taskattest/spec.json`, run `verify_task` on it once, and
   show the person the verdict so they know what the gate will hold you to.

## Rules

- The spec is the person's definition of done. Once it exists, never edit it,
  the inputs, or `.taskattest/` to make a check pass. If a check is wrong, say so
  and let the person change it.
- When the gate blocks, follow the structured `fix` in the reason. If the fix
  asks to undo a change the person explicitly requested, stop and ask them.
- The gate hands over to the person after the same failure three times; report
  what is still failing instead of trying again.
