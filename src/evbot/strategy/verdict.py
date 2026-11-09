from collections.abc import Sequence
from dataclasses import dataclass
from typing import Protocol

from evbot.domain.signals import FilterResult, Verdict
from evbot.strategy.filters import AVG_VOLUME, IV_RV, SPREAD_AVAILABLE, TERM_SLOPE


class VerdictPolicy(Protocol):
    def decide(self, filters: Sequence[FilterResult]) -> Verdict: ...


@dataclass(frozen=True, slots=True)
class DefaultVerdictPolicy:
    def decide(self, filters: Sequence[FilterResult]) -> Verdict:
        passed = {result.name: result.passed for result in filters}
        if not passed[SPREAD_AVAILABLE] or not passed[TERM_SLOPE]:
            return Verdict.AVOID
        supporting = passed[AVG_VOLUME] + passed[IV_RV]
        if supporting == 2:
            return Verdict.RECOMMENDED
        return Verdict.CONSIDER if supporting == 1 else Verdict.AVOID
