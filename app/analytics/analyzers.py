"""Narrow analyzer ports and dependency-free deterministic implementations."""

from __future__ import annotations

import re
from typing import Protocol, runtime_checkable

from app.analytics.provenance import stable_config_digest
from app.analytics.opaque_refs import entity_ref, topic_ref
from app.models.analytics import (
    AnalysisMode,
    CalibrationStatus,
    EngagementResult,
    EngagementState,
    EntityMention,
    EntityType,
    MessageAnalysisInput,
    SentimentLabel,
    SentimentResult,
    TopicEntityResult,
    TopicMention,
)


@runtime_checkable
class SentimentAnalyzer(Protocol):
    """Analyze one message without persistence or network side effects."""

    name: str
    revision: str
    config_digest: str
    mode: AnalysisMode
    calibration_status: CalibrationStatus

    def analyze(self, message: MessageAnalysisInput) -> SentimentResult: ...


@runtime_checkable
class TopicEntityAnalyzer(Protocol):
    """Extract topics and explicit entities from one message."""

    name: str
    revision: str
    config_digest: str
    mode: AnalysisMode
    calibration_status: CalibrationStatus

    def analyze(self, message: MessageAnalysisInput) -> TopicEntityResult: ...


@runtime_checkable
class EngagementAnalyzer(Protocol):
    """Classify the observable engagement function of one message."""

    name: str
    revision: str
    config_digest: str
    mode: AnalysisMode
    calibration_status: CalibrationStatus

    def analyze(self, message: MessageAnalysisInput) -> EngagementResult: ...


_TOKEN_RE = re.compile(r"[\w']+", flags=re.UNICODE)


class RuleBasedSentimentAnalyzer:
    """Small lexicon baseline intended to be replaced by a model adapter later."""

    name = "rule_based_sentiment"
    revision = "sentiment.rules.v1"
    mode = AnalysisMode.BASELINE
    calibration_status = CalibrationStatus.NOT_CALIBRATED
    positive_terms = frozenset(
        {
            "appreciate",
            "excellent",
            "glad",
            "good",
            "great",
            "happy",
            "helpful",
            "resolved",
            "thanks",
            "thank",
            "welcome",
            "yes",
        }
    )
    negative_terms = frozenset(
        {
            "cancel",
            "confused",
            "delay",
            "delayed",
            "error",
            "issue",
            "problem",
            "sorry",
            "unhappy",
            "upset",
            "wrong",
        }
    )
    negators = frozenset({"hardly", "never", "no", "not"})
    config_digest = stable_config_digest(
        name=name,
        revision=revision,
        config={
            "positive_terms": sorted(positive_terms),
            "negative_terms": sorted(negative_terms),
            "negators": sorted(negators),
            "positive_threshold": 0.15,
            "negative_threshold": -0.15,
        },
    )

    def analyze(self, message: MessageAnalysisInput) -> SentimentResult:
        tokens = [token.lower() for token in _TOKEN_RE.findall(message.text)]
        total = 0
        hits = 0
        evidence: list[str] = []
        for index, token in enumerate(tokens):
            polarity = 0
            if token in self.positive_terms:
                polarity = 1
            elif token in self.negative_terms:
                polarity = -1
            if polarity == 0:
                continue
            negated = index > 0 and tokens[index - 1] in self.negators
            if negated:
                polarity *= -1
            total += polarity
            hits += 1
            prefix = "negated" if negated else (
                "positive" if polarity > 0 else "negative"
            )
            evidence.append(f"{prefix}:{token}")

        score = 0.0 if hits == 0 else round(total / hits, 6)
        if score > 0.15:
            label = SentimentLabel.POSITIVE
        elif score < -0.15:
            label = SentimentLabel.NEGATIVE
        else:
            label = SentimentLabel.NEUTRAL
        confidence = 0.35 if hits == 0 else min(0.95, 0.45 + hits * 0.1)
        return SentimentResult(
            label=label,
            score=score,
            confidence=round(confidence, 6),
            evidence_count=len(evidence),
            analyzer_name=self.name,
            analyzer_revision=self.revision,
            analyzer_config_digest=self.config_digest,
            analysis_mode=self.mode,
            calibration_status=self.calibration_status,
        )


