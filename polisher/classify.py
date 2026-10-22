"""Stage 2: label each slide with a type and a one-sentence intent (17 Oct build plan).

The labels are hints for the planner, never instructions; the pipeline runs
without them when the classifier fails. ``ClaudeClassifier`` makes one cheap
call per deck; ``HeuristicClassifier`` is the free offline stand-in.
"""

from dataclasses import dataclass


@dataclass
class SlideLabel:
    type: str
    intent: str
