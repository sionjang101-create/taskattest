# Example: is a robot recording fit to train on?

A robot arm recorder wrote five sessions at 20 Hz. Before they go into a training set, you want to
know that each session was recorded whole and the recorder behaved. Reading 324 frames by eye does
not scale; a summary that says "5 sessions" may not notice that one of them is empty.

The recordings here are synthetic, written for this example. Each session hides at most one problem:

| Session | What is wrong |
| --- | --- |
| 1 | nothing |
| 2 | one frame was dropped |
| 3 | the arm values freeze for 3 s |
| 4 | the recording stops early, without a final `done` |
| 5 | the file is empty |

```bash
python -m taskattest --spec examples/recording_quality/spec.json
```

```
  - fail    every session recorded: 4 distinct session; expected exactly 5
  - fail    no dropped frames: 320 consecutive pair(s) checked, 1 violation(s), first at step 117 (session 2): changed by 2, expected 1
  - fail    clock steady at 20 Hz: 320 consecutive pair(s) checked, 1 violation(s), first at step 117 (session 2): changed by 0.1, expected 0.05
  - fail    arm never frozen for more than 2 s: longest run of unchanged joint1, joint2, gripper: 60 step(s), allowed 40; 1 stall(s), first from step 189 (session 3)
  - fail    every session ends done: 3 of 4 session group(s) end in the condition; wrong end: 4
  - pass    joints finite and in range: 324 step(s) checked, 0 violation(s)
```

Every problem is found and placed in its session. Each check is one entry in
[spec.json](spec.json):

- `trace_count` with `distinct` counts the sessions that actually start; the empty one is missing.
- `trace_delta` checks consecutive frames: the frame number must rise by 1 and the clock by 0.05 s.
- `trace_stall` fails when the same joint values repeat for more than 40 frames (2 s).
- `trace_final` with `group_by` checks that every session's last frame is marked `done`.
- `trace_always` keeps every value finite and in range; NaN and infinity fail it.

The bounds come from the recorder (20 Hz) and the arm (joint limits), not from the data, so a
recording cannot pass by being whatever it happens to be. The same checks read JSONL, CSV, a JSON
array, or a raw log with `line_prefix`.

Checks like these are most useful on raw recordings, right after capture. Datasets that have
already been converted and cleaned tend to have their clock rebuilt, and the evidence of a dropped
frame or a stalled recorder is gone by then.
