# Kalshi Alpha Lab - research loop report

Generated 2026-09-24T22:43:44+00:00

> Simulated/historical results under explicit fill, fee and latency assumptions. They are not evidence of future profitability. Out-of-sample means walk-forward folds never used for parameter selection; 'holdout' is a final set evaluated at most once.

## Data

```
{
  "markets": 27,
  "book_events": 9050,
  "trades": 0,
  "settlements": 26,
  "settlements_inferred": 26,
  "sources": [
    {
      "src": "import:crypto_sample_csv",
      "n": 25
    },
    {
      "src": "import:baseline_csv",
      "n": 2
    }
  ],
  "from": "2026-02-25T04:22:45.058297+00:00",
  "to": "2026-03-02T20:14:59.675114+00:00",
  "hours": 135.87
}
```

## Experiment results

| experiment | strategy | status | OOS trades | OOS net | OOS expectancy | holdout net | gating flags |
|---|---|---|---|---|---|---|---|
| loop:crypto15m_fair_value@import:crypto_sample_csv | crypto15m_fair_value | RESEARCH | 16 | $0.92 | $0.06 | not run | small_sample,concentrated_day,few_days,fails_fees_x2,parameter_sensitive,is_oos_degradation,walk_forward_inconsistent,low_deflated_sharpe,assumed_depth |
| loop:vol_fade@import:crypto_sample_csv | vol_fade | REJECT | 110 | -$25.11 | -$0.23 | not run | small_sample,few_days,dies_after_costs,parameter_sensitive,walk_forward_inconsistent,not_better_than_random,low_deflated_sharpe,assumed_depth |
| loop:vol_breakout@import:crypto_sample_csv | vol_breakout | REJECT | 101 | -$19.36 | -$0.19 | not run | small_sample,few_days,dies_after_costs,parameter_sensitive,walk_forward_inconsistent,not_better_than_random,low_deflated_sharpe,assumed_depth |
| loop:mean_reversion@import:crypto_sample_csv | mean_reversion | REJECT | 154 | -$39.86 | -$0.26 | not run | small_sample,few_days,parameter_sensitive,walk_forward_inconsistent,not_better_than_random,low_deflated_sharpe,assumed_depth |
| loop:momentum@import:crypto_sample_csv | momentum | REJECT | 154 | -$30.18 | -$0.20 | not run | small_sample,few_days,dies_after_costs,parameter_sensitive,walk_forward_inconsistent,not_better_than_random,low_deflated_sharpe,assumed_depth |
| loop:vol_fade@import:baseline_csv | vol_fade | REJECT | 9 | -$3.20 | -$0.36 | not run | small_sample,few_days,few_markets,parameter_sensitive,walk_forward_inconsistent,not_better_than_random,low_deflated_sharpe |
| loop:vol_breakout@import:baseline_csv | vol_breakout | REJECT | 14 | -$0.80 | -$0.06 | not run | small_sample,few_days,few_markets,dies_after_costs,walk_forward_inconsistent,low_deflated_sharpe |
| loop:mean_reversion@import:baseline_csv | mean_reversion | REJECT | 23 | -$6.27 | -$0.27 | not run | small_sample,few_days,few_markets,parameter_sensitive,walk_forward_inconsistent,not_better_than_random,low_deflated_sharpe |
| loop:momentum@import:baseline_csv | momentum | RESEARCH | 22 | $4.29 | $0.19 | not run | small_sample,concentrated_market,concentrated_day,few_days,few_markets,walk_forward_inconsistent,low_deflated_sharpe |
| loop:market_maker@import:baseline_csv | market_maker | REJECT | 7 | -$3.54 | -$0.51 | not run | small_sample,few_days,few_markets,parameter_sensitive,walk_forward_inconsistent,low_deflated_sharpe |
| loop:imbalance@import:baseline_csv | imbalance | REJECT | 26 | -$3.66 | -$0.14 | not run | small_sample,few_days,few_markets,dies_after_costs,parameter_sensitive,walk_forward_inconsistent,not_better_than_random,low_deflated_sharpe |

## Latest predictive-feature study

IC = Spearman rank correlation between the feature and the forward mid move, computed on non-overlapping samples. Cost hurdle = mean spread + 2 x taker fee (cents).

