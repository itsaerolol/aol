from abc import ABC, abstractmethod


class AOLPlugin(ABC):
    name: str = "unnamed"

    def on_start(self) -> None:
        """Called once when the filter agent starts."""

    def on_stop(self) -> None:
        """Called once on clean shutdown — flush any pending state here."""

    @abstractmethod
    def process(self, result: dict, context: str, source: str) -> None:
        """
        Called after every filter cycle.

        result: dict with 'memories' (list) and 'kg_facts' (list)
        context: the raw screen/audio text sent to Claude
        source: 'activity-summary' or 'raw-search'
        """
