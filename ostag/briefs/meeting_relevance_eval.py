"""OT-2 offline synthetic evaluation. No production imports, IO clients or authority.

Hashes bind declared inputs; they neither authenticate provenance nor establish
independent human labels. Python sorted JSON is versioned here, not shared JCS.
"""
from __future__ import annotations

from collections import Counter
import hashlib
import json
import sys

MAX_BYTES = 1024 * 1024
SPLITS = ("development", "holdout")
RELEVANCE = ("relevant", "irrelevant")
DUPLICATE = ("duplicate", "distinct")


class ContractError(ValueError):
    """Invalid evaluation input; never include source text in errors."""


def _require(condition):
    if not condition:
        raise ContractError("invalid meeting evaluation input")


def _keys(value, names):
    _require(type(value) is dict and set(value) == set(names.split()))


def _text(value, maximum=4096):
    _require(type(value) is str and bool(value.strip()))
    try:
        _require(len(value.encode("utf-8")) <= maximum)
    except UnicodeError:
        raise ContractError("invalid meeting evaluation input") from None
    return value


def _list(value, maximum):
    _require(type(value) is list and len(value) <= maximum)
    return value


def _bytes(value):
    return json.dumps(value, sort_keys=True, ensure_ascii=False,
                      separators=(",", ":"), allow_nan=False).encode("utf-8")


def digest(value):
    return hashlib.sha256(_bytes(value)).hexdigest()


def _normal(text):
    return " ".join(text.casefold().split())


def _validate_suite(suite, *, with_gold=True):
    _keys(suite, "version provenance cases")
    kind = "suite" if with_gold else "input"
    _require(suite["version"] == f"meeting-relevance-{kind}/1")
    _require(suite["provenance"] == "synthetic")
    cases = _list(suite["cases"], 100)
    _require(bool(cases))
    seen, partitions = set(), {}
    for case in cases:
        _keys(case, "id family split topic terms materials" + (" gold" if with_gold else ""))
        cid = _text(case["id"], 120)
        _require(cid not in seen)
        seen.add(cid)
        family = _text(case["family"], 120)
        split = case["split"]
        _require(split in SPLITS)
        _text(case["topic"], 1000)
        terms = _list(case["terms"], 16)
        _require(bool(terms))
        for term in terms:
            _text(term, 100)
        _require(len({_normal(t) for t in terms}) == len(terms))
        materials = _list(case["materials"], 2)
        _require(len(materials) == 2)
        source_ids, identities = set(), [("family", family)]
        for material in materials:
            _keys(material, "id origin text")
            mid = _text(material["id"], 120)
            _require(mid not in source_ids)
            source_ids.add(mid)
            text = _text(material["text"])
            identities.append(("text", _normal(text)))
            if material["origin"] is not None:
                identities.append(("origin", _text(material["origin"], 120)))
        for identity in identities:
            _require(identity not in partitions or partitions[identity] == split)
            partitions[identity] = split
        if with_gold:
            gold = case["gold"]
            _keys(gold, "relevance duplicate")
            _require(len(_list(gold["relevance"], 2)) == 2)
            _require(all(label in (*RELEVANCE, "uncertain") for label in gold["relevance"]))
            _require(gold["duplicate"] in (*DUPLICATE, "uncertain"))


def input_snapshot(suite):
    """Copy inputs without gold fields; callers must audit other semantic cues."""
    _validate_suite(suite)
    return {"version": "meeting-relevance-input/1", "provenance": "synthetic", "cases": [
        {**{k: case[k] for k in ("id", "family", "split", "topic")},
         "terms": list(case["terms"]), "materials": [dict(m) for m in case["materials"]]}
        for case in suite["cases"]]}


def baseline(snapshot):
    """Literal topic mentions and same-origin normalized copies; otherwise abstain.

    Relevance means a lexical topic mention, never claim support/approval.
    Paraphrases, unknown origins and differing texts require semantic review.
    """
    _validate_suite(snapshot, with_gold=False)
    rows = []
    for case in snapshot["cases"]:
        left, right = case["materials"]
        duplicate = (left["origin"] is not None and left["origin"] == right["origin"]
                     and _normal(left["text"]) == _normal(right["text"]))
        rows.append({"id": case["id"], "relevance": [
            "relevant" if any(_normal(t) in _normal(m["text"]) for t in case["terms"])
            else "abstain" for m in case["materials"]],
            "duplicate": "duplicate" if duplicate else "abstain"})
    return {"version": "meeting-relevance-predictions/1", "method": "literal-v1",
            "input_sha256": digest(snapshot), "cases": rows}


def _predictions(artifact, snapshot):
    _keys(artifact, "version method input_sha256 cases")
    _require(artifact["version"] == "meeting-relevance-predictions/1")
    _text(artifact["method"], 120)
    _require(artifact["input_sha256"] == digest(snapshot))
    known = {case["id"] for case in snapshot["cases"]}
    rows = {}
    for row in _list(artifact["cases"], 100):
        _keys(row, "id relevance duplicate")
        cid = _text(row["id"], 120)
        _require(cid in known and cid not in rows)
        _require(len(_list(row["relevance"], 2)) == 2)
        _require(all(label in (*RELEVANCE, "abstain") for label in row["relevance"]))
        _require(row["duplicate"] in (*DUPLICATE, "abstain"))
        rows[cid] = row
    return rows