| study_id | feature | horizon_s | split | n | ic | ic_tstat | hit_rate | cost_hurdle | net_edge_top | net_edge_bottom |
|---|---|---|---|---|---|---|---|---|---|---|
| study-2f03a276 | accel | 3.0 | confirmation | 1359 | 0.0131 | 0.4824 | 0.5072 | 4.0463 | -4.2485 | -3.9268 |
| study-2f03a276 | accel | 3.0 | discovery | 2248 | -0.0346 | -1.6388 | 0.4261 | 4.0381 | -3.9348 | -4.1148 |
| study-2f03a276 | accel | 5.0 | confirmation | 903 | -0.0235 | -0.7068 | 0.4624 | 4.0615 | -3.4234 | -4.2963 |
| study-2f03a276 | accel | 5.0 | discovery | 1497 | -0.0304 | -1.177 | 0.4606 | 4.0513 | -3.8996 | -4.0796 |
| study-2f03a276 | accel | 10.0 | confirmation | 495 | -0.0459 | -1.021 | 0.4685 | 4.1041 | -2.7961 | -4.5991 |
| study-2f03a276 | accel | 10.0 | discovery | 816 | -0.047 | -1.3417 | 0.4415 | 4.0715 | -3.8567 | -4.1965 |
| study-2f03a276 | accel | 30.0 | confirmation | 172 | -0.212 | -2.8286 | 0.3672 | 4.2784 | 0.4216 | -6.3355 |
| study-2f03a276 | accel | 30.0 | discovery | 285 | -0.0062 | -0.1047 | 0.4739 | 4.152 | -3.5993 | -5.1607 |
| study-2f03a276 | accel | 60.0 | confirmation | 87 | -0.1246 | -1.1582 | 0.4286 | 4.3945 | 2.4111 | -7.4778 |
| study-2f03a276 | accel | 60.0 | discovery | 142 | 0.0295 | 0.3488 | 0.4583 | 4.2533 | -6.7188 | -2.5636 |
| study-2f03a276 | ret_10s | 3.0 | confirmation | 1403 | -0.0301 | -1.1275 |  | 4.2087 | -3.9739 | -4.2087 |
| study-2f03a276 | ret_10s | 3.0 | discovery | 2322 | 0.0005 | 0.0246 |  | 4.2386 | -4.2988 | -4.2558 |
| study-2f03a276 | ret_10s | 5.0 | confirmation | 933 | -0.0411 | -1.2539 |  | 4.2308 | -3.9554 | -4.2308 |
| study-2f03a276 | ret_10s | 5.0 | discovery | 1543 | -0.0002 | -0.0087 |  | 4.2461 | -4.1571 | -4.2072 |
| study-2f03a276 | ret_10s | 10.0 | confirmation | 511 | -0.0493 | -1.1135 |  | 4.2674 | -3.4438 | -4.2674 |
| study-2f03a276 | ret_10s | 10.0 | discovery | 844 | 0.0002 | 0.0063 |  | 4.2952 | -4.5555 | -4.3041 |
| study-2f03a276 | ret_10s | 30.0 | confirmation | 179 | 0.0892 | 1.1914 | 0.6719 | 4.3974 | -8.9113 | -0.3418 |
| study-2f03a276 | ret_10s | 30.0 | discovery | 295 | 0.0186 | 0.3183 | 0.5672 | 4.4443 | -4.5629 | -4.4273 |
| study-2f03a276 | ret_10s | 60.0 | confirmation | 89 | -0.0491 | -0.4588 | 0.6 | 4.7045 | 3.6566 | -7.4823 |
| study-2f03a276 | ret_10s | 60.0 | discovery | 145 | 0.0424 | 0.5073 | 0.6176 | 4.8332 | -5.6091 | -3.747 |
| study-2f03a276 | ret_1s | 3.0 | confirmation | 1425 | -0.0084 | -0.3186 |  | 4.2744 | -3.9972 | -4.3094 |
| study-2f03a276 | ret_1s | 3.0 | discovery | 2356 | 0.0002 | 0.0086 |  | 4.3227 | -4.3981 | -4.3333 |
| study-2f03a276 | ret_1s | 5.0 | confirmation | 948 | -0.014 | -0.432 |  | 4.2934 | -4.0065 | -4.7197 |
| study-2f03a276 | ret_1s | 5.0 | discovery | 1568 | -0.0 | -0.0004 |  | 4.3449 | -4.3194 | -4.3242 |
| study-2f03a276 | ret_1s | 10.0 | confirmation | 518 | -0.0269 | -0.6105 |  | 4.346 | -3.3219 | -4.995 |
| study-2f03a276 | ret_1s | 10.0 | discovery | 859 | 0.0 | 0.0 |  | 4.3994 | -4.3558 | -4.4227 |
| study-2f03a276 | ret_1s | 30.0 | confirmation | 181 | 0.0202 | 0.27 | 0.75 | 4.4807 | -7.7585 | -1.3996 |
| study-2f03a276 | ret_1s | 30.0 | discovery | 300 | 0.0235 | 0.4065 | 0.6 | 4.4622 | -3.8122 | -3.8539 |
| study-2f03a276 | ret_1s | 60.0 | discovery | 152 | 0.0534 | 0.6543 | 0.5714 | 4.7713 | -5.11 | -5.2552 |
| study-2f03a276 | ret_30s | 3.0 | confirmation | 1359 | -0.0347 | -1.2791 | 0.4928 | 4.0463 | -3.6272 | -4.2485 |
| study-2f03a276 | ret_30s | 3.0 | discovery | 2248 | 0.0392 | 1.8587 | 0.5739 | 4.0381 | -4.0926 | -3.9292 |
| study-2f03a276 | ret_30s | 5.0 | confirmation | 903 | 0.006 | 0.1788 | 0.5376 | 4.0615 | -4.719 | -3.4649 |
| study-2f03a276 | ret_30s | 5.0 | discovery | 1497 | 0.0346 | 1.3378 | 0.5394 | 4.0513 | -4.078 | -3.7246 |
| study-2f03a276 | ret_30s | 10.0 | confirmation | 495 | 0.0147 | 0.3266 | 0.5315 | 4.1041 | -5.786 | -2.8718 |
| study-2f03a276 | ret_30s | 10.0 | discovery | 816 | 0.0547 | 1.5636 | 0.5585 | 4.0715 | -4.1389 | -3.4587 |
| study-2f03a276 | ret_30s | 30.0 | confirmation | 172 | 0.0423 | 0.5525 | 0.5703 | 4.2784 | -9.5641 | 0.1645 |
| study-2f03a276 | ret_30s | 30.0 | discovery | 285 | 0.074 | 1.2487 | 0.5498 | 4.152 | -4.2748 | -2.5029 |
| study-2f03a276 | ret_30s | 60.0 | confirmation | 87 | 0.0003 | 0.0027 | 0.5584 | 4.3945 | -13.6722 | 2.4389 |
| study-2f03a276 | ret_30s | 60.0 | discovery | 142 | 0.0344 | 0.4069 | 0.5333 | 4.2533 | -4.9429 | -0.8395 |
| study-2f03a276 | ret_5s | 3.0 | confirmation | 1416 | -0.0138 | -0.518 |  | 4.2437 | -4.0087 | -4.293 |
| study-2f03a276 | ret_5s | 3.0 | discovery | 2341 | 0.0001 | 0.0072 |  | 4.2841 | -4.3172 | -4.3257 |
| study-2f03a276 | ret_5s | 5.0 | confirmation | 941 | -0.0273 | -0.8362 |  | 4.2676 | -3.9245 | -4.5189 |
| study-2f03a276 | ret_5s | 5.0 | discovery | 1557 | -0.0001 | -0.0034 |  | 4.2995 | -4.3171 | -4.1456 |
| study-2f03a276 | ret_5s | 10.0 | confirmation | 518 | -0.0396 | -0.901 |  | 4.3369 | -3.6494 | -4.486 |
| study-2f03a276 | ret_5s | 10.0 | discovery | 852 | 0.0002 | 0.0063 |  | 4.3381 | -4.2533 | -4.4726 |
| study-2f03a276 | ret_5s | 30.0 | confirmation | 181 | -0.0495 | -0.6635 | 0.619 | 4.4629 | -0.9907 | -6.517 |
| study-2f03a276 | ret_5s | 30.0 | discovery | 297 | 0.0171 | 0.2931 | 0.6296 | 4.4589 | -3.8089 | -4.8923 |
| study-2f03a276 | ret_5s | 60.0 | discovery | 151 | 0.0858 | 1.0517 | 1.0 | 4.7975 | -3.7808 | -5.4265 |
| study-2f03a276 | ret_60s | 3.0 | confirmation | 1293 | 0.0351 | 1.2623 | 0.589 | 3.894 | -4.1875 | -3.506 |
| study-2f03a276 | ret_60s | 3.0 | discovery | 2137 | 0.0303 | 1.4005 | 0.5207 | 4.0041 | -3.969 | -3.7681 |
| study-2f03a276 | ret_60s | 5.0 | confirmation | 860 | 0.0429 | 1.2573 | 0.5645 | 3.9181 | -4.8484 | -2.8658 |
| study-2f03a276 | ret_60s | 5.0 | discovery | 1422 | 0.0519 | 1.9574 | 0.5455 | 4.0104 | -3.9262 | -3.4473 |
| study-2f03a276 | ret_60s | 10.0 | confirmation | 470 | 0.0152 | 0.3298 | 0.5339 | 3.9554 | -5.5671 | -2.4448 |
| study-2f03a276 | ret_60s | 10.0 | discovery | 778 | 0.1012 | 2.8326 | 0.5736 | 4.0332 | -3.7352 | -2.9852 |
| study-2f03a276 | ret_60s | 30.0 | confirmation | 163 | 0.0418 | 0.5303 | 0.563 | 4.1302 | -8.7514 | 0.7031 |
| study-2f03a276 | ret_60s | 30.0 | discovery | 271 | 0.1415 | 2.3444 | 0.5566 | 4.0995 | -3.2013 | -1.254 |
| study-2f03a276 | ret_60s | 60.0 | confirmation | 82 | 0.0914 | 0.8211 | 0.5714 | 4.2599 | -12.1128 | 5.5342 |
| study-2f03a276 | ret_60s | 60.0 | discovery | 137 | 0.0805 | 0.9381 | 0.5242 | 4.166 | -5.7553 | -1.9695 |
| study-2f03a276 | spot_ret_10s | 3.0 | confirmation | 1428 | 0.0444 | 1.6779 | 0.675 | 4.2998 | -4.2701 | -4.083 |
| study-2f03a276 | spot_ret_10s | 3.0 | discovery | 2352 | 0.0975 | 4.748 | 0.7671 | 4.3505 | -4.0363 | -4.1403 |
| study-2f03a276 | spot_ret_10s | 5.0 | confirmation | 950 | 0.0639 | 1.9701 | 0.6481 | 4.3133 | -4.2659 | -3.7528 |
| study-2f03a276 | spot_ret_10s | 5.0 | discovery | 1564 | 0.1105 | 4.3951 | 0.7037 | 4.3496 | -3.9486 | -4.0461 |
| study-2f03a276 | spot_ret_10s | 10.0 | confirmation | 520 | 0.0771 | 1.7593 | 0.6129 | 4.3509 | -4.2548 | -3.2884 |
| study-2f03a276 | spot_ret_10s | 10.0 | discovery | 855 | 0.1286 | 3.7864 | 0.6566 | 4.3942 | -3.365 | -3.0959 |
| study-2f03a276 | spot_ret_10s | 30.0 | confirmation | 181 | 0.1299 | 1.7522 | 0.5625 | 4.5178 | -5.9483 | -0.1259 |
| study-2f03a276 | spot_ret_10s | 30.0 | discovery | 298 | 0.0956 | 1.6521 | 0.5776 | 4.4716 | -4.305 | -3.2966 |
| study-2f03a276 | spot_ret_10s | 60.0 | confirmation | 91 | 0.1666 | 1.5945 | 0.625 | 4.738 | -8.238 | 3.2094 |
| study-2f03a276 | spot_ret_10s | 60.0 | discovery | 150 | 0.0842 | 1.0279 | 0.5593 | 4.7987 | -3.4154 | -3.3154 |
| study-2f03a276 | spot_ret_60s | 3.0 | confirmation | 1428 | 0.0301 | 1.1381 | 0.5833 | 4.2998 | -4.4799 | -3.9886 |
| study-2f03a276 | spot_ret_60s | 3.0 | discovery | 2302 | 0.0383 | 1.8363 | 0.5746 | 4.3447 | -4.1744 | -4.2005 |
| study-2f03a276 | spot_ret_60s | 5.0 | confirmation | 950 | 0.0908 | 2.8058 | 0.6607 | 4.3133 | -4.6133 | -3.5817 |
| study-2f03a276 | spot_ret_60s | 5.0 | discovery | 1532 | 0.0814 | 3.1963 | 0.6141 | 4.3356 | -4.2753 | -3.8796 |
| study-2f03a276 | spot_ret_60s | 10.0 | confirmation | 520 | 0.055 | 1.2542 | 0.5615 | 4.3509 | -4.8317 | -2.8221 |
| study-2f03a276 | spot_ret_60s | 10.0 | discovery | 839 | 0.1184 | 3.4483 | 0.6176 | 4.3809 | -4.0475 | -3.4463 |
| study-2f03a276 | spot_ret_60s | 30.0 | confirmation | 181 | 0.0222 | 0.2974 | 0.5448 | 4.5178 | -7.0733 | -0.8286 |
| study-2f03a276 | spot_ret_60s | 30.0 | discovery | 292 | 0.1147 | 1.9665 | 0.5561 | 4.4343 | -3.7309 | -1.9936 |
| study-2f03a276 | spot_ret_60s | 60.0 | confirmation | 91 | 0.0205 | 0.193 | 0.5844 | 4.738 | -8.8769 | 1.4199 |
| study-2f03a276 | spot_ret_60s | 60.0 | discovery | 148 | 0.1282 | 1.5621 | 0.5484 | 4.7476 | -5.1643 | -0.8976 |
| study-2f03a276 | spread | 3.0 | confirmation | 1428 | 0.0253 | 0.9549 | 0.2706 | 4.2998 | -4.3855 | -4.0603 |
| study-2f03a276 | spread | 3.0 | discovery | 2364 | -0.0129 | -0.6283 | 0.5286 | 4.3582 | -4.2419 | -4.317 |
| study-2f03a276 | spread | 5.0 | confirmation | 950 | 0.0237 | 0.7293 | 0.2895 | 4.3133 | -4.8633 | -3.9765 |
| study-2f03a276 | spread | 5.0 | discovery | 1572 | -0.0271 | -1.0741 | 0.5228 | 4.3573 | -4.0255 | -4.3986 |
| study-2f03a276 | spread | 10.0 | confirmation | 520 | 0.0343 | 0.7815 | 0.3258 | 4.3509 | -5.3076 | -3.5961 |
| study-2f03a276 | spread | 10.0 | discovery | 859 | -0.0263 | -0.7693 | 0.4907 | 4.401 | -3.6946 | -4.465 |
| study-2f03a276 | spread | 30.0 | confirmation | 181 | -0.0112 | -0.1496 | 0.3 | 4.5178 | -1.7956 | -6.5718 |
| study-2f03a276 | spread | 30.0 | discovery | 300 | -0.0279 | -0.4826 | 0.5063 | 4.4622 | -2.9622 | -4.9456 |
| study-2f03a276 | spread | 60.0 | confirmation | 91 | 0.1341 | 1.2766 | 0.3068 | 4.738 | -9.238 | 3.7883 |
| study-2f03a276 | spread | 60.0 | discovery | 152 | 0.1417 | 1.7529 | 0.5315 | 4.7769 | -3.1479 | -2.7931 |
| study-2f03a276 | strike_z | 3.0 | confirmation | 1420 | 0.0039 | 0.1456 | 0.6265 | 4.282 | -4.5549 | -4.238 |
| study-2f03a276 | strike_z | 3.0 | discovery | 2334 | 0.059 | 2.8553 | 0.6143 | 4.3243 | -4.2932 | -4.1658 |
| study-2f03a276 | strike_z | 5.0 | confirmation | 946 | 0.0337 | 1.0347 | 0.6374 | 4.2908 | -4.5844 | -4.196 |
| study-2f03a276 | strike_z | 5.0 | discovery | 1554 | 0.0997 | 3.9454 | 0.6082 | 4.319 | -4.2804 | -4.0747 |
| study-2f03a276 | strike_z | 10.0 | confirmation | 518 | 0.0456 | 1.0374 | 0.6231 | 4.3389 | -4.8485 | -4.1418 |
| study-2f03a276 | strike_z | 10.0 | discovery | 850 | 0.1652 | 4.8783 | 0.6435 | 4.373 | -4.2083 | -3.3024 |
| study-2f03a276 | strike_z | 30.0 | confirmation | 181 | -0.0212 | -0.2836 | 0.6027 | 4.5178 | -2.3372 | -4.8691 |
| study-2f03a276 | strike_z | 30.0 | discovery | 296 | 0.202 | 3.536 | 0.6266 | 4.4532 | -4.6142 | -2.2865 |
| study-2f03a276 | strike_z | 60.0 | confirmation | 91 | -0.0298 | -0.2812 | 0.6628 | 4.738 | 1.1231 | -6.7906 |
| study-2f03a276 | strike_z | 60.0 | discovery | 149 | 0.1964 | 2.4285 | 0.6594 | 4.8004 | -5.8504 | -1.6504 |
| study-2f03a276 | tts_s | 3.0 | confirmation | 1428 | 0.0247 | 0.9333 | 0.2706 | 4.2998 | -4.5043 | -4.1442 |
| study-2f03a276 | tts_s | 3.0 | discovery | 2364 | -0.0285 | -1.3834 | 0.5286 | 4.3582 | -4.4089 | -4.3793 |
| study-2f03a276 | tts_s | 5.0 | confirmation | 950 | 0.0273 | 0.8402 | 0.2895 | 4.3133 | -4.4449 | -4.0765 |
| study-2f03a276 | tts_s | 5.0 | discovery | 1572 | -0.0141 | -0.5589 | 0.5228 | 4.3573 | -4.4192 | -4.6113 |
| study-2f03a276 | tts_s | 10.0 | confirmation | 520 | 0.0402 | 0.9159 | 0.3258 | 4.3509 | -4.6394 | -4.0817 |
| study-2f03a276 | tts_s | 10.0 | discovery | 859 | -0.0638 | -1.8701 | 0.4907 | 4.401 | -3.9068 | -4.8342 |
| study-2f03a276 | tts_s | 30.0 | confirmation | 181 | 0.0826 | 1.1085 | 0.3 | 4.5178 | -5.2678 | -2.9908 |
| study-2f03a276 | tts_s | 30.0 | discovery | 300 | -0.0414 | -0.7151 | 0.5063 | 4.4622 | -4.2539 | -5.7289 |
| study-2f03a276 | tts_s | 60.0 | confirmation | 91 | 0.0531 | 0.5013 | 0.3068 | 4.738 | -7.6824 | -1.6327 |
| study-2f03a276 | tts_s | 60.0 | discovery | 152 | -0.0772 | -0.9483 | 0.5315 | 4.7769 | -4.1802 | -5.7286 |
| study-2f03a276 | vol_10s | 3.0 | confirmation | 1428 | 0.0757 | 2.8685 |  | 4.2998 | -4.2998 | -3.9134 |
| study-2f03a276 | vol_10s | 3.0 | discovery | 2364 | -0.0095 | -0.4635 |  | 4.3582 | -4.3582 | -4.2535 |
| study-2f03a276 | vol_10s | 5.0 | confirmation | 950 | 0.1017 | 3.1485 |  | 4.3133 | -4.3133 | -3.8422 |
| study-2f03a276 | vol_10s | 5.0 | discovery | 1572 | -0.0111 | -0.4389 |  | 4.3573 | -4.3573 | -4.3017 |
| study-2f03a276 | vol_10s | 10.0 | confirmation | 520 | 0.1263 | 2.8975 |  | 4.3509 | -4.3509 | -3.4038 |
| study-2f03a276 | vol_10s | 10.0 | discovery | 859 | 0.0068 | 0.1988 |  | 4.401 | -4.401 | -4.2324 |
| study-2f03a276 | vol_10s | 30.0 | confirmation | 181 | 0.2047 | 2.7978 | 0.3846 | 4.5178 | -5.0872 | -0.4367 |
| study-2f03a276 | vol_10s | 30.0 | discovery | 300 | -0.1034 | -1.7953 | 0.4318 | 4.4622 | -3.1372 | -5.3789 |
| study-2f03a276 | vol_10s | 60.0 | confirmation | 91 | -0.0762 | -0.7214 | 0.2333 | 4.738 | 0.1509 | -10.3959 |
| study-2f03a276 | vol_10s | 60.0 | discovery | 152 | -0.094 | -1.1565 | 0.4615 | 4.7769 | -3.2124 | -3.2931 |
| study-2f03a276 | vol_60s | 3.0 | confirmation | 1428 | -0.0513 | -1.9397 | 0.25 | 4.2998 | -3.8942 | -4.2998 |
| study-2f03a276 | vol_60s | 3.0 | discovery | 2364 | -0.004 | -0.1959 | 0.5276 | 4.3582 | -4.3793 | -4.2937 |
| study-2f03a276 | vol_60s | 5.0 | confirmation | 950 | -0.1076 | -3.3314 | 0.2895 | 4.3133 | -3.4922 | -4.187 |
| study-2f03a276 | vol_60s | 5.0 | discovery | 1572 | -0.0303 | -1.2021 | 0.5079 | 4.3573 | -3.9446 | -4.189 |
| study-2f03a276 | vol_60s | 10.0 | confirmation | 520 | -0.1334 | -3.0638 | 0.328 | 4.3509 | -2.6826 | -4.1874 |
| study-2f03a276 | vol_60s | 10.0 | discovery | 859 | -0.0749 | -2.1994 | 0.4804 | 4.401 | -3.4679 | -4.0725 |
| study-2f03a276 | vol_60s | 30.0 | confirmation | 181 | -0.3256 | -4.6076 | 0.2966 | 4.5178 | 0.9683 | -3.7205 |
| study-2f03a276 | vol_60s | 30.0 | discovery | 300 | -0.0928 | -1.6088 | 0.4957 | 4.4622 | -2.7039 | -3.4122 |
| study-2f03a276 | vol_60s | 60.0 | confirmation | 91 | -0.3373 | -3.3796 | 0.2532 | 4.738 | 5.1231 | -4.3959 |
| study-2f03a276 | vol_60s | 60.0 | discovery | 152 | -0.1583 | -1.9639 | 0.5197 | 4.7769 | -3.7124 | -2.2931 |

