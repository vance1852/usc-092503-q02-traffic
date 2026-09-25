"""交通事故结构化证据的基础组件。"""

from .contracts import EvidenceItem, EvidenceProtocol, ValidationError
from .analysis import ALGORITHM_VERSION, analyze, bootstrap_mean_interval
from .numeric import NumericSummary, WilsonInterval
from .service import EvidenceReviewService

__all__ = [
    "NumericSummary",
    "EvidenceItem",
    "EvidenceProtocol",
    "ValidationError",
    "WilsonInterval",
    "ALGORITHM_VERSION",
    "EvidenceReviewService",
    "analyze",
    "bootstrap_mean_interval",
]

__version__ = "0.1.0"
