from enum import Enum


class TruthClass(str, Enum):
    OBSERVED = "OBSERVED"
    ESTIMATED = "ESTIMATED"
    INFERRED = "INFERRED"
    UNKNOWN = "UNKNOWN"


class EvidencePurpose(str, Enum):
    PURCHASE = "PURCHASE"
    SEARCH = "SEARCH"
    AUDIENCE = "AUDIENCE"
    CONTENT = "CONTENT"
    QUALITATIVE = "QUALITATIVE"
    PRICE = "PRICE"
    COMPETITION = "COMPETITION"
    REACH = "REACH"


class Classification(str, Enum):
    RED = "RED"
    YELLOW = "YELLOW"
    GREEN = "GREEN"
