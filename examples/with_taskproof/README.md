# Using taskattest as taskproof's acceptance command

[taskproof](https://github.com/HaoHaoP/taskproof) dispatches work to coding agents and then runs
an acceptance command, whose exit code decides. taskattest exits `0` on `pass` and `1` otherwise,
so it can be that command.

In taskproof's `projects.toml`:

```toml
[[taskgroup]]
id = "reports"
project = "reports"
verify = "taskattest --spec .taskattest/spec.json"
verify_kind = "check"
```

taskproof runs `verify` in the project's working directory after the agent exits, so a relative
spec path works. The check details taskattest prints land in taskproof's ledger as the command's
output, and taskattest's evidence bundle is written next to the spec.

What this adds over a hand-written command: the acceptance condition is declared as data and
judged by typed checks — invented records, tampered inputs, totals recomputed from the inputs —
with a replayable evidence digest, instead of whatever a script happened to test.

This combination has not been tested inside taskproof itself yet; the configuration above follows
its documented `projects.example.toml`. If you try it, an issue saying whether it worked is welcome.
