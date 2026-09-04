# Build log

One directory per stage, in the order they were built. Each `notes.txt` records
what the stage does, the concept behind each decision, and **where that concept
lives in the code** -- because a principle nobody can point at in a file is a
principle that quietly stops being true.

| Stage | Subject                          | Commit    |
|-------|----------------------------------|-----------|
| 1     | Scaffold, schema, domain core    | `fee141b`, `9af1425` |
| 2     | Control API create path          | `4c32927` |
| 3     | Provisioner                      | `d6e6913` |
| 4     | Gateway wiring                   | `49b8180` |
| 5     | Reconciler and RBAC              | `ed13f34` |
| 6     | GPU placement, without a GPU     | `0307145` |
| 7     | Colab probe: real vLLM findings   | `2dc8e18` |

Read them in order. Each stage assumes the one before it.

Architecture doc (the "why" behind all of this):
https://claude.ai/code/artifact/fae767c7-1da9-4428-a50f-773f2739392c

The section references below (§2.1, §3.2 ...) point into that document.
