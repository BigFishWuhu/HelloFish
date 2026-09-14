from bisect import bisect_right


# Captured from the app's “等级说明 -> 贡献等级” table on 2026-09-14.
# Each tuple is: first level, last level, minimum contribution at first level,
# and the per-level increment inside that inclusive range.
WEALTH_LEVEL_SEGMENTS: tuple[tuple[int, int, int, int], ...] = (
    (0, 10, 0, 10),
    (11, 19, 200, 100),
    (20, 29, 1_200, 200),
    (30, 43, 3_500, 500),
    (44, 53, 11_000, 1_000),
    (54, 68, 22_000, 2_000),
    (69, 98, 55_000, 5_000),
    (99, 128, 210_000, 10_000),
    (129, 158, 550_000, 50_000),
    (159, 188, 2_100_000, 100_000),
    (189, 198, 5_500_000, 500_000),
    (199, 238, 11_000_000, 1_000_000),
    (239, 288, 52_000_000, 2_000_000),
    (289, 298, 155_000_000, 5_000_000),
    (299, 300, 210_000_000, 10_000_000),
)


def _build_thresholds() -> tuple[int, ...]:
    values: list[int] = []
    expected_level = 0
    for first, last, first_value, step in WEALTH_LEVEL_SEGMENTS:
        if first != expected_level:
            raise ValueError(f"财富等级映射不连续：缺少等级 {expected_level}")
        values.extend(first_value + (level - first) * step for level in range(first, last + 1))
        expected_level = last + 1
    if expected_level != 301 or any(a >= b for a, b in zip(values, values[1:])):
        raise ValueError("财富等级映射必须严格递增并覆盖 0-300 级")
    return tuple(values)


WEALTH_LEVEL_MIN_CONTRIBUTIONS = _build_thresholds()


def minimum_contribution_for_wealth_level(level: int) -> int:
    if not 0 <= level < len(WEALTH_LEVEL_MIN_CONTRIBUTIONS):
        raise ValueError("财富等级必须在 0-300 之间")
    return WEALTH_LEVEL_MIN_CONTRIBUTIONS[level]


def wealth_level_for_contribution(contribution: int) -> int:
    if contribution < 0:
        raise ValueError("贡献值不能为负数")
    return min(
        bisect_right(WEALTH_LEVEL_MIN_CONTRIBUTIONS, contribution) - 1,
        300,
    )
