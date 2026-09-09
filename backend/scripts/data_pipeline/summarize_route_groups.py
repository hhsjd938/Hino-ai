#!/usr/bin/env python3
"""Group repeated HINO trips from validated route-pair coverage results."""

from __future__ import annotations

import argparse
import json
from collections import Counter, defaultdict
from pathlib import Path

import pandas as pd


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Group trips connected by validated route overlap.")
    parser.add_argument("--pairs", type=Path, required=True, help="Path to route_screened_pairs.csv")
    parser.add_argument("--output-dir", type=Path, required=True, help="Directory for group CSV and JSON outputs")
    parser.add_argument("--threshold", type=float, default=0.9, help="Minimum bidirectional route coverage")
    return parser.parse_args()


class UnionFind:
    def __init__(self) -> None:
        self.parent: dict[tuple[str, str], tuple[str, str]] = {}

    def add(self, item: tuple[str, str]) -> None:
        self.parent.setdefault(item, item)

    def find(self, item: tuple[str, str]) -> tuple[str, str]:
        root = item
        while self.parent[root] != root:
            root = self.parent[root]
        while self.parent[item] != item:
            parent = self.parent[item]
            self.parent[item] = root
            item = parent
        return root

    def union(self, left: tuple[str, str], right: tuple[str, str]) -> None:
        self.add(left)
        self.add(right)
        a, b = self.find(left), self.find(right)
        if a != b:
            self.parent[b] = a


def main() -> None:
    args = parse_args()
    if not 0 < args.threshold <= 1:
        raise SystemExit("--threshold must be greater than 0 and at most 1")

    pairs = pd.read_csv(
        args.pairs,
        dtype={"vehicle": str, "earlier_journey": str, "later_journey": str},
    )
    required = {"vehicle", "earlier_journey", "later_journey", "route_min_coverage"}
    missing = required.difference(pairs.columns)
    if missing:
        raise SystemExit(f"Missing required columns: {', '.join(sorted(missing))}")

    edges = pairs[pairs["route_min_coverage"].ge(args.threshold)].copy()
    uf = UnionFind()
    node_values: dict[tuple[str, str], dict] = {}

    for row in edges.to_dict("records"):
        vehicle = str(row["vehicle"])
        earlier = (vehicle, str(row["earlier_journey"]))
        later = (vehicle, str(row["later_journey"]))
        uf.union(earlier, later)
        node_values.setdefault(
            earlier,
            {
                "vehicle": vehicle,
                "journey": earlier[1],
                "start": row.get("earlier_start"),
                "km": row.get("earlier_km"),
                "fuel_l": row.get("earlier_fuel_l"),
                "l_per_100km": row.get("earlier_l_per_100km"),
            },
        )
        node_values.setdefault(
            later,
            {
                "vehicle": vehicle,
                "journey": later[1],
                "start": row.get("later_start"),
                "km": row.get("later_km"),
                "fuel_l": row.get("later_fuel_l"),
                "l_per_100km": row.get("later_l_per_100km"),
            },
        )

    components: dict[tuple[str, str], list[tuple[str, str]]] = defaultdict(list)
    for node in uf.parent:
        components[uf.find(node)].append(node)

    ordered = sorted(
        components.values(),
        key=lambda nodes: (nodes[0][0], min(str(node_values[node].get("start")) for node in nodes)),
    )
    counters: Counter[str] = Counter()
    group_for_node: dict[tuple[str, str], str] = {}
    for nodes in ordered:
        vehicle = nodes[0][0]
        counters[vehicle] += 1
        group_id = f"{vehicle}-R{counters[vehicle]:03d}"
        for node in nodes:
            group_for_node[node] = group_id

    edge_counts: Counter[str] = Counter()
    edge_min_coverage: dict[str, float] = {}
    for row in edges.to_dict("records"):
        node = (str(row["vehicle"]), str(row["earlier_journey"]))
        group_id = group_for_node[node]
        edge_counts[group_id] += 1
        value = float(row["route_min_coverage"])
        edge_min_coverage[group_id] = min(edge_min_coverage.get(group_id, value), value)

    members = []
    summaries = []
    for nodes in ordered:
        group_id = group_for_node[nodes[0]]
        records = [node_values[node] for node in nodes]
        frame = pd.DataFrame(records).sort_values("start", kind="stable")
        trip_count = len(frame)
        possible_pairs = trip_count * (trip_count - 1) // 2
        direct_pairs = edge_counts[group_id]
        frame.insert(0, "route_group", group_id)
        frame["group_trip_count"] = trip_count
        members.extend(frame.to_dict("records"))
        summaries.append(
            {
                "route_group": group_id,
                "vehicle": frame["vehicle"].iloc[0],
                "trip_count": trip_count,
                "first_start": frame["start"].min(),
                "last_start": frame["start"].max(),
                "median_km": float(frame["km"].median()),
                "median_fuel_l": float(frame["fuel_l"].median()),
                "median_l_per_100km": float(frame["l_per_100km"].median()),
                "validated_direct_pairs": direct_pairs,
                "possible_pairs": possible_pairs,
                "direct_pair_density": direct_pairs / possible_pairs if possible_pairs else 0,
                "minimum_observed_coverage": edge_min_coverage[group_id],
            }
        )

    member_frame = pd.DataFrame(members)
    summary_frame = pd.DataFrame(summaries).sort_values(
        ["trip_count", "vehicle", "route_group"], ascending=[False, True, True], kind="stable"
    )
    output = args.output_dir.resolve()
    output.mkdir(parents=True, exist_ok=True)
    member_frame.to_csv(output / "route_group_members.csv", index=False, encoding="utf-8-sig")
    summary_frame.to_csv(output / "route_groups.csv", index=False, encoding="utf-8-sig")

    complete_groups = int(summary_frame["direct_pair_density"].eq(1).sum()) if len(summary_frame) else 0
    report = {
        "definition": "Same vehicle; pair passed endpoint, distance, and fuel screening; bidirectional route coverage minimum meets the threshold.",
        "coverage_threshold": args.threshold,
        "validated_direct_pairs": int(len(edges)),
        "repeated_route_groups": int(len(summary_frame)),
        "grouped_distinct_trips": int(len(member_frame)),
        "vehicles_with_repeated_routes": int(member_frame["vehicle"].nunique()) if len(member_frame) else 0,
        "complete_pair_groups": complete_groups,
        "largest_group_trip_count": int(summary_frame["trip_count"].max()) if len(summary_frame) else 0,
        "trip_count_by_group_size": {
            str(int(size)): int(count) for size, count in summary_frame["trip_count"].value_counts().sort_index().items()
        },
        "method_limit": "Connected components are used. A chain of overlapping pairs can place two trips in one group even when those two trips were not directly validated against each other; direct_pair_density shows how complete each group is.",
    }
    (output / "route_groups_summary.json").write_text(
        json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    print(json.dumps(report, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
