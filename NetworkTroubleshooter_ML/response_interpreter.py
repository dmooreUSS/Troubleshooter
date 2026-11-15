"""Optional, offline BGE suggestions for agent phrasing.

Suggestions are tentative: confidence=0.5 keeps them below the rule engine's
required 1.0. Similarity scores are not probabilities. Explicit measurements
remain the parser's responsibility. Negation, chronology, and device identity
are not reliably resolved by embedding similarity; suggestions need review.
"""

import math
import re
import sys
from pathlib import Path


MODEL_NAME = "BAAI/bge-small-en-v1.5"
QUERY_PREFIX = "Represent this sentence for searching relevant passages: "
MIN_SIMILARITY = 0.78
MIN_MARGIN = 0.05
TENTATIVE_CONFIDENCE = 0.5
CHUNK_WORDS = 80
CHUNK_OVERLAP = 15

INTERFACE_GUARD = r"\b(?:interfaces?|ports?|links?|bvi\d*)\b"
REACHABILITY_GUARD = (
    r"\b(?:pings?|pinging|reach(?:able|ability)?|unreachable|"
    r"respond(?:s|ed|ing)?|repl(?:y|ies)|time[ -]?outs?|timed\s+out)\b"
)
PROTOTYPES = (
    ("interface_flap", "flapping", "The network interface keeps bouncing up and down.", INTERFACE_GUARD),
    ("interface_flap", "flapping", "The connection repeatedly flaps and recovers.", INTERFACE_GUARD),
    ("reachability", "unreachable", "I cannot ping the device; it is unreachable.", REACHABILITY_GUARD),
    ("reachability", "unreachable", "The ping requests time out with no replies.", REACHABILITY_GUARD),
    ("reachability", "reachable", "The device responds to ping and is reachable.", REACHABILITY_GUARD),
    ("reachability", "reachable", "Ping succeeds and we get replies.", REACHABILITY_GUARD),
    ("bgp_state", "down", "The BGP neighbor has dropped and the session is down.", r"\bbgp\b"),
    ("bgp_state", "established", "The BGP peer is established and stable.", r"\bbgp\b"),
    ("alarm_state", "active", "There is an active alarm on the device.", r"\b(?:alarms?|faults?)\b"),
    ("alarm_state", "cleared", "The device alarm has cleared.", r"\b(?:alarms?|faults?)\b"),
)

_model = None
_vectors = None
_unavailable = False
_reported = set()
MODEL_ERRORS = (ImportError, OSError, ValueError, RuntimeError, TypeError)


def _report_once(key, message):
    """Avoid printing the same optional-model failure on every response."""
    if key not in _reported:
        _reported.add(key)
        print(f"\n{message}")


def _load_model():
    global _model, _vectors, _unavailable
    if _unavailable:
        return None
    if _model is not None and _vectors is not None:
        return _model

    app_dir = (
        Path(sys.executable).resolve().parent
        if getattr(sys, "frozen", False)
        else Path(__file__).resolve().parent
    )
    local = next(
        (path for path in (
            app_dir / "bge-small-en-v1.5",
            app_dir.parent / "bge-small-en-v1.5",
        ) if path.is_dir()),
        None,
    )
    source = str(local) if local else MODEL_NAME
    try:
        from sentence_transformers import SentenceTransformer

        # Assign globals only after both initialization steps succeed.
        model = SentenceTransformer(source, device="cpu", local_files_only=True)
        vectors = model.encode(
            [item[2] for item in PROTOTYPES],
            normalize_embeddings=True,
            show_progress_bar=False,
        )
        _model, _vectors = model, vectors
    except MODEL_ERRORS as exc:
        _unavailable = True
        _report_once(
            "load",
            f"BGE interpretation unavailable locally ({type(exc).__name__}): "
            f"{exc}. Explicit measurements will still be recorded.",
        )
        return None

    print("\nBGE agent-phrasing interpretation enabled.")
    return _model


def _response_chunks(response):
    """Cover the whole response in short passages, without a character cutoff.

    Word windows reduce model truncation; unusual token-heavy text may still
    exceed the model's token limit. Overlap preserves some boundary context.
    """
    for passage in re.split(r"\n+|(?<=[.!?])\s+", response):
        words = passage.split()
        for start in range(0, len(words), CHUNK_WORDS - CHUNK_OVERLAP):
            chunk = " ".join(words[start:start + CHUNK_WORDS])
            if chunk:
                yield chunk
            if start + CHUNK_WORDS >= len(words):
                break


def interpret_phrasing(response, available_types, existing_type_ids=()):
    """Return at most one tentative text meaning per observation type.

    Competing labels are compared within the same type. If different passages
    confidently suggest conflicting labels, omit that type rather than choose
    a device or assume which statement is current.
    """
    if not response or response.startswith("[Skipped]"):
        return []
    if len(re.findall(r"[A-Za-z]+", response)) < 3:
        return []

    existing = set(existing_type_ids)
    eligible = [
        (index, key, value, guard)
        for index, (key, value, _, guard) in enumerate(PROTOTYPES)
        if key in available_types
        and available_types[key]["active"]
        and available_types[key]["data_type"] == "text"
        and available_types[key]["observation_type_id"] not in existing
    ]
    passages = [
        (chunk, [item for item in eligible if re.search(item[3], chunk, re.I)])
        for chunk in _response_chunks(response)
    ]
    passages = [(chunk, items) for chunk, items in passages if items]
    if not passages:
        return []
    model = _load_model()
    if model is None or _vectors is None:
        return []

    proposed = {}
    try:
        for chunk, candidates in passages:
            query = model.encode(
                QUERY_PREFIX + chunk,
                normalize_embeddings=True,
                show_progress_bar=False,
            )
            scores = {}
            for index, key, value, _ in candidates:
                similarity = float(_vectors[index] @ query)
                if not math.isfinite(similarity):
                    raise ValueError("Model returned a non-finite similarity")
                labels = scores.setdefault(key, {})
                labels[value] = max(labels.get(value, -1.0), similarity)

            for key, labels in scores.items():
                ordered = sorted(labels.items(), key=lambda item: item[1], reverse=True)
                value, best = ordered[0]
                # Single-label types have no competing class: margin is not
                # evidence of certainty and only the score threshold applies.
                runner_up = ordered[1][1] if len(ordered) > 1 else None
                if best < MIN_SIMILARITY:
                    continue
                if runner_up is not None and best - runner_up < MIN_MARGIN:
                    continue
                meanings = proposed.setdefault(key, {})
                if value not in meanings or best > meanings[value][0]:
                    meanings[value] = (best, chunk)
    except MODEL_ERRORS as exc:
        _report_once(
            "encode",
            f"BGE interpretation failed ({type(exc).__name__}): {exc}. "
            "No model suggestions were saved for this response; "
            "explicit measurements are unaffected.",
        )
        return []

    suggestions = []
    for key, meanings in proposed.items():
        if len(meanings) != 1:
            continue
        value, (similarity, evidence) = next(iter(meanings.items()))
        definition = available_types[key]
        suggestions.append({
            "observation_type_id": definition["observation_type_id"],
            "display_name": definition["display_name"],
            "value_text": value,
            "value_number": None,
            "unit": None,
            "confidence": TENTATIVE_CONFIDENCE,
            # Extra metadata is available to callers; current DB save code
            # ignores these fields. Similarity is not a probability.
            "similarity_score": similarity,
            "evidence_text": evidence,
        })
    return suggestions
