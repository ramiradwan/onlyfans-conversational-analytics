"""Lossless scalar transport records for an immutable exchange generation."""

from datetime import datetime, timezone

from app.analytics.exchange_codec import (
    ExchangeInvalid, canonical_bytes, content_digest, decode_exchange, decode_value, validate_exchange,
)


def instant_us(at):
    delta = at - datetime(1970, 1, 1, tzinfo=timezone.utc)
    return delta.days * 86400000000 + delta.seconds * 1000000 + delta.microseconds


def exchange_records(bundle):
    bundle = validate_exchange(bundle)
    document = bundle.model_dump(mode="json")
    artifact = document.pop("artifact")
    facts = document.pop("facts")
    projection = artifact["projection"]
    enrichments = projection.pop("message_enrichments")
    metrics = projection.pop("conversation_metrics")
    document["projection"] = projection
    document["facts_schema"] = facts["schema_version"]
    records = []

    def add(logical_id, label, value, index=0, **fields):
        records.append({"logical_id": logical_id, "label": label,
            "record_json": canonical_bytes(value).decode("utf-8"), "fields": {"record_index": index, **fields}})

    add("header", "exchange_header", document)
    for index, item in enumerate(enrichments):
        add("enrichment:" + item["message_ref"], "exchange_enrichment", item, index)
    for index, item in enumerate(metrics):
        add("metric:" + item["conversation_ref"], "exchange_metric", item, index)
    for item in artifact["nodes"]:
        add("node:" + item["node_id"], item["kind"], item)
    for item in artifact["edges"]:
        add("edge:" + item["edge_id"], item["relation"], item)
        records[-1].update(source_id="node:" + item["source_id"], target_id="node:" + item["target_id"])
    for item in facts["conversations"]:
        add("conversation:" + item["conversation_ref"], "exchange_conversation", item)
    for item, typed in zip(facts["observations"], bundle.facts.observations, strict=True):
        add("observation:" + item["evidence"]["message_ref"], "exchange_observation", item,
            sent_us=instant_us(typed.evidence.sent_at), conversation_ref=typed.evidence.conversation_ref)
    return sorted(records, key=lambda r: r["logical_id"])


def records_digest(records):
    return content_digest(records)


def recover_exchange(records):
    headers = [r for r in records if r["logical_id"] == "header"]
    if len(headers) != 1 or len({r["logical_id"] for r in records}) != len(records):
        raise ExchangeInvalid("analytics_exchange_records")
    try:
        header = decode_value(headers[0]["record_json"])
        def values(prefix, *, ordered=False):
            selected = [r for r in records if r["logical_id"].startswith(prefix)]
            selected.sort(key=(lambda r: r["fields"]["record_index"]) if ordered else lambda r: r["logical_id"])
            if ordered and [r["fields"]["record_index"] for r in selected] != list(range(len(selected))):
                raise ExchangeInvalid("analytics_exchange_record_order")
            return [decode_value(r["record_json"]) for r in selected]
        projection = header.pop("projection")
        projection.update(message_enrichments=values("enrichment:", ordered=True),
                          conversation_metrics=values("metric:", ordered=True))
        header["artifact"] = {"projection": projection, "nodes": values("node:"), "edges": values("edge:")}
        header["facts"] = {"schema_version": header.pop("facts_schema"),
                           "conversations": values("conversation:"), "observations": values("observation:")}
        bundle = decode_exchange(canonical_bytes(header))
        if exchange_records(bundle) != sorted(records, key=lambda r: r["logical_id"]):
            raise ExchangeInvalid("analytics_exchange_record_binding")
        return bundle
    except ExchangeInvalid:
        raise
    except (ValueError, TypeError, KeyError):
        raise ExchangeInvalid("analytics_exchange_records") from None
