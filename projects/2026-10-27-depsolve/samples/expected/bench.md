## Random registries (1000 seeds, 3-5 packages, exhaustive oracle)

| strategy | solvable found | candidates tried |
|---|---:|---:|
| oracle (brute force) | 228/228 | - |
| greedy, no backtracking | 163/228 | - |
| chronological backtracking | 228/228 | 944 |
| backjumping (default) | 228/228 | 932 |

Unsatisfiable seeds: 772; invalid solutions returned by the resolver: 0.

## Deep conflict (a@2 -> q -> w ^2 vs root w ^1, k independent 4-version packages)

| k | chronological candidates | backjumping candidates | backjumps |
|---:|---:|---:|---:|
| 1 | 28 | 10 | 1 |
| 2 | 105 | 12 | 2 |
| 3 | 410 | 14 | 3 |
| 4 | 1627 | 16 | 4 |
| 5 | 6492 | 18 | 5 |
| 6 | 25949 | 20 | 6 |
| 7 | 103774 | 22 | 7 |