## Research-loop summary

```
{
  "data_groups": {
    "import:baseline_csv": 2,
    "import:crypto_sample_csv": 25
  },
  "feature_study": {
    "import:baseline_csv": {
      "rows": 862,
      "pairs_tested": 114,
      "surviving_pairs": [],
      "split": "time split within markets (few markets); source=import:baseline_csv"
    },
    "import:crypto_sample_csv": {
      "rows": 7618,
      "pairs_tested": 114,
      "surviving_pairs": [],
      "split": "market split 15/10; source=import:crypto_sample_csv"
    }
  },
  "crypto_incremental_info": {
    "import:crypto_sample_csv": {
      "strike_z": {
        "ok": true,
        "n_train_markets": 14,
        "n_test_markets": 10,
        "n_test_samples": 219,
        "baseline_logloss": 0.7001879544877291,
        "model_logloss": 0.7342383940659297,
        "baseline_brier": 0.22133962457617565,
        "model_brier": 0.22937286171527155,
        "mean_improvement_per_market": -0.029258737877970474,
        "t_stat_markets": -1.9087066884828776,
        "features": [
          "strike_z"
        ],
        "verdict": "no demonstrated incremental information"
      },
      "strike_z+spot_ret_60s": {
        "ok": true,
        "n_train_markets": 14,
        "n_test_markets": 10,
        "n_test_samples": 219,
        "baseline_logloss": 0.6872402474243374,
        "model_logloss": 0.7457461714591624,
        "baseline_brier": 0.2170731910480645,
        "model_brier": 0.2288535104697611,
        "mean_improvement_per_market": -0.05006361564431483,
        "t_stat_markets": -2.174667014072794,
        "features": [
          "strike_z",
          "spot_ret_60s"
        ],
        "verdict": "no demonstrated incremental information"
      }
    }
  },
  "arb_scan": {
    "crossed_books": 0,
    "events_scanned": 0,
    "candidates": {},
    "note": "net = gross edge minus taker fees on every leg, per 1-contract set, in cents; 'executable' counts episodes that persisted longer than the order latency"
  },
  "experiments": {
    "imbalance@import:baseline_csv": {
      "experiment_id": "exp-79cb0eb766",
      "classification": "REJECT",
      "reason": "out-of-sample net P&L -3.66 <= 0",
      "oos_net": -3.6599999999999993,
      "oos_mid_pnl": 0.40500000000000114,
      "oos_trades": 26,
      "gating_flags": [
        "small_sample",
        "few_days",
        "few_markets",
        "dies_after_costs",
        "parameter_sensitive",
        "walk_forward_inconsistent",
        "not_better_than_random",
        "low_deflated_sharpe"
      ]
    },
    "market_maker@import:baseline_csv": {
      "experiment_id": "exp-c0ae7edfdf",
      "classification": "REJECT",
      "reason": "out-of-sample net P&L -3.54 <= 0",
      "oos_net": -3.5399999999999996,
      "oos_mid_pnl": -1.8999999999999995,
      "oos_trades": 7,
      "gating_flags": [
        "small_sample",
        "few_days",
        "few_markets",
        "parameter_sensitive",
        "walk_forward_inconsistent",
        "low_deflated_sharpe"
      ]
    },
    "momentum@import:baseline_csv": {
      "experiment_id": "exp-a375c4f216",
      "classification": "RESEARCH",
      "reason": "positive but only 22 out-of-sample trades",
      "oos_net": 4.290000000000004,
      "oos_mid_pnl": 8.085000000000004,
      "oos_trades": 22,
      "gating_flags": [
        "small_sample",
        "concentrated_market",
        "concentrated_day",
        "few_days",
        "few_markets",
        "walk_forward_inconsistent",
        "low_deflated_sharpe"
      ]
    },
    "mean_reversion@import:baseline_csv": {
      "experiment_id": "exp-e7cdfc1f7a",
      "classification": "REJECT",
      "reason": "out-of-sample net P&L -6.27 <= 0",
      "oos_net": -6.269999999999996,
      "oos_mid_pnl": -2.599999999999996,
      "oos_trades": 23,
      "gating_flags": [
        "small_sample",
        "few_days",
        "few_markets",
        "parameter_sensitive",
        "walk_forward_inconsistent",
        "not_better_than_random",
        "low_deflated_sharpe"
      ]
    },
    "vol_breakout@import:baseline_csv": {
      "experiment_id": "exp-8ee4c50aac",
      "classification": "REJECT",
      "reason": "out-of-sample net P&L -0.80 <= 0",
      "oos_net": -0.800000000000002,
      "oos_mid_pnl": 1.7999999999999983,
      "oos_trades": 14,
      "gating_flags": [
        "small_sample",
        "few_days",
        "few_markets",
        "dies_after_costs",
        "walk_forward_inconsistent",
        "low_deflated_sharpe"
      ]
    },
    "vol_fade@import:baseline_csv": {
      "experiment_id": "exp-4bb6b374b7",
      "classification": "REJECT",
      "reason": "out-of-sample net P&L -3.20 <= 0",
      "oos_net": -3.200000000000003,
      "oos_mid_pnl": -1.5800000000000023,
      "oos_trades": 9,
      "gating_flags": [
        "small_sample",
        "few_days",
        "few_markets",
        "parameter_sensitive",
        "walk_forward_inconsistent",
        "not_better_than_random",
        "low_deflated_sharpe"
      ]
    },
    "crypto15m_fair_value@import:baseline_csv": {
      "skipped": "no markets with strike + external reference price"
    },
    "logical_arb@import:baseline_csv": {
      "skipped": "no events with >= 2 related markets"
    },
    "imbalance@import:crypto_sample_csv": {
      "skipped": "depth sizes not recorded in this data (assumed), so imbalance/queue dynamics are untestable"
    },
    "market_maker@import:crypto_sample_csv": {
      "skipped": "depth sizes not recorded in this data (assumed), so imbalance/queue dynamics are untestable"
    },
    "momentum@import:crypto_sample_csv": {
      "experiment_id": "exp-7eefb4e637",
      "classification": "REJECT",
      "reason": "out-of-sample net P&L -30.18 <= 0",
      "oos_net": -30.17999999999997,
      "oos_mid_pnl": 4.825000000000056,
      "oos_trades": 154,
      "gating_flags": [
        "small_sample",
        "few_days",
        "dies_after_costs",
        "parameter_sensitive",
        "walk_forward_inconsistent",
        "not_better_than_random",
        "low_deflated_sharpe",
        "assumed_depth"
      ]
    },
    "mean_reversion@import:crypto_sample_csv": {
      "experiment_id": "exp-a96853abce",
      "classification": "REJECT",
      "reason": "out-of-sample net P&L -39.86 <= 0",
      "oos_net": -39.86,
      "oos_mid_pnl": -4.824999999999974,
      "oos_trades": 154,
      "gating_flags": [
        "small_sample",
        "few_days",
        "parameter_sensitive",
        "walk_forward_inconsistent",
        "not_better_than_random",
        "low_deflated_sharpe",
        "assumed_depth"
      ]
    },
    "vol_breakout@import:crypto_sample_csv": {
      "experiment_id": "exp-2fd5aa6e0e",
      "classification": "REJECT",
      "reason": "out-of-sample net P&L -19.36 <= 0",
      "oos_net": -19.359999999999985,
      "oos_mid_pnl": 3.500000000000023,
      "oos_trades": 101,
      "gating_flags": [
        "small_sample",
        "few_days",
        "dies_after_costs",
        "parameter_sensitive",
        "walk_forward_inconsistent",
        "not_better_than_random",
        "low_deflated_sharpe",
        "assumed_depth"
      ]
    },
    "vol_fade@import:crypto_sample_csv": {
      "experiment_id": "exp-ca1b9d35da",
      "classification": "REJECT",
      "reason": "out-of-sample net P&L -25.11 <= 0",
      "oos_net": -25.10999999999999,
      "oos_mid_pnl": 1.9539925233402755e-14,
      "oos_trades": 110,
      "gating_flags": [
        "small_sample",
        "few_days",
        "dies_after_costs",
        "parameter_sensitive",
        "walk_forward_inconsistent",
        "not_better_than_random",
        "low_deflated_sharpe",
        "assumed_depth"
      ]
    },
    "crypto15m_fair_value@import:crypto_sample_csv": {
      "experiment_id": "exp-de9e505d9f",
      "classification": "RESEARCH",
      "reason": "positive but only 16 out-of-sample trades",
      "oos_net": 0.9200000000000034,
      "oos_mid_pnl": 3.0250000000000035,
      "oos_trades": 16,
      "gating_flags": [
        "small_sample",
        "concentrated_day",
        "few_days",
        "fails_fees_x2",
        "parameter_sensitive",
        "is_oos_degradation",
        "walk_forward_inconsistent",
        "low_deflated_sharpe",
        "assumed_depth"
      ]
    },
    "logical_arb@import:crypto_sample_csv": {
      "skipped": "no events with >= 2 related markets"
    }
  },
  "paper_candidates": []
}
```

