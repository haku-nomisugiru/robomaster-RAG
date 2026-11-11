"""Local vector lookup and weighted rank fusion for HTTP functional search."""

import logging
from threading import Lock

from library import result_content_key
from vector_search import STORE, card_record, encoder, find_similar, open_collection, stored_metadata


RRF_OFFSET = 4
VECTOR_WEIGHT = 0.35
LEXICAL_COVERAGE_WEIGHT = 0.12
LOG = logging.getLogger(__name__)


class SemanticSearch:
    def __init__(self, root, cards):
        self.root = root
        self.cards = cards
        self.status = "not_loaded"
        self._lock = Lock()

    def search(self, query, limit, section=None, *, candidate_ids=None):
        if candidate_ids is not None and not candidate_ids:
            return []
        with self._lock:
            try:
                if self.status == "not_loaded":
                    self.collection = open_collection(self.root / STORE)
                    self.records = {mid: card_record(card) for mid, card in self.cards.items()}
                    if stored_metadata(self.collection) != {mid: record["metadata"]
                                                           for mid, record in self.records.items()}:
                        self.status = "stale"
                        LOG.warning("Chroma index differs from the catalog. Synchronize and restart the service.")
                        return None
                    self.model = encoder(self.root)
                    self.status = "ready"
                if self.status == "ready":
                    return find_similar(self.collection, self.records, self.model, query, limit, section,
                                        candidate_ids=candidate_ids)
            except Exception:
                # A broken optional encoder or store should leave the SQLite endpoint usable.
                self.status = "unavailable"
                LOG.warning("Chroma unavailable. Falling back to SQLite.", exc_info=True)
            return None


def fuse(lexical, semantic, cards, limit, query_term_count):
    candidates = {}
    for channel, hits in (("sqlite", lexical), ("chroma", semantic)):
        for rank, hit in enumerate(hits, 1):
            mid = hit["id"]
            key = result_content_key(cards[mid])
            result = candidates.setdefault(key, {"id": mid, "score": 0.0, "exact_match": False,
                                                "matches": [], "retrieval_channels": {}})
            channels = result["retrieval_channels"]
            if channel in channels:
                continue
            if channel == "sqlite":
                matched = {term for field in hit["matches"] for term in field["terms"]}
                coverage = len(matched) / query_term_count
                channels[channel] = {"rank": rank, "score": hit["score"], "term_coverage": coverage}
                result["exact_match"] = hit["exact_match"]
                result["matches"] = hit["matches"]
                result["score"] += (int(hit["exact_match"]) + 1 / (RRF_OFFSET + rank)
                                    + LEXICAL_COVERAGE_WEIGHT * coverage)
            else:
                channels[channel] = {"rank": rank, "distance": hit["distance"]}
                if mid != result["id"]:
                    channels[channel]["card_id"] = mid
                result["score"] += VECTOR_WEIGHT / (RRF_OFFSET + rank)
    return sorted(candidates.values(), key=lambda hit: (-hit["score"], hit["id"]))[:limit]
