"""Descriptive summaries of already calculated paired sensitivity evidence."""

import csv
from collections import Counter, defaultdict
from pathlib import Path


def summarize_outputs(stage: Path) -> dict:
    """Summarize final states and paired timing without pooling budget scales.

    Inputs are the writer's own completed staging tables. Configuration-level
    fractions retain their planned denominator. Overall counts describe final
    states at each configuration's declared budget; they are not an optimizer
    ranking or an estimate of a population success probability.
    """
    with (stage / "success-trajectories.csv").open(newline="", encoding="utf-8") as source:
        rows = list(csv.DictReader(source))
    baseline = {
        (row["configuration_id"], int(row["high_fidelity_budget"])): row
        for row in rows
        if row["series"] == "archived_score_at_least_two"
    }
    finals = {}
    curves = defaultdict(
        lambda: {"max_abs_confirmed_fraction_change": 0.0, "max_abs_upper_bound_change": 0.0}
    )
    for row in rows:
        key = row["configuration_id"], row["series"]
        spent = int(row["high_fidelity_budget"])
        if key not in finals or spent > int(finals[key]["high_fidelity_budget"]):
            finals[key] = row
        reference = baseline[(key[0], spent)]
        for source_field, output_field in (
            ("success_fraction", "max_abs_confirmed_fraction_change"),
            ("success_fraction_upper_bound", "max_abs_upper_bound_change"),
        ):
            change = abs(float(row[source_field]) - float(reference[source_field]))
            if change > curves[key][output_field]:
                curves[key][output_field] = change
                curves[key][output_field + "_budget"] = spent

    paired = defaultdict(Counter)
    with (stage / "paired-comparisons.csv").open(newline="", encoding="utf-8") as source:
        for row in csv.DictReader(source):
            stats = paired[(row["configuration_id"], row["candidate_series"])]
            left, right = row["archived_score_state"], row["candidate_state"]
            stats["recorded_pair_count"] += 1
            stats["changed_state_count"] += int(left != right)
            stats["known_success_gain_count"] += int(row["confirmed_success_gain"] == "True")
            stats["known_success_loss_count"] += int(row["confirmed_success_loss"] == "True")
            stats["changed_state_involving_unknown_count"] += int(
                left != right and "unknown" in (left, right)
            )
            if left == right == "confirmed_success":
                difference = int(row["candidate_minus_archived_first_index"])
                stats["both_confirmed_by_budget_count"] += 1
                stats["earlier_confirmed_event_count"] += int(difference < 0)
                stats["later_confirmed_event_count"] += int(difference > 0)
                stats["unchanged_confirmed_event_count"] += int(difference == 0)

    configuration_rows = []
    totals = defaultdict(Counter)
    pair_totals = defaultdict(Counter)
    count_fields = {
        "cohort_size": "planned_campaign_count",
        "success_count": "confirmed_success_count",
        "unknown_success_count": "unknown_count",
        "observed_no_success_count": "observed_no_success_count",
    }
    for key, row in finals.items():
        config_id, series = key
        reference = finals[(config_id, "archived_score_at_least_two")]
        item = {
            "configuration_id": config_id,
            "series": series,
            "declared_high_fidelity_budget": int(row["high_fidelity_budget"]),
        }
        for source_field, output_field in count_fields.items():
            item[output_field] = int(row[source_field])
            totals[series][output_field] += item[output_field]
        item.update(
            success_fraction=float(row["success_fraction"]),
            success_fraction_upper_bound=float(row["success_fraction_upper_bound"]),
            candidate_minus_archived_confirmed_count=int(row["success_count"])
            - int(reference["success_count"]),
            **curves[key],
        )
        if series != "archived_score_at_least_two":
            item.update(paired[key])
            pair_totals[series].update(paired[key])
        configuration_rows.append(item)
    columns = list(dict.fromkeys(name for row in configuration_rows for name in row))
    with (stage / "configuration-summary.csv").open("w", newline="", encoding="utf-8") as output:
        writer = csv.DictWriter(output, fieldnames=columns)
        writer.writeheader()
        writer.writerows(configuration_rows)
    return {
        "overall_final_budget_states": [
            {"series": key, **dict(value)} for key, value in totals.items()
        ],
        "overall_paired_changes": [
            {"candidate_series": key, **dict(value)} for key, value in pair_totals.items()
        ],
        "overall_count_scope": "Descriptive counts at each configuration's own declared HF budget; budgets and protocols differ. Unknown outcomes remain in the planned denominator. No optimizer ranking or causal effect is estimated.",
        "timing_scope": "Earlier/later event counts include only recorded pairs with confirmed success for both rules by the declared budget. They do not describe unsuccessful or unknown campaigns.",
        "configuration_summary_row_count": len(configuration_rows),
    }
