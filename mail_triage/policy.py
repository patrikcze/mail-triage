"""Laya questions and the policy that turns its answers plus header evidence into a decision.

Standard library only. The model never acts alone: its junk probability is fused with header
evidence in log-odds space, guards keep receipts and notices out of junk, and only a
"highly likely" result becomes junk.
"""

from __future__ import annotations

from dataclasses import dataclass
import math

from .config import COARSE, Config


# Lower bound of each band, highest first.
BANDS = ((0.90, "highly likely"), (0.70, "likely"), (0.30, "uncertain"),
         (0.10, "unlikely"), (0.0, "highly unlikely"))
# When no model fluent in the email's language is available, its vote counts for less.
NON_FLUENT_WEIGHT = 0.4
# The multilingual checkpoint collapses on long choice lists but is reliable on these three.
COARSE_LABELS = {
    "personal": "personal or work message",
    "transactional": "receipt, bill or account notice",
    "marketing": "marketing or newsletter",
}
EVIDENCE = ("list_unsubscribe", "bulk", "esp", "noreply", "automated", "auth_fail")
BULK = ("list_unsubscribe", "bulk", "esp")


@dataclass(frozen=True)
class Decision:
    verdict: str            # keep | review | junk
    category: str           # category name, or "review"
    folder: str             # planned destination ("" = stays where it is)
    junk_probability: float
    likelihood: str
    reasons: tuple[str, ...]


def likelihood(p: float) -> str:
    for bound, label in BANDS:
        if p >= bound:
            return label
    return BANDS[-1][1]


def _logit(p: float) -> float:
    p = min(max(p, 0.01), 0.99)
    return math.log(p / (1 - p))


def _sigmoid(x: float) -> float:
    return 1 / (1 + math.exp(-x))


def questions(config: Config, multilingual: bool = False) -> dict:
    """Typed questions in the system-one format shared by laya-mlx, llama.cpp and Ollama."""
    ins = config.instructions
    if multilingual:
        coarse = [c for c in COARSE if any(cat.coarse == c for cat in config.categories)]
        kind = {"type": "choice", "instructions": ins["kind"], "criteria": [COARSE_LABELS[c] for c in coarse]}
    else:
        kind = {"type": "choice", "instructions": ins["kind"],
                "criteria": {c.name: c.description for c in config.categories}}
    return {
        "kind": kind,
        "is_marketing": {"type": "noul", "instructions": ins["is_marketing"]},
        "is_spam": {"type": "noul", "instructions": ins["is_spam"]},
        "is_phishing": {"type": "noul", "instructions": ins["is_phishing"]},
    }


def kind_probabilities(config: Config, answers: dict) -> dict[str, float]:
    """Category probabilities; multilingual coarse labels go to the first category of each bucket."""
    raw = {str(k): float(v) for k, v in answers["kind"]["probabilities"].items()}
    names = config.names()
    if set(raw) <= set(names):
        return {name: raw.get(name, 0.0) for name in names}
    by_label = {label: coarse for coarse, label in COARSE_LABELS.items()}
    mapped = dict.fromkeys(names, 0.0)
    for label, value in raw.items():
        coarse = by_label.get(label)
        target = next((c.name for c in config.categories if c.coarse == coarse), None)
        if target is None:
            raise ValueError(f"model returned unknown kind {label!r}")
        mapped[target] += value
    return mapped


def apply_temperature(answers: dict, temperature: float) -> dict:
    """Temperature scaling of the returned probabilities (1.0 = unchanged)."""
    if temperature == 1.0:
        return answers
    scaled = {}
    for qid, answer in answers.items():
        answer = dict(answer)
        if "probabilities" in answer and answer.get("type", "choice") == "choice":
            logs = {k: math.log(max(float(v), 1e-9)) / temperature for k, v in answer["probabilities"].items()}
            top = max(logs.values())
            exp = {k: math.exp(v - top) for k, v in logs.items()}
            total = sum(exp.values())
            answer["probabilities"] = {k: v / total for k, v in exp.items()}
        if "noul" in answer:
            answer["noul"] = _sigmoid(_logit(float(answer["noul"])) / temperature)
        scaled[qid] = answer
    return scaled


def _best(kinds: dict[str, float], config: Config, action: str, exclude: str = "") -> str:
    names = [n for n in config.names(action) if n != exclude] or config.names(action)
    return max(names, key=lambda n: kinds.get(n, 0.0))


