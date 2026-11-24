---
name: lab_interpreter
description: |
  Use this skill when the user shares lab test values (血常规, 血生化, 甲功, 凝血 etc.)
  and asks what they mean, whether they are normal, or what action to take. Triggered by
  phrases like "帮我看看化验单", "我的血红蛋白偏低是什么意思", "这个指标正常吗". Primarily
  used by report_agent; also available to clinic_agent when symptoms co-occur with results.
tags: [report, clinic]
version: "1.1"
---

# Lab Interpreter Skill

Compares each numeric result with the supplied report range. Returns
HIGH / LOW / NORMAL when comparable and UNKNOWN when the range is unusable.

## Supported Test Panels

| Panel | Items |
|-------|-------|
| 血常规 (CBC) | WBC, RBC, HGB, PLT, HCT, MCV |
| 血糖 | GLU (空腹血糖), HBA1C |
| 肾功能 | CREA, BUN, UA |
| 肝功能 | ALT, AST |
| 血脂 | TC, TG, HDL, LDL |
| 甲状腺 | TSH, FT3, FT4 |
| 凝血 | PT, APTT, INR |

## Required Input

- `items` — list of `{name, value}` objects, e.g.:
  ```json
  [{"name": "WBC", "value": 11.5}, {"name": "HGB", "value": 102}]
  ```

## Optional Inputs

- `items[].unit` — the reported result unit
- `items[].reference_range` — the printed or explicitly supplied numeric interval
  or inequality for this row, such as `1–10` or `<20`
- `require_report_reference` — set to `true` to compare a personal report
  using its original ranges. Leave missing or illegible ranges empty.
- `patient_gender` — adjusts HGB, RBC, UA reference ranges

## Output Notes

- `summary` counts comparable and pending rows separately.
- `reference_source` records whether each range came from the report or the built-in reference.
- Show `disclaimer` alongside the comparison.
