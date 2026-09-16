from enum import Enum


class TruthClass(str, Enum):
    OBSERVED = "OBSERVED"
    ESTIMATED = "ESTIMATED"
    INFERRED = "INFERRED"
    UNKNOWN = "UNKNOWN"


class EvidencePurpose(str, Enum):
    PURCHASE = "PURCHASE"
    SEARCH_DEMAND = "SEARCH_DEMAND"
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


class ProductFormat(str, Enum):
    PDF_GUIDE = "PDF_GUIDE"
    WORKBOOK = "WORKBOOK"
    CHECKLIST = "CHECKLIST"
    TEMPLATE_PACK = "TEMPLATE_PACK"
    SPREADSHEET_TOOL = "SPREADSHEET_TOOL"
    DATA_TEMPLATE = "DATA_TEMPLATE"
    PRINTABLE_BUNDLE = "PRINTABLE_BUNDLE"


class CandidateStatus(str, Enum):
    UNRESEARCHED = "UNRESEARCHED"
    RESEARCHING = "RESEARCHING"
    RESEARCHED = "RESEARCHED"
    REJECTED = "REJECTED"


class SnapshotStatus(str, Enum):
    PENDING = "PENDING"
    COMPLETE = "COMPLETE"
    PARTIAL = "PARTIAL"
    FAILED = "FAILED"


# ------------------------------------------------------- collection methods
#
# The vocabulary of `EvidenceItem.collection_method`. Defined here, in the
# domain that owns the field, so every capability and Evidence Confidence
# agree on the spelling: `provenance_directness_v1` maps exactly these values,
# and a divergent spelling would silently score reused evidence as a direct
# API observation.
COLLECTION_METHOD_OFFICIAL_API = "official_api"
COLLECTION_METHOD_CACHE = "cache"
