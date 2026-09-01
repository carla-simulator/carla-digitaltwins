"""Run ``twinmodel build`` with the divided-carriageway model switched off, for before/after
comparisons on the same code (the validator, the cul-de-sac tagging and the junction lane-link
fallback stay on, so the difference is exactly the profile-gated junction model).

    python -m tools.build_variant off  build --bbox ... --profile us_suburban ...
    python -m tools.build_variant on   build ...            # same as `python -m twinmodel`
"""
from __future__ import annotations

import sys
from dataclasses import replace

from twinmodel import profiles


def main(argv: list[str] | None = None) -> int:
    argv = list(sys.argv[1:] if argv is None else argv)
    if not argv or argv[0] not in ("on", "off"):
        print(__doc__)
        return 2
    mode, rest = argv[0], argv[1:]
    if mode == "off":
        for name, p in list(profiles.PROFILES.items()):
            profiles.PROFILES[name] = p.with_(junction=replace(
                p.junction, dual_carriageway_max_gap_m=0.0, median_max_width_m=0.0, sliver_m=0.0))
    from twinmodel.cli import main as cli_main
    return cli_main(rest)


if __name__ == "__main__":
    raise SystemExit(main())
