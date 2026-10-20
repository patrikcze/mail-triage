"""Configuration: built-in defaults (default_config.toml) overlaid with the user's config.toml."""

from __future__ import annotations

from dataclasses import dataclass, field
import hashlib
import ipaddress
import json
import os
from pathlib import Path
import re
import sys
import tomllib
from urllib.parse import urlparse


DEFAULT_FILE = Path(__file__).with_name("default_config.toml")
ACTIONS = ("keep", "review", "junk")
COARSE = ("personal", "transactional", "marketing")
CONSOLE = ("full", "redacted", "counts")
BACKENDS = ("auto", "mlx", "systemone")
RULE_MATCHERS = ("sender", "domain", "subject", "subject_regex", "header")
_NAME = re.compile(r"^[A-Za-z0-9_]{1,40}$")


class ConfigError(Exception):
    pass


def home() -> Path:
    """Per-user data folder: config, history database and reports."""
    if os.environ.get("MAIL_TRIAGE_HOME"):
        return Path(os.environ["MAIL_TRIAGE_HOME"]).expanduser()
    return default_home()


def default_home() -> Path:
    if sys.platform == "darwin":
        return Path.home() / "Library/Application Support/MailTriage"
    if sys.platform == "win32":
        return Path(os.environ.get("LOCALAPPDATA", Path.home() / "AppData/Local")) / "MailTriage"
    return Path(os.environ.get("XDG_DATA_HOME", Path.home() / ".local/share")) / "mail-triage"


@dataclass(frozen=True)
class Category:
    name: str
    description: str
    action: str
    folder: str
    coarse: str


@dataclass(frozen=True)
class Rule:
    name: str
    category: str
    sender: str = ""
    domain: str = ""
    subject: str = ""
    subject_regex: str = ""
    header: str = ""

    @property
    def needs_headers(self) -> bool:
        return bool(self.header)


@dataclass(frozen=True)
class Config:
    categories: tuple[Category, ...]
    rules: tuple[Rule, ...]
    model: dict
    instructions: dict
    policy: dict
    learning: dict
    privacy: dict
    actions: dict
    path: Path | None = field(default=None, compare=False)

    def category(self, name: str) -> Category:
        for category in self.categories:
            if category.name == name:
                return category
        raise KeyError(name)

    def names(self, action: str | None = None) -> list[str]:
        return [c.name for c in self.categories if action is None or c.action == action]

    def fingerprint(self) -> str:
        """Changes when anything that affects decisions changes (not privacy/learning)."""
        relevant = {
            "categories": [c.__dict__ for c in self.categories],
            "rules": [r.__dict__ for r in self.rules],
            "model": self.model, "instructions": self.instructions, "policy": self.policy,
        }
        return hashlib.sha256(json.dumps(relevant, sort_keys=True).encode()).hexdigest()[:12]


def _merge(base: dict, override: dict) -> dict:
    merged = dict(base)
    for key, value in override.items():
        if isinstance(value, dict) and isinstance(merged.get(key), dict):
            merged[key] = _merge(merged[key], value)
        else:
            merged[key] = value
    return merged


def is_loopback_url(url: str) -> bool:
    parsed = urlparse(url)
    if parsed.scheme not in ("http", "https") or not parsed.hostname:
        return False
    if parsed.hostname == "localhost":
        return True
    try:
        return ipaddress.ip_address(parsed.hostname).is_loopback
    except ValueError:
        return False


def _number(section: dict, key: str, low: float, high: float, where: str) -> None:
    value = section.get(key)
    if isinstance(value, bool) or not isinstance(value, (int, float)) or not low <= value <= high:
        raise ConfigError(f"{where}.{key} must be a number between {low} and {high}")