def decide(config: Config, answers: dict, signals: dict, *, fluent: bool = True) -> Decision:
    """Fuse model answers and header signals into keep / review / junk with a category."""
    weights = config.policy["weights"]
    answers = apply_temperature(answers, float(config.model["temperature"]))
    kinds = kind_probabilities(config, answers)
    top = max(kinds, key=kinds.get)
    junk_kind = sum(kinds[n] for n in config.names("junk"))
    marketing = float(answers["is_marketing"]["noul"])
    spam = float(answers["is_spam"]["noul"])
    phishing = float(answers["is_phishing"]["noul"])

    model_p = 0.5 * junk_kind + 0.5 * max(marketing, spam)
    score = _logit(model_p) * (1.0 if fluent else NON_FLUENT_WEIGHT)
    reasons = [f"model: {top} {kinds[top]:.2f}, marketing {marketing:.2f}, spam {spam:.2f}"]
    if not fluent:
        reasons.append("language not supported by model: weight reduced")
    for name in EVIDENCE:
        if signals.get(name):
            score += weights.get(name, 0.0)
            reasons.append(f"header: {name}")
    commercial = bool(signals.get("commercial_marker"))
    if commercial:
        score += weights.get("commercial_marker", 0.0)
        reasons.append("marked commercial (OS)")
    bulk_evidence = commercial or any(signals.get(name) for name in BULK)
    if signals.get("is_reply") and not bulk_evidence:
        score += weights.get("conversation", 0.0)
        reasons.append("conversation reply")
    # A sender that legally declares the message commercial outranks the subject guards.
    transactional = bool(signals.get("transactional")) and not commercial
    notice = bool(signals.get("notice")) and not commercial
    if transactional:
        score += weights.get("transactional", 0.0)
        reasons.append("transactional subject")
    if notice:
        score += weights.get("notice", 0.0)
        reasons.append("contract/legal/account-action subject")
    if signals.get("account_sender"):
        score += weights.get("account_sender", 0.0)
        reasons.append("account/billing sender")
    personal_like = {c.name for c in config.categories if c.coarse == "personal"} | {"personal", "work"}
    if top in personal_like and kinds[top] >= 0.6:
        score += weights.get("personal", 0.0)
    p = _sigmoid(score)

    verdict = ("junk" if p >= config.policy["junk_threshold"]
               else "review" if p >= config.policy["review_threshold"] else "keep")
    caps = []
    # The models alone over-call automated mail as marketing (receipts, bank notices),
    # so junk needs the headers bulk senders are required to send.
    if not bulk_evidence:
        caps.append("no bulk-sender headers")
    transactional_names = {c.name for c in config.categories if c.coarse == "transactional"}
    model_transactional = sum(kinds[n] for n in transactional_names) >= 0.5 and not commercial
    if model_transactional or transactional or signals.get("account_sender"):
        caps.append("looks transactional")
    if notice:
        caps.append("contract or account notice")
    if verdict == "junk" and caps:
        verdict = "review"
        reasons.extend(f"capped: {cap}" for cap in caps)

    if verdict == "junk":
        scam = config.policy.get("scam_category", "")
        category = _best(kinds, config, "junk")
        if category == scam and bulk_evidence and not signals.get("auth_fail"):
            # Authenticated bulk mail with unsubscribe headers is marketing, not a scam.
            category = _best(kinds, config, "junk", exclude=scam)
        elif phishing >= 0.9 and category == scam:
            reasons.append(f"possible phishing {phishing:.2f}")
        folder = config.category(category).folder
    elif verdict == "review":
        category, folder = "review", config.policy["review_folder"]
        if phishing >= 0.9 and (not bulk_evidence or signals.get("auth_fail")):
            reasons.append(f"possible phishing {phishing:.2f}")
    else:
        category = max(config.names("keep") + config.names("review"), key=lambda n: kinds.get(n, 0.0))
        folder = config.category(category).folder
    return Decision(verdict, category, folder, round(p, 4), likelihood(p), tuple(reasons))


def decide_category(config: Config, category: str, reason: str, likelihood_label: str = "rule") -> Decision:
    """Decision for a rule, a taught sender, or sender memory: the category's own action."""
    if category == "review":
        return Decision("review", "review", config.policy["review_folder"], 0.0, likelihood_label, (reason,))
    cat = config.category(category)
    folder = config.policy["review_folder"] if cat.action == "review" and not cat.folder else cat.folder
    return Decision(cat.action, cat.name, folder, 1.0 if cat.action == "junk" else 0.0, likelihood_label, (reason,))
