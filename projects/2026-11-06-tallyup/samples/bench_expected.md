## Settle-up: transfers needed

| members | balances | trials | greedy mean | optimal mean | optimal < greedy | max saved |
|---:|---|---:|---:|---:|---:|---:|
| 4 | mixed | 300 | 3.00 | 3.00 | 0.0% | 0 |
| 6 | mixed | 300 | 5.00 | 5.00 | 0.0% | 0 |
| 8 | mixed | 300 | 7.00 | 7.00 | 0.0% | 0 |
| 10 | mixed | 300 | 9.00 | 8.99 | 1.0% | 1 |
| 12 | mixed | 300 | 10.99 | 10.98 | 1.7% | 1 |
| 4 | sub-groups | 300 | 1.99 | 1.99 | 0.0% | 0 |
| 6 | sub-groups | 300 | 3.07 | 2.99 | 8.3% | 1 |
| 8 | sub-groups | 300 | 5.38 | 4.97 | 37.3% | 2 |
| 10 | sub-groups | 300 | 6.57 | 5.96 | 50.0% | 2 |
| 12 | sub-groups | 300 | 7.92 | 6.96 | 67.3% | 3 |

## Rounding drift over a stream of expenses

| members | expenses | fixed-order max abs drift (minor units) | drift-aware max abs drift |
|---:|---:|---:|---:|
| 3 | 2000 | 239.03 | 0.82 |
| 4 | 2000 | 240.87 | 1.05 |
| 6 | 2000 | 232.71 | 0.96 |