## Feature study - import:baseline_csv (confirmation set, 15 strongest |t|)

| feature | horizon_s | n | ic | ic_tstat | top_bucket_move | bottom_bucket_move | cost_hurdle | net_edge_top | net_edge_bottom |
|---|---|---|---|---|---|---|---|---|---|
| micro_minus_mid | 3.0 | 147 | 0.4 | 5.2552 | 0.6 | -0.9667 | 2.7547 | -2.1547 | -1.788 |
| imbalance_1 | 3.0 | 147 | 0.2944 | 3.7094 | 0.4833 | -0.9167 | 2.7547 | -2.2714 | -1.838 |
| spread | 5.0 | 93 | -0.3431 | -3.4846 | -1.8158 | 0.0 | 2.8473 | -1.0315 | -2.8473 |
| vol_60s | 5.0 | 93 | -0.3144 | -3.159 | 0.0789 | 0.1316 | 2.8473 | -2.9263 | -2.7158 |
| accel | 3.0 | 145 | 0.2479 | 3.0605 | 0.3276 | -1.1207 | 2.7751 | -2.4475 | -1.6544 |
| vol_60s | 10.0 | 49 | -0.3802 | -2.8185 | -0.05 | 0.3 | 2.764 | -2.714 | -2.464 |
| vol_10s | 5.0 | 93 | -0.2499 | -2.4621 | -0.0263 | 0.0263 | 2.8473 | -2.821 | -2.821 |
| spread | 10.0 | 49 | -0.3243 | -2.3501 | -2.85 | 0.05 | 2.764 | 0.086 | -2.714 |
| vol_10s | 10.0 | 49 | -0.3242 | -2.3494 | -1.15 | 0.2 | 2.764 | -1.614 | -2.564 |
| imbalance | 3.0 | 147 | -0.1906 | -2.3378 | -1.0333 | -0.0167 | 2.7547 | -1.7214 | -2.7714 |
| imbalance_1 | 1.0 | 343 | 0.1248 | 2.3222 | 0.0362 | -0.2101 | 2.8027 | -2.7664 | -2.5925 |
| tts_s | 5.0 | 93 | -0.2343 | -2.2993 | 0.1579 | 0.1316 | 2.8473 | -3.0052 | -2.7158 |
| ret_5s | 5.0 | 92 | 0.2273 | 2.2144 | 0.3947 | -0.5263 | 2.8652 | -2.4704 | -2.3388 |
| ret_1s | 5.0 | 93 | 0.2193 | 2.1437 | 1.1316 | -1.2368 | 2.8473 | -1.7158 | -1.6105 |
| micro_minus_mid | 5.0 | 93 | 0.2072 | 2.0208 | 1.3158 | -1.3421 | 2.8473 | -1.5315 | -1.5052 |

