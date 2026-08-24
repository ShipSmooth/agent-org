"""FBA inbound box planning (docs/replenishment.md §8).

Amazon wants 5–10 boxes per shipment, every box packed identically. So for
each SKU we need one per-box quantity that, multiplied by the box count,
lands as close as possible to what we wanted to send without going over.

Brute force on purpose: six box counts, two candidate quantities each. A
solver would be harder to check by hand, and this needs to be checkable by
hand.
"""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class BoxLine:
    sku: str
    target: int
    per_box: int

    def planned(self, boxes: int) -> int:
        return boxes * self.per_box

    def shortfall(self, boxes: int) -> int:
        return self.target - self.planned(boxes)


@dataclass(frozen=True)
class BoxPlan:
    boxes: int
    lines: tuple[BoxLine, ...]
    error: int

    def planned(self, sku: str) -> int:
        for line in self.lines:
            if line.sku == sku:
                return line.planned(self.boxes)
        return 0


def plan_boxes(
    targets: dict[str, int],
    box_min: int = 5,
    box_max: int = 10,
    overship_tolerance: int = 0,
) -> BoxPlan | None:
    """Choose a box count and a per-box quantity per SKU.

    Ties break toward fewer boxes: less handling at prep time, and the
    per-box weight caps already bound the alternative.
    """
    wanted = {sku: qty for sku, qty in targets.items() if qty > 0}
    if not wanted:
        return None

    best: BoxPlan | None = None
    for boxes in range(box_min, box_max + 1):
        lines: list[BoxLine] = []
        error = 0
        for sku, target in sorted(wanted.items()):
            candidates = {target // boxes, -(-target // boxes)}
            allowed = [
                qty
                for qty in sorted(candidates)
                if qty >= 0 and boxes * qty <= target + overship_tolerance
            ]
            per_box = max(allowed) if allowed else 0
            lines.append(BoxLine(sku=sku, target=target, per_box=per_box))
            error += abs(boxes * per_box - target)
        if all(line.per_box == 0 for line in lines):
            continue
        plan = BoxPlan(boxes=boxes, lines=tuple(lines), error=error)
        if best is None or plan.error < best.error:
            best = plan  # strict <, so an equal-error larger B never wins
    return best


__all__ = ["BoxLine", "BoxPlan", "plan_boxes"]
