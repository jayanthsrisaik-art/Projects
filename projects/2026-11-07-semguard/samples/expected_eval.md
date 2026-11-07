dev topics (1043 queries): api, billing, credentials, travel
test topics (1957 queries): auth_2fa, coding, data, leave, live_data, orders, security
threshold chosen on dev for hit precision >= 0.99

| policy (test split) | threshold | hit rate | hit precision | recall of reusable | wrong hits | wrong by kind |
|---|---|---|---|---|---|---|
| exact_match | exact | 0.547 | 0.790 | 0.573 | 225 | stale_volatile 225 |
| semantic_only | 0.99 (target unmet) | 0.738 | 0.787 | 0.772 | 307 | stale_volatile 307 |
| semantic_plus_volatile | 0.86 | 0.619 | 0.951 | 0.781 | 60 | sibling_polarity 49, slot_value 10, stale_volatile 1 |
| guarded | 0.68 | 0.683 | 0.985 | 0.893 | 20 | sibling_polarity 18, stale_volatile 2 |

| ablation (guarded threshold) | dev precision | dev wrong | test precision | test recall | test wrong |
|---|---|---|---|---|---|
| without_literals | 0.863 | 91 | 0.903 | 0.832 | 132 |
| without_negation | 0.947 | 34 | 0.985 | 0.893 | 20 |
| without_polarity | 0.975 | 16 | 0.848 | 0.780 | 206 |
| without_terms | 0.836 | 110 | 0.842 | 0.785 | 218 |
| without_volatile | 0.729 | 235 | 0.776 | 0.893 | 380 |

| same threshold, test split | semantic_only precision / wrong | guarded precision / wrong |
|---|---|---|
| 0.68 | 0.528 / 849 | 0.985 / 20 |
| 0.70 | 0.519 / 860 | 0.986 / 19 |
| 0.80 | 0.672 / 530 | 0.994 / 7 |
| 0.90 | 0.765 / 354 | 0.993 / 8 |