## Feature study - import:crypto_sample_csv (confirmation set, 15 strongest |t|)

| feature | horizon_s | n | ic | ic_tstat | top_bucket_move | bottom_bucket_move | cost_hurdle | net_edge_top | net_edge_bottom |
|---|---|---|---|---|---|---|---|---|---|
| vol_60s | 30.0 | 181 | -0.3256 | -4.6076 | -5.4861 | 0.7973 | 4.5178 | 0.9683 | -3.7205 |
| vol_60s | 60.0 | 91 | -0.3373 | -3.3796 | -9.8611 | 0.3421 | 4.738 | 5.1231 | -4.3959 |
| vol_60s | 5.0 | 950 | -0.1076 | -3.3314 | -0.8211 | 0.1263 | 4.3133 | -3.4922 | -4.187 |
| vol_10s | 5.0 | 950 | 0.1017 | 3.1485 | 0.0 | -0.4711 | 4.3133 | -4.3133 | -3.8422 |
| vol_60s | 10.0 | 520 | -0.1334 | -3.0638 | -1.6683 | 0.1635 | 4.3509 | -2.6826 | -4.1874 |
| vol_10s | 10.0 | 520 | 0.1263 | 2.8975 | 0.0 | -0.9471 | 4.3509 | -4.3509 | -3.4038 |
| vol_10s | 3.0 | 1428 | 0.0757 | 2.8685 | 0.0 | -0.3864 | 4.2998 | -4.2998 | -3.9134 |
| accel | 30.0 | 172 | -0.212 | -2.8286 | -4.7 | -2.0571 | 4.2784 | 0.4216 | -6.3355 |
| spot_ret_60s | 5.0 | 950 | 0.0908 | 2.8058 | -0.3 | -0.7316 | 4.3133 | -4.6133 | -3.5817 |
| vol_10s | 30.0 | 181 | 0.2047 | 2.7978 | -0.5694 | -4.0811 | 4.5178 | -5.0872 | -0.4367 |
| spot_ret_10s | 5.0 | 950 | 0.0639 | 1.9701 | 0.0474 | -0.5605 | 4.3133 | -4.2659 | -3.7528 |
| vol_60s | 3.0 | 1428 | -0.0513 | -1.9397 | -0.4056 | 0.0 | 4.2998 | -3.8942 | -4.2998 |
| spot_ret_10s | 10.0 | 520 | 0.0771 | 1.7593 | 0.0962 | -1.0625 | 4.3509 | -4.2548 | -3.2884 |
| spot_ret_10s | 30.0 | 181 | 0.1299 | 1.7522 | -1.4306 | -4.3919 | 4.5178 | -5.9483 | -0.1259 |
| spot_ret_10s | 3.0 | 1428 | 0.0444 | 1.6779 | 0.0297 | -0.2168 | 4.2998 | -4.2701 | -4.083 |

## Crypto incremental-information tests

```
{
  "import:crypto_sample_csv": {
    "strike_z": {
      "ok": true,
      "n_train_markets": 14,
      "n_test_markets": 10,
      "n_test_samples": 219,
      "baseline_logloss": 0.7001879544877291,
      "model_logloss": 0.7342383940659297,
      "baseline_brier": 0.22133962457617565,
      "model_brier": 0.22937286171527155,
      "mean_improvement_per_market": -0.029258737877970474,
      "t_stat_markets": -1.9087066884828776,
      "features": [
        "strike_z"
      ],
      "verdict": "no demonstrated incremental information"
    },
    "strike_z+spot_ret_60s": {
      "ok": true,
      "n_train_markets": 14,
      "n_test_markets": 10,
      "n_test_samples": 219,
      "baseline_logloss": 0.6872402474243374,
      "model_logloss": 0.7457461714591624,
      "baseline_brier": 0.2170731910480645,
      "model_brier": 0.2288535104697611,
      "mean_improvement_per_market": -0.05006361564431483,
      "t_stat_markets": -2.174667014072794,
      "features": [
        "strike_z",
        "spot_ret_60s"
      ],
      "verdict": "no demonstrated incremental information"
    }
  }
}
```

## Logical-arbitrage scan

```
{
  "crossed_books": 0,
  "events_scanned": 0,
  "candidates": {},
  "note": "net = gross edge minus taker fees on every leg, per 1-contract set, in cents; 'executable' counts episodes that persisted longer than the order latency"
}
```

## Scorecards