def _metrics(pairs, positive):
    counts = Counter()
    for reference, predicted in pairs:
        counts["total"] += 1
        counts["not_run"] += predicted is None
        counts["abstained"] += predicted == "abstain"
        if reference == "uncertain":
            counts["reference_uncertain"] += 1
            counts["decisive_on_uncertain"] += predicted not in (None, "abstain")
            continue
        counts["known_reference"] += 1
        counts["positive_reference"] += reference == positive
        if predicted in (None, "abstain"):
            continue
        counts["scored"] += 1
        counts["correct"] += reference == predicted
        counts["true_positive"] += reference == predicted == positive
        counts["false_positive"] += predicted == positive and reference != positive
        counts["false_negative"] += reference == positive and predicted != positive
    fields = ("total not_run abstained reference_uncertain decisive_on_uncertain known_reference "
              "positive_reference scored correct true_positive false_positive false_negative")
    result = {field: counts[field] for field in fields.split()}
    # Fractions retain zero denominators rather than fabricating 0% or 100%.
    for name, numerator, denominator in (
        ("precision", counts["true_positive"], counts["true_positive"] + counts["false_positive"]),
        ("recall", counts["true_positive"], counts["positive_reference"]),
        ("coverage", counts["scored"], counts["known_reference"]),
        ("accuracy_on_decided", counts["correct"], counts["scored"]),
    ):
        result[name] = {"numerator": numerator, "denominator": denominator}
    return result


def _score(suite, rows):
    result = {}
    for split in SPLITS:
        cases = [c for c in suite["cases"] if c["split"] == split]
        relevance, duplicate = [], []
        for case in cases:
            row = rows.get(case["id"])
            relevance.extend(zip(case["gold"]["relevance"],
                                 row["relevance"] if row else (None, None)))
            duplicate.append((case["gold"]["duplicate"], row["duplicate"] if row else None))
        result[split] = {"cases": len(cases), "relevance": _metrics(relevance, "relevant"),
                         "duplicate": _metrics(duplicate, "duplicate")}
    return result


def evaluate(suite, candidate=None):
    snapshot = input_snapshot(suite)
    reference = baseline(snapshot)
    base_rows = _predictions(reference, snapshot)
    candidate_rows = _predictions(candidate, snapshot) if candidate is not None else None
    return {
        "version": "meeting-relevance-report/1", "suite_sha256": digest(suite),
        "input_sha256": digest(snapshot), "baseline_sha256": digest(reference),
        "candidate_sha256": digest(candidate) if candidate is not None else None,
        "baseline_method": reference["method"],
        "candidate_method_declared": candidate["method"] if candidate is not None else None,
        "baseline": _score(suite, base_rows),
        "candidate": _score(suite, candidate_rows) if candidate_rows is not None else None,
        "cases": [{"id": case["id"], "split": case["split"],
                   "source_refs": [{"id": m["id"], "text_sha256": hashlib.sha256(
                       m["text"].encode("utf-8")).hexdigest()} for m in case["materials"]],
                   "reference": dict(case["gold"]), "baseline": base_rows[case["id"]],
                   "candidate": candidate_rows.get(case["id"])
                   if candidate_rows is not None else None} for case in suite["cases"]],
        "acceptance_evaluated": False, "live_model_evaluated": False, "holdout_sealed": False,
        "network_client_present": False, "network_activity_measured": False,
        "conclusion": "inconclusive_synthetic_only",
        "limitations": ["Synthetic engineering examples, not independent human annotations.",
                        "Holdout has published reference labels, not a sealed or blinded test.",
                        "Declared family/origin and exact-text leakage checks cannot prove independence.",
                        "Candidate method and synthetic provenance are operator declarations.",
                        "No model execution, cost, time saved or production benefit is measured.",
                        "Relevance is not factual support; duplicate advice never removes material."],
    }


def _object(pairs):
    result = {}
    for key, value in pairs:
        _require(key not in result)
        result[key] = value
    return result


def parse_input(raw):
    try:
        _require(type(raw) is bytes and len(raw) <= MAX_BYTES)
        value = json.loads(raw.decode("utf-8"), object_pairs_hook=_object,
                           parse_constant=lambda _: _require(False))
        pending = [(value, 0)]
        while pending:
            item, depth = pending.pop()
            if type(item) in (dict, list):
                _require(depth < 16)
                children = item.values() if type(item) is dict else item
                pending.extend((child, depth + 1) for child in children)
        _bytes(value)  # Reject lone surrogates and non-finite numbers, including 1e999.
        return value
    except (ValueError, TypeError, UnicodeError, RecursionError):
        raise ContractError("invalid meeting evaluation input") from None


def main(argv=None):
    try:
        args = sys.argv[1:] if argv is None else argv
        _require(args in ([], ["--snapshot"], ["--baseline"]))
        value = parse_input(sys.stdin.buffer.read(MAX_BYTES + 1))
        _keys(value, "suite candidate")
        if args:
            _require(value["candidate"] is None)
            snapshot = input_snapshot(value["suite"])
            result = snapshot if args == ["--snapshot"] else baseline(snapshot)
        else:
            result = evaluate(value["suite"], value["candidate"])
        encoded = _bytes(result) + b"\n"
    except (ValueError, TypeError, KeyError, RecursionError, UnicodeError):
        print("Invalid meeting evaluation input", file=sys.stderr)
        return 2
    try:
        sys.stdout.buffer.write(encoded)
        sys.stdout.buffer.flush()
    except OSError:
        # Close a failed pipe so interpreter shutdown does not retry its buffer.
        try:
            sys.stdout.close()
        except OSError:
            pass
        print("Unable to write meeting evaluation output", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