class RuleBasedTopicEntityAnalyzer:
    """Keyword taxonomy plus explicit mention/URL/amount/hashtag extraction."""

    name = "rule_based_topics_entities"
    revision = "topics-entities.rules.v1"
    mode = AnalysisMode.BASELINE
    calibration_status = CalibrationStatus.NOT_CALIBRATED
    topic_terms: tuple[tuple[str, str, frozenset[str]], ...] = (
        (
            "feedback",
            "Feedback",
            frozenset({"appreciate", "feedback", "great", "helpful", "thanks"}),
        ),
        (
            "greeting",
            "Greeting",
            frozenset({"hello", "hey", "hi", "welcome"}),
        ),
        (
            "media",
            "Media",
            frozenset({"file", "image", "link", "media", "photo", "upload", "video"}),
        ),
        (
            "pricing",
            "Pricing",
            frozenset({"budget", "cost", "payment", "price", "tip"}),
        ),
        (
            "scheduling",
            "Scheduling",
            frozenset(
                {"available", "calendar", "schedule", "time", "today", "tomorrow"}
            ),
        ),
        (
            "support",
            "Support",
            frozenset({"error", "help", "issue", "problem", "resolve", "support"}),
        ),
    )
    entity_patterns: tuple[tuple[EntityType, re.Pattern[str]], ...] = (
        (EntityType.URL, re.compile(r"https?://[^\s]+", flags=re.IGNORECASE)),
        (EntityType.MENTION, re.compile(r"(?<!\w)@[A-Za-z0-9_]+")),
        (EntityType.HASHTAG, re.compile(r"(?<!\w)#[A-Za-z0-9_]+")),
        (
            EntityType.AMOUNT,
            re.compile(r"(?:[$€£]\s?\d+(?:[.,]\d{1,2})?|\d+(?:[.,]\d{1,2})?\s?(?:usd|eur|gbp))", flags=re.IGNORECASE),
        ),
    )
    config_digest = stable_config_digest(
        name=name,
        revision=revision,
        config={
            "topic_terms": [
                [topic_id, label, sorted(terms)]
                for topic_id, label, terms in topic_terms
            ],
            "entity_patterns": [
                [entity_type.value, pattern.pattern, pattern.flags]
                for entity_type, pattern in entity_patterns
            ],
        },
    )

    def analyze(self, message: MessageAnalysisInput) -> TopicEntityResult:
        tokens = [token.lower() for token in _TOKEN_RE.findall(message.text)]
        token_set = set(tokens)
        topics: list[TopicMention] = []
        for topic_id, label, terms in self.topic_terms:
            evidence = sorted(token_set.intersection(terms))
            if not evidence:
                continue
            confidence = min(0.95, 0.55 + 0.1 * len(evidence))
            topics.append(
                TopicMention(
                    topic_ref=topic_ref(message.creator_account_id, topic_id),
                    taxonomy_id=topic_id,
                    label=label,
                    confidence=round(confidence, 6),
                    evidence_count=len(evidence),
                )
            )

        entity_matches: list[tuple[int, int, str, EntityMention]] = []
        seen: set[tuple[EntityType, int, int, str]] = set()
        for entity_type, pattern in self.entity_patterns:
            for match in pattern.finditer(message.text):
                value = match.group(0)
                if entity_type == EntityType.URL:
                    value = value.rstrip(".,;:!?)\"]}")
                end_offset = match.start() + len(value)
                normalized = value.casefold().replace(" ", "")
                identity = (entity_type, match.start(), end_offset, normalized)
                if identity in seen:
                    continue
                seen.add(identity)
                entity_matches.append(
                    (
                        match.start(),
                        end_offset,
                        normalized,
                        EntityMention(
                            entity_ref=entity_ref(
                                message.creator_account_id,
                                entity_type.value,
                                normalized,
                            ),
                            entity_type=entity_type,
                            confidence=1.0,
                        ),
                    )
                )
        entity_matches.sort(
            key=lambda item: (item[0], item[1], item[3].entity_type.value, item[2])
        )
        return TopicEntityResult(
            topics=topics,
            entities=[item[3] for item in entity_matches],
            analyzer_name=self.name,
            analyzer_revision=self.revision,
            analyzer_config_digest=self.config_digest,
            analysis_mode=self.mode,
            calibration_status=self.calibration_status,
        )