```
Strategy: crypto15m_fair_value   (experiment exp-de9e505d9f: loop:crypto15m_fair_value@import:crypto_sample_csv)
Markets: KXBTC15M, KXETH15M, KXSOL15M, KXXRP15M
Selected parameters (chosen on development data only): {"edge_c": 8.0, "min_tts_s": 60.0, "series_prefix": "KX"}
Grid points tried: 6

Walk-forward OUT-OF-SAMPLE (never used for selection):
  Sample:               16 trades / 16 fills over 16 markets, 1 days
  P&L at mid:           $3.03
  Gross P&L:            $2.35
  Fees:                 $1.43
  Estimated slippage:   $0.68  (taker cost vs mid, already inside gross)
  Net P&L:              $0.92
  Expectancy / trade:   $0.06
  Win rate:             50.0%
  Profit factor:        1.05
  Max drawdown:         $7.13
  Sharpe (per trade):   0.02   Sharpe (daily, ann.): n/a
  Fill rate:            100.0%
  Adverse selection:    -3.28 c/contract (30s markout, + = adverse)
  Avg holding:          829.0 s

In-sample expectancy:   $0.57 on 16 trades
Out-of-sample expect.:  $0.06
Holdout test:           not evaluated (candidate failed earlier gates)
Cost/latency stress (OOS, net P&L): conservative_queue=$0.92, fees_x2=-$0.51, latency_plus_250ms=$0.92, slippage_plus_1tick=$0.12
Random-direction benchmark: mean net -$3.87 over 200 trials; p-value 0.005
Probabilistic Sharpe (deflated for 6 trials): 0.212
Flags:
  [GATING] small_sample: out-of-sample trades 16 < 300
  [GATING] concentrated_day: 100% of positive P&L from one day
  [GATING] few_days: only 1 distinct trading days out of sample
  [GATING] fails_fees_x2: net P&L -0.51 under fees x2
  [GATING] parameter_sensitive: only 0% of neighbouring parameter sets are profitable
  [GATING] is_oos_degradation: OOS expectancy 0.0575 < 50% of IS 0.5738
  [GATING] walk_forward_inconsistent: 25% of walk-forward folds profitable
  [GATING] low_deflated_sharpe: probabilistic Sharpe vs multiple-testing benchmark 0.21 < 0.95
  [info] inferred_settlements: 24 settlements inferred, not official
  [GATING] assumed_depth: some markets have assumed (not recorded) depth

Status: RESEARCH  -  positive but only 16 out-of-sample trades

Simulated/historical results under explicit fill, fee and latency assumptions. They are not evidence of future profitability. Out-of-sample means walk-forward folds never used for parameter selection; 'holdout' is a final set evaluated at most once.
```

```
Strategy: vol_fade   (experiment exp-ca1b9d35da: loop:vol_fade@import:crypto_sample_csv)
Markets: KXBTC15M, KXETH15M, KXSOL15M, KXXRP15M
Selected parameters (chosen on development data only): {"horizon_s": 10, "ratio": 0.8}
Grid points tried: 4

Walk-forward OUT-OF-SAMPLE (never used for selection):
  Sample:               110 trades / 220 fills over 16 markets, 1 days
  P&L at mid:           $0.00
  Gross P&L:            -$9.15
  Fees:                 $15.96
  Estimated slippage:   $9.15  (taker cost vs mid, already inside gross)
  Net P&L:              -$25.11
  Expectancy / trade:   -$0.23
  Win rate:             0.0%
  Profit factor:        0.00
  Max drawdown:         $25.11
  Sharpe (per trade):   -3.79   Sharpe (daily, ann.): n/a
  Fill rate:            100.0%
  Adverse selection:    -0.07 c/contract (30s markout, + = adverse)
  Avg holding:          10.1 s

In-sample expectancy:   -$0.22 on 98 trades
Out-of-sample expect.:  -$0.23
Holdout test:           not evaluated (candidate failed earlier gates)
Cost/latency stress (OOS, net P&L): conservative_queue=-$25.11, fees_x2=-$41.07, latency_plus_250ms=-$25.11, slippage_plus_1tick=-$36.16
Random-direction benchmark: mean net -$25.11 over 200 trials; p-value 0.985
Probabilistic Sharpe (deflated for 4 trials): 0.000
Flags:
  [GATING] small_sample: out-of-sample trades 110 < 300
  [GATING] few_days: only 1 distinct trading days out of sample
  [GATING] dies_after_costs: positive before costs (mid 0.00, gross -9.15) but net -25.11
  [GATING] parameter_sensitive: only 0% of neighbouring parameter sets are profitable
  [GATING] walk_forward_inconsistent: 0% of walk-forward folds profitable
  [GATING] not_better_than_random: p-value vs random-direction benchmark 0.985
  [GATING] low_deflated_sharpe: probabilistic Sharpe vs multiple-testing benchmark 0.00 < 0.95
  [info] inferred_settlements: 24 settlements inferred, not official
  [GATING] assumed_depth: some markets have assumed (not recorded) depth

Status: REJECT  -  out-of-sample net P&L -25.11 <= 0

Simulated/historical results under explicit fill, fee and latency assumptions. They are not evidence of future profitability. Out-of-sample means walk-forward folds never used for parameter selection; 'holdout' is a final set evaluated at most once.
```

```
Strategy: vol_breakout   (experiment exp-2fd5aa6e0e: loop:vol_breakout@import:crypto_sample_csv)
Markets: KXBTC15M, KXETH15M, KXSOL15M, KXXRP15M
Selected parameters (chosen on development data only): {"horizon_s": 30, "ratio": 0.8}
Grid points tried: 4

Walk-forward OUT-OF-SAMPLE (never used for selection):
  Sample:               101 trades / 202 fills over 16 markets, 1 days
  P&L at mid:           $3.50
  Gross P&L:            -$5.05
  Fees:                 $14.31
  Estimated slippage:   $8.55  (taker cost vs mid, already inside gross)
  Net P&L:              -$19.36
  Expectancy / trade:   -$0.19
  Win rate:             28.7%
  Profit factor:        0.26
  Max drawdown:         $19.97
  Sharpe (per trade):   -0.51   Sharpe (daily, ann.): n/a
  Fill rate:            100.0%
  Adverse selection:    0.40 c/contract (30s markout, + = adverse)
  Avg holding:          30.2 s

In-sample expectancy:   -$0.19 on 91 trades
Out-of-sample expect.:  -$0.19
Holdout test:           not evaluated (candidate failed earlier gates)
Cost/latency stress (OOS, net P&L): conservative_queue=-$19.36, fees_x2=-$33.67, latency_plus_250ms=-$19.36, slippage_plus_1tick=-$29.44
Random-direction benchmark: mean net -$23.08 over 200 trials; p-value 0.219
Probabilistic Sharpe (deflated for 4 trials): 0.000
Flags:
  [GATING] small_sample: out-of-sample trades 101 < 300
  [GATING] few_days: only 1 distinct trading days out of sample
  [GATING] dies_after_costs: positive before costs (mid 3.50, gross -5.05) but net -19.36
  [GATING] parameter_sensitive: only 0% of neighbouring parameter sets are profitable
  [GATING] walk_forward_inconsistent: 0% of walk-forward folds profitable
  [GATING] not_better_than_random: p-value vs random-direction benchmark 0.219
  [GATING] low_deflated_sharpe: probabilistic Sharpe vs multiple-testing benchmark 0.00 < 0.95
  [info] inferred_settlements: 24 settlements inferred, not official
  [GATING] assumed_depth: some markets have assumed (not recorded) depth

Status: REJECT  -  out-of-sample net P&L -19.36 <= 0

Simulated/historical results under explicit fill, fee and latency assumptions. They are not evidence of future profitability. Out-of-sample means walk-forward folds never used for parameter selection; 'holdout' is a final set evaluated at most once.
```

```
Strategy: mean_reversion   (experiment exp-a96853abce: loop:mean_reversion@import:crypto_sample_csv)
Markets: KXBTC15M, KXETH15M, KXSOL15M, KXXRP15M
Selected parameters (chosen on development data only): {"horizon_s": 30, "lookback_s": 5, "min_move_c": 2.0}
Grid points tried: 12

Walk-forward OUT-OF-SAMPLE (never used for selection):
  Sample:               154 trades / 308 fills over 16 markets, 1 days
  P&L at mid:           -$4.82
  Gross P&L:            -$17.85
  Fees:                 $22.01
  Estimated slippage:   $13.03  (taker cost vs mid, already inside gross)
  Net P&L:              -$39.86
  Expectancy / trade:   -$0.26
  Win rate:             22.7%
  Profit factor:        0.16
  Max drawdown:         $40.03
  Sharpe (per trade):   -0.72   Sharpe (daily, ann.): n/a
  Fill rate:            99.4%
  Adverse selection:    -0.32 c/contract (30s markout, + = adverse)
  Avg holding:          30.2 s

In-sample expectancy:   -$0.24 on 140 trades
Out-of-sample expect.:  -$0.26
Holdout test:           not evaluated (candidate failed earlier gates)
Cost/latency stress (OOS, net P&L): conservative_queue=-$39.86, fees_x2=-$61.87, latency_plus_250ms=-$39.86, slippage_plus_1tick=-$55.26
Random-direction benchmark: mean net -$30.60 over 200 trials; p-value 0.980
Probabilistic Sharpe (deflated for 12 trials): 0.000
Flags:
  [GATING] small_sample: out-of-sample trades 154 < 300
  [GATING] few_days: only 1 distinct trading days out of sample
  [GATING] parameter_sensitive: only 0% of neighbouring parameter sets are profitable
  [GATING] walk_forward_inconsistent: 0% of walk-forward folds profitable
  [GATING] not_better_than_random: p-value vs random-direction benchmark 0.980
  [GATING] low_deflated_sharpe: probabilistic Sharpe vs multiple-testing benchmark 0.00 < 0.95
  [info] inferred_settlements: 24 settlements inferred, not official
  [GATING] assumed_depth: some markets have assumed (not recorded) depth

Status: REJECT  -  out-of-sample net P&L -39.86 <= 0

Simulated/historical results under explicit fill, fee and latency assumptions. They are not evidence of future profitability. Out-of-sample means walk-forward folds never used for parameter selection; 'holdout' is a final set evaluated at most once.
```

