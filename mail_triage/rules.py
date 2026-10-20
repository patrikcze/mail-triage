"""User rules from config.toml: decide instantly, without fetching the message or asking the model."""

from __future__ import annotations

import re

from .config import Config, Rule


def _domain_matches(address: str, domain: str) -> bool:
    host = address.rpartition("@")[2].lower()
    domain = domain.lower().lstrip("@")
    return host == domain or host.endswith("." + domain)


def matches(rule: Rule, address: str, subject: str, headers: dict | None) -> bool:
    """All matchers given in the rule must match. Header rules need headers (None = not fetched yet)."""
    address = address.lower()
    if rule.sender and address != rule.sender.lower():
        return False
    if rule.domain and not _domain_matches(address, rule.domain):
        return False
    if rule.subject and rule.subject.lower() not in subject.lower():
        return False
    if rule.subject_regex and not re.search(rule.subject_regex, subject, re.I):
        return False
    if rule.header:
        if headers is None:
            return False
        name, _, text = rule.header.partition(":")
        if text.strip().lower() not in headers.get(name.strip().lower(), "").lower():
            return False
    return True


def first_match(config: Config, address: str, subject: str, headers: dict | None = None) -> Rule | None:
    """First matching rule. Without headers, header rules are skipped (they run after the fetch)."""
    for rule in config.rules:
        if rule.needs_headers and headers is None:
            continue
        if matches(rule, address, subject, headers):
            return rule
    return None


def has_header_rules(config: Config) -> bool:
    return any(rule.needs_headers for rule in config.rules)