def _validate(raw: dict) -> Config:
    categories = []
    for index, item in enumerate(raw.get("categories", [])):
        where = f"categories[{index + 1}]"
        if not isinstance(item, dict):
            raise ConfigError(f"{where} must be a table")
        name = str(item.get("name", ""))
        if not _NAME.match(name):
            raise ConfigError(f"{where}.name must be 1-40 letters, digits or _ (got {name!r})")
        if item.get("action") not in ACTIONS:
            raise ConfigError(f"{where}.action must be one of {', '.join(ACTIONS)}")
        coarse = item.get("coarse", "")
        if coarse not in COARSE + ("",):
            raise ConfigError(f"{where}.coarse must be one of {', '.join(COARSE)} or \"\"")
        description = str(item.get("description", "")).strip()
        if not description:
            raise ConfigError(f"{where}.description must not be empty")
        categories.append(Category(name, description, item["action"], str(item.get("folder", "")).strip("/ "), coarse))
    names = [c.name for c in categories]
    if len(set(names)) != len(names):
        raise ConfigError("category names must be unique")
    if not any(c.action == "keep" for c in categories) or not any(c.action == "junk" for c in categories):
        raise ConfigError("define at least one category with action \"keep\" and one with \"junk\"")
    if len(categories) > 16:
        raise ConfigError("at most 16 categories are supported")

    rules = []
    for index, item in enumerate(raw.get("rules", [])):
        where = f"rules[{index + 1}]"
        if item.get("category") not in names:
            raise ConfigError(f"{where}.category must be one of the category names")
        matchers = {key: str(item.get(key, "")) for key in RULE_MATCHERS}
        if not any(matchers.values()):
            raise ConfigError(f"{where} needs at least one of {', '.join(RULE_MATCHERS)}")
        if matchers["subject_regex"]:
            try:
                re.compile(matchers["subject_regex"])
            except re.error as exc:
                raise ConfigError(f"{where}.subject_regex is not valid: {exc}") from exc
        if matchers["header"] and ":" not in matchers["header"]:
            raise ConfigError(f"{where}.header must look like \"Header-Name: text\"")
        rules.append(Rule(name=str(item.get("name", f"rule {index + 1}")), category=item["category"], **matchers))

    model = raw["model"]
    if model.get("backend") not in BACKENDS:
        raise ConfigError(f"model.backend must be one of {', '.join(BACKENDS)}")
    _number(model, "temperature", 0.1, 10, "model")
    _number(model, "body_chars", 200, 20000, "model")
    systemone = model["systemone"]
    for key in ("url", "multilingual_url"):
        url = systemone.get(key, "")
        if url and not is_loopback_url(url):
            raise ConfigError(f"model.systemone.{key} must point to this computer (127.0.0.1 or localhost); "
                              "emails are never sent to other machines")
    _number(systemone, "timeout_seconds", 1, 600, "model.systemone")

    instructions = raw["instructions"]
    for key in ("kind", "is_marketing", "is_spam", "is_phishing"):
        if not str(instructions.get(key, "")).strip():
            raise ConfigError(f"instructions.{key} must not be empty")

    policy = raw["policy"]
    _number(policy, "junk_threshold", 0.5, 0.999, "policy")
    _number(policy, "review_threshold", 0.3, 0.999, "policy")
    if policy["review_threshold"] > policy["junk_threshold"]:
        raise ConfigError("policy.review_threshold must not exceed policy.junk_threshold")
    for key, value in policy["weights"].items():
        _number(policy["weights"], key, -20, 20, "policy.weights")

    learning = raw["learning"]
    _number(learning, "min_samples", 1, 1000, "learning")
    _number(learning, "agreement", 0.5, 1.0, "learning")
    _number(learning, "recheck_every", 1, 10000, "learning")

    privacy = raw["privacy"]
    if privacy.get("console") not in CONSOLE:
        raise ConfigError(f"privacy.console must be one of {', '.join(CONSOLE)}")
    _number(privacy, "retention_days", 0, 36500, "privacy")

    actions = raw["actions"]
    if not str(actions.get("allowed_root", "")).strip("/ "):
        raise ConfigError("actions.allowed_root must not be empty")

    return Config(tuple(categories), tuple(rules), model, instructions, policy, learning, privacy, actions)


def load(path: Path | None = None) -> Config:
    """Defaults merged with the user's file (if it exists). Arrays such as categories replace defaults."""
    with DEFAULT_FILE.open("rb") as handle:
        raw = tomllib.load(handle)
    path = path or home() / "config.toml"
    if path.is_file():
        try:
            with path.open("rb") as handle:
                raw = _merge(raw, tomllib.load(handle))
        except tomllib.TOMLDecodeError as exc:
            raise ConfigError(f"{path} is not valid TOML: {exc}") from exc
    config = _validate(raw)
    return Config(**{**config.__dict__, "path": path if path.is_file() else None})