```
Strategy: momentum   (experiment exp-7eefb4e637: loop:momentum@import:crypto_sample_csv)
Markets: KXBTC15M, KXETH15M, KXSOL15M, KXXRP15M
Selected parameters (chosen on development data only): {"horizon_s": 30, "lookback_s": 5, "min_move_c": 2.0}
Grid points tried: 12

Walk-forward OUT-OF-SAMPLE (never used for selection):
  Sample:               154 trades / 308 fills over 16 markets, 1 days
  P&L at mid:           $4.83
  Gross P&L:            -$8.20
  Fees:                 $21.98
  Estimated slippage:   $13.03  (taker cost vs mid, already inside gross)
  Net P&L:              -$30.18
  Expectancy / trade:   -$0.20
  Win rate:             28.6%
  Profit factor:        0.27
  Max drawdown:         $31.96
  Sharpe (per trade):   -0.51   Sharpe (daily, ann.): n/a
  Fill rate:            100.0%
  Adverse selection:    0.32 c/contract (30s markout, + = adverse)
  Avg holding:          30.2 s

In-sample expectancy:   -$0.20 on 140 trades
Out-of-sample expect.:  -$0.20
Holdout test:           not evaluated (candidate failed earlier gates)
Cost/latency stress (OOS, net P&L): conservative_queue=-$30.18, fees_x2=-$52.16, latency_plus_250ms=-$30.18, slippage_plus_1tick=-$45.55
Random-direction benchmark: mean net -$30.60 over 200 trials; p-value 0.478
Probabilistic Sharpe (deflated for 12 trials): 0.000
Flags:
  [GATING] small_sample: out-of-sample trades 154 < 300
  [GATING] few_days: only 1 distinct trading days out of sample
  [GATING] dies_after_costs: positive before costs (mid 4.83, gross -8.20) but net -30.18
  [GATING] parameter_sensitive: only 0% of neighbouring parameter sets are profitable
  [GATING] walk_forward_inconsistent: 0% of walk-forward folds profitable
  [GATING] not_better_than_random: p-value vs random-direction benchmark 0.478
  [GATING] low_deflated_sharpe: probabilistic Sharpe vs multiple-testing benchmark 0.00 < 0.95
  [info] inferred_settlements: 24 settlements inferred, not official
  [GATING] assumed_depth: some markets have assumed (not recorded) depth

Status: REJECT  -  out-of-sample net P&L -30.18 <= 0

Simulated/historical results under explicit fill, fee and latency assumptions. They are not evidence of future profitability. Out-of-sample means walk-forward folds never used for parameter selection; 'holdout' is a final set evaluated at most once.
```

```
Strategy: vol_fade   (experiment exp-4bb6b374b7: loop:vol_fade@import:baseline_csv)
Markets: KXETH15M
Selected parameters (chosen on development data only): {"horizon_s": 10, "ratio": 0.8}
Grid points tried: 4

Walk-forward OUT-OF-SAMPLE (never used for selection):
  Sample:               9 trades / 20 fills over 2 markets, 1 days
  P&L at mid:           -$1.58
  Gross P&L:            -$2.25
  Fees:                 $0.95
  Estimated slippage:   $0.67  (taker cost vs mid, already inside gross)
  Net P&L:              -$3.20
  Expectancy / trade:   -$0.36
  Win rate:             11.1%
  Profit factor:        0.01
  Max drawdown:         $3.20
  Sharpe (per trade):   -0.78   Sharpe (daily, ann.): n/a
  Fill rate:            89.4%
  Adverse selection:    1.78 c/contract (30s markout, + = adverse)
  Avg holding:          15.0 s

In-sample expectancy:   -$0.31 on 7 trades
Out-of-sample expect.:  -$0.36
Holdout test:           not evaluated (candidate failed earlier gates)
Cost/latency stress (OOS, net P&L): conservative_queue=-$3.20, fees_x2=-$4.15, latency_plus_250ms=-$3.43, slippage_plus_1tick=-$4.06
Random-direction benchmark: mean net -$1.31 over 200 trials; p-value 0.955
Probabilistic Sharpe (deflated for 4 trials): 0.000
Flags:
  [GATING] small_sample: out-of-sample trades 9 < 300
  [GATING] few_days: only 1 distinct trading days out of sample
  [GATING] few_markets: only 2 distinct markets out of sample
  [GATING] parameter_sensitive: only 0% of neighbouring parameter sets are profitable
  [GATING] walk_forward_inconsistent: 0% of walk-forward folds profitable
  [GATING] not_better_than_random: p-value vs random-direction benchmark 0.955
  [GATING] low_deflated_sharpe: probabilistic Sharpe vs multiple-testing benchmark 0.00 < 0.95
  [info] inferred_settlements: 2 settlements inferred, not official

Status: REJECT  -  out-of-sample net P&L -3.20 <= 0

Simulated/historical results under explicit fill, fee and latency assumptions. They are not evidence of future profitability. Out-of-sample means walk-forward folds never used for parameter selection; 'holdout' is a final set evaluated at most once.
```

```
Strategy: vol_breakout   (experiment exp-8ee4c50aac: loop:vol_breakout@import:baseline_csv)
Markets: KXETH15M
Selected parameters (chosen on development data only): {"horizon_s": 30, "ratio": 0.6}
Grid points tried: 4

Walk-forward OUT-OF-SAMPLE (never used for selection):
  Sample:               14 trades / 29 fills over 2 markets, 1 days
  P&L at mid:           $1.80
  Gross P&L:            $0.69
  Fees:                 $1.49
  Estimated slippage:   $1.11  (taker cost vs mid, already inside gross)
  Net P&L:              -$0.80
  Expectancy / trade:   -$0.06
  Win rate:             21.4%
  Profit factor:        0.70
  Max drawdown:         $1.70
  Sharpe (per trade):   -0.14   Sharpe (daily, ann.): n/a
  Fill rate:            88.7%
  Adverse selection:    -1.24 c/contract (30s markout, + = adverse)
  Avg holding:          19.6 s

In-sample expectancy:   -$0.06 on 11 trades
Out-of-sample expect.:  -$0.06
Holdout test:           not evaluated (candidate failed earlier gates)
Cost/latency stress (OOS, net P&L): conservative_queue=-$0.80, fees_x2=-$2.29, latency_plus_250ms=-$0.52, slippage_plus_1tick=-$2.07
Random-direction benchmark: mean net -$2.46 over 200 trials; p-value 0.005
Probabilistic Sharpe (deflated for 4 trials): 0.001
Flags:
  [GATING] small_sample: out-of-sample trades 14 < 300
  [GATING] few_days: only 1 distinct trading days out of sample
  [GATING] few_markets: only 2 distinct markets out of sample
  [GATING] dies_after_costs: positive before costs (mid 1.80, gross 0.69) but net -0.80
  [GATING] walk_forward_inconsistent: 25% of walk-forward folds profitable
  [GATING] low_deflated_sharpe: probabilistic Sharpe vs multiple-testing benchmark 0.00 < 0.95
  [info] inferred_settlements: 2 settlements inferred, not official

Status: REJECT  -  out-of-sample net P&L -0.80 <= 0

Simulated/historical results under explicit fill, fee and latency assumptions. They are not evidence of future profitability. Out-of-sample means walk-forward folds never used for parameter selection; 'holdout' is a final set evaluated at most once.
```