class RuleBasedEngagementAnalyzer:
    """Observable message-function classifier with documented lexical rules."""

    name = "rule_based_engagement"
    revision = "engagement.rules.v1"
    mode = AnalysisMode.BASELINE
    calibration_status = CalibrationStatus.NOT_CALIBRATED
    signal_terms: tuple[tuple[EngagementState, frozenset[str]], ...] = (
        (
            EngagementState.CONSTRAINT,
            frozenset({"cannot", "can't", "limit", "restricted", "unavailable"}),
        ),
        (
            EngagementState.TRANSACTIONAL,
            frozenset({"budget", "cost", "payment", "price", "tip"}),
        ),
        (
            EngagementState.COORDINATION,
            frozenset({"available", "calendar", "schedule", "time", "tomorrow"}),
        ),
        (
            EngagementState.ACKNOWLEDGEMENT,
            frozenset({"appreciate", "got", "okay", "thanks", "thank"}),
        ),
        (
            EngagementState.COMMITMENT,
            frozenset({"confirm", "send", "will"}),
        ),
    )
    question_terms = frozenset({"can", "could", "how", "what", "when", "where", "who", "why", "would"})
    config_digest = stable_config_digest(
        name=name,
        revision=revision,
        config={
            "signal_terms": [
                [state.value, sorted(terms)] for state, terms in signal_terms
            ],
            "question_terms": sorted(question_terms),
            "amount_pattern": (
                r"(?:[$€£]\s?\d+(?:[.,]\d{1,2})?|"
                r"\d+(?:[.,]\d{1,2})?\s?(?:usd|eur|gbp))"
            ),
        },
    )

    def analyze(self, message: MessageAnalysisInput) -> EngagementResult:
        text = message.text.strip()
        if not text:
            return EngagementResult(
                state=EngagementState.MINIMAL,
                confidence=1.0,
                signal_count=1,
                analyzer_name=self.name,
                analyzer_revision=self.revision,
                analyzer_config_digest=self.config_digest,
                analysis_mode=self.mode,
                calibration_status=self.calibration_status,
            )
        tokens = [token.lower() for token in _TOKEN_RE.findall(text)]
        token_set = set(tokens)
        if re.search(
            r"(?:[$€£]\s?\d+(?:[.,]\d{1,2})?|\d+(?:[.,]\d{1,2})?\s?(?:usd|eur|gbp))",
            text,
            flags=re.IGNORECASE,
        ):
            return EngagementResult(
                state=EngagementState.TRANSACTIONAL,
                confidence=0.85,
                signal_count=1,
                analyzer_name=self.name,
                analyzer_revision=self.revision,
                analyzer_config_digest=self.config_digest,
                analysis_mode=self.mode,
                calibration_status=self.calibration_status,
            )
        for state, terms in self.signal_terms:
            signals = sorted(token_set.intersection(terms))
            if signals:
                return EngagementResult(
                    state=state,
                    confidence=round(min(0.95, 0.65 + 0.05 * len(signals)), 6),
                    signal_count=len(signals),
                    analyzer_name=self.name,
                    analyzer_revision=self.revision,
                    analyzer_config_digest=self.config_digest,
                    analysis_mode=self.mode,
                    calibration_status=self.calibration_status,
                )
        question_signals = sorted(token_set.intersection(self.question_terms))
        if "?" in text or (tokens and tokens[0] in self.question_terms):
            signals = (["question_mark"] if "?" in text else []) + question_signals
            return EngagementResult(
                state=EngagementState.INQUIRY,
                confidence=0.85,
                signal_count=len(dict.fromkeys(signals)),
                analyzer_name=self.name,
                analyzer_revision=self.revision,
                analyzer_config_digest=self.config_digest,
                analysis_mode=self.mode,
                calibration_status=self.calibration_status,
            )
        return EngagementResult(
            state=EngagementState.INFORMATION,
            confidence=0.55,
            signal_count=1,
            analyzer_name=self.name,
            analyzer_revision=self.revision,
            analyzer_config_digest=self.config_digest,
            analysis_mode=self.mode,
            calibration_status=self.calibration_status,
        )