```
Strategy: mean_reversion   (experiment exp-e7cdfc1f7a: loop:mean_reversion@import:baseline_csv)
Markets: KXETH15M
Selected parameters (chosen on development data only): {"horizon_s": 30, "lookback_s": 30, "min_move_c": 2.0}
Grid points tried: 12

Walk-forward OUT-OF-SAMPLE (never used for selection):
  Sample:               23 trades / 48 fills over 2 markets, 1 days
  P&L at mid:           -$2.60
  Gross P&L:            -$4.14
  Fees:                 $2.13
  Estimated slippage:   $1.54  (taker cost vs mid, already inside gross)
  Net P&L:              -$6.27
  Expectancy / trade:   -$0.27
  Win rate:             8.7%
  Profit factor:        0.11
  Max drawdown:         $6.27
  Sharpe (per trade):   -0.65   Sharpe (daily, ann.): n/a
  Fill rate:            81.7%
  Adverse selection:    -0.71 c/contract (30s markout, + = adverse)
  Avg holding:          69.4 s

In-sample expectancy:   -$0.08 on 17 trades
Out-of-sample expect.:  -$0.27
Holdout test:           not evaluated (candidate failed earlier gates)
Cost/latency stress (OOS, net P&L): conservative_queue=-$6.27, fees_x2=-$8.40, latency_plus_250ms=-$1.61, slippage_plus_1tick=-$8.08
Random-direction benchmark: mean net -$3.07 over 200 trials; p-value 0.995
Probabilistic Sharpe (deflated for 12 trials): 0.000
Flags:
  [GATING] small_sample: out-of-sample trades 23 < 300
  [GATING] few_days: only 1 distinct trading days out of sample
  [GATING] few_markets: only 2 distinct markets out of sample
  [GATING] parameter_sensitive: only 0% of neighbouring parameter sets are profitable
  [GATING] walk_forward_inconsistent: 0% of walk-forward folds profitable
  [GATING] not_better_than_random: p-value vs random-direction benchmark 0.995
  [GATING] low_deflated_sharpe: probabilistic Sharpe vs multiple-testing benchmark 0.00 < 0.95
  [info] inferred_settlements: 2 settlements inferred, not official

Status: REJECT  -  out-of-sample net P&L -6.27 <= 0

Simulated/historical results under explicit fill, fee and latency assumptions. They are not evidence of future profitability. Out-of-sample means walk-forward folds never used for parameter selection; 'holdout' is a final set evaluated at most once.
```

```
Strategy: momentum   (experiment exp-a375c4f216: loop:momentum@import:baseline_csv)
Markets: KXETH15M
Selected parameters (chosen on development data only): {"horizon_s": 30, "lookback_s": 5, "min_move_c": 2.0}
Grid points tried: 12

Walk-forward OUT-OF-SAMPLE (never used for selection):
  Sample:               22 trades / 48 fills over 2 markets, 1 days
  P&L at mid:           $8.09
  Gross P&L:            $6.39
  Fees:                 $2.10
  Estimated slippage:   $1.69  (taker cost vs mid, already inside gross)
  Net P&L:              $4.29
  Expectancy / trade:   $0.19
  Win rate:             31.8%
  Profit factor:        2.06
  Max drawdown:         $1.42
  Sharpe (per trade):   0.19   Sharpe (daily, ann.): n/a
  Fill rate:            76.6%
  Adverse selection:    -0.97 c/contract (30s markout, + = adverse)
  Avg holding:          81.9 s

In-sample expectancy:   $0.16 on 22 trades
Out-of-sample expect.:  $0.19
Holdout test:           not evaluated (candidate failed earlier gates)
Cost/latency stress (OOS, net P&L): conservative_queue=$4.29, fees_x2=$2.19, latency_plus_250ms=$4.93, slippage_plus_1tick=$2.51
Random-direction benchmark: mean net -$2.86 over 200 trials; p-value 0.010
Probabilistic Sharpe (deflated for 12 trials): 0.051
Flags:
  [GATING] small_sample: out-of-sample trades 22 < 300
  [GATING] concentrated_market: 100% of positive P&L from one market
  [GATING] concentrated_day: 100% of positive P&L from one day
  [GATING] few_days: only 1 distinct trading days out of sample
  [GATING] few_markets: only 2 distinct markets out of sample
  [GATING] walk_forward_inconsistent: 50% of walk-forward folds profitable
  [GATING] low_deflated_sharpe: probabilistic Sharpe vs multiple-testing benchmark 0.05 < 0.95
  [info] inferred_settlements: 2 settlements inferred, not official

Status: RESEARCH  -  positive but only 22 out-of-sample trades

Simulated/historical results under explicit fill, fee and latency assumptions. They are not evidence of future profitability. Out-of-sample means walk-forward folds never used for parameter selection; 'holdout' is a final set evaluated at most once.
```

```
Strategy: market_maker   (experiment exp-c0ae7edfdf: loop:market_maker@import:baseline_csv)
Markets: KXETH15M
Selected parameters (chosen on development data only): {"gamma": 0.05, "min_edge_c": 0.25}
Grid points tried: 6

Walk-forward OUT-OF-SAMPLE (never used for selection):
  Sample:               7 trades / 12 fills over 2 markets, 1 days
  P&L at mid:           -$1.90
  Gross P&L:            -$3.25
  Fees:                 $0.29
  Estimated slippage:   $0.00  (taker cost vs mid, already inside gross)
  Net P&L:              -$3.54
  Expectancy / trade:   -$0.51
  Win rate:             14.3%
  Profit factor:        0.03
  Max drawdown:         $3.77
  Sharpe (per trade):   -1.15   Sharpe (daily, ann.): n/a
  Fill rate:            3.5%
  Adverse selection:    1.12 c/contract (30s markout, + = adverse)
  Avg holding:          120.7 s

In-sample expectancy:   -$0.50 on 4 trades
Out-of-sample expect.:  -$0.51
Holdout test:           not evaluated (candidate failed earlier gates)
Cost/latency stress (OOS, net P&L): conservative_queue=-$3.54, fees_x2=-$3.83, latency_plus_250ms=-$3.54, slippage_plus_1tick=-$3.54
Probabilistic Sharpe (deflated for 6 trials): 0.000
Flags:
  [GATING] small_sample: out-of-sample trades 7 < 300
  [GATING] few_days: only 1 distinct trading days out of sample
  [GATING] few_markets: only 2 distinct markets out of sample
  [GATING] parameter_sensitive: only 0% of neighbouring parameter sets are profitable
  [GATING] walk_forward_inconsistent: 0% of walk-forward folds profitable
  [info] no_random_benchmark: no random-entry benchmark available for this strategy type
  [GATING] low_deflated_sharpe: probabilistic Sharpe vs multiple-testing benchmark 0.00 < 0.95
  [info] inferred_settlements: 2 settlements inferred, not official

Status: REJECT  -  out-of-sample net P&L -3.54 <= 0

Simulated/historical results under explicit fill, fee and latency assumptions. They are not evidence of future profitability. Out-of-sample means walk-forward folds never used for parameter selection; 'holdout' is a final set evaluated at most once.
```

```
Strategy: imbalance   (experiment exp-79cb0eb766: loop:imbalance@import:baseline_csv)
Markets: KXETH15M
Selected parameters (chosen on development data only): {"horizon_s": 30, "max_spread_c": 3.0, "threshold": 0.7}
Grid points tried: 20

Walk-forward OUT-OF-SAMPLE (never used for selection):
  Sample:               26 trades / 53 fills over 2 markets, 1 days
  P&L at mid:           $0.41
  Gross P&L:            -$1.34
  Fees:                 $2.32
  Estimated slippage:   $1.74  (taker cost vs mid, already inside gross)
  Net P&L:              -$3.66
  Expectancy / trade:   -$0.14
  Win rate:             26.9%
  Profit factor:        0.30
  Max drawdown:         $4.27
  Sharpe (per trade):   -0.30   Sharpe (daily, ann.): n/a
  Fill rate:            84.4%
  Adverse selection:    0.07 c/contract (30s markout, + = adverse)
  Avg holding:          25.9 s

In-sample expectancy:   -$0.07 on 25 trades
Out-of-sample expect.:  -$0.14
Holdout test:           not evaluated (candidate failed earlier gates)
Cost/latency stress (OOS, net P&L): conservative_queue=-$3.66, fees_x2=-$5.98, latency_plus_250ms=-$7.00, slippage_plus_1tick=-$5.83
Random-direction benchmark: mean net -$4.88 over 200 trials; p-value 0.343
Probabilistic Sharpe (deflated for 20 trials): 0.000
Flags:
  [GATING] small_sample: out-of-sample trades 26 < 300
  [GATING] few_days: only 1 distinct trading days out of sample
  [GATING] few_markets: only 2 distinct markets out of sample
  [GATING] dies_after_costs: positive before costs (mid 0.41, gross -1.34) but net -3.66
  [GATING] parameter_sensitive: only 0% of neighbouring parameter sets are profitable
  [GATING] walk_forward_inconsistent: 0% of walk-forward folds profitable
  [GATING] not_better_than_random: p-value vs random-direction benchmark 0.343
  [GATING] low_deflated_sharpe: probabilistic Sharpe vs multiple-testing benchmark 0.00 < 0.95
  [info] inferred_settlements: 2 settlements inferred, not official

Status: REJECT  -  out-of-sample net P&L -3.66 <= 0

Simulated/historical results under explicit fill, fee and latency assumptions. They are not evidence of future profitability. Out-of-sample means walk-forward folds never used for parameter selection; 'holdout' is a final set evaluated at most once.
```
