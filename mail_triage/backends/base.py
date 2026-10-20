"""Model backends answer typed questions in the "system-one" format shared by laya-mlx,
llama.cpp (`llama serve`) and Ollama:

    predict(state: dict, questions: dict) -> answers
    answers = {question_id: {"type": "choice", "probabilities": {...}} | {"type": "noul", "noul": p}}

Add a backend (ONNX, OpenVINO, a Windows runtime …) by implementing Backend.
"""

from __future__ import annotations

from typing import Protocol, runtime_checkable

from ..config import Config
from ..lang import is_english
from ..policy import questions


class BackendError(Exception):
    pass


@runtime_checkable
class Backend(Protocol):
    label: str

    def predict(self, state: dict, questions: dict) -> dict: ...

    def close(self) -> None: ...


def validate_answers(answers, asked: dict) -> dict:
    """Refuse malformed answers instead of guessing."""
    if not isinstance(answers, dict) or set(asked) - set(answers):
        raise BackendError("model answer is missing questions")
    for qid, question in asked.items():
        answer = answers[qid]
        if question["type"] == "choice":
            probabilities = answer.get("probabilities") if isinstance(answer, dict) else None
            criteria = question["criteria"]
            if not isinstance(probabilities, dict) or set(probabilities) != set(criteria):
                raise BackendError(f"model answer for {qid} does not match the offered choices")
        elif question["type"] == "noul":
            value = answer.get("noul") if isinstance(answer, dict) else None
            if not isinstance(value, (int, float)) or not 0 <= value <= 1:
                raise BackendError(f"model answer for {qid} is not a probability")
    return answers


class Router:
    """English mail → the English model; other languages → the multilingual model if configured."""

    def __init__(self, config: Config, english: Backend, multilingual: Backend | None):
        self.config = config
        self.english = english
        self.multilingual = multilingual
        self.english_questions = questions(config)
        self.multilingual_questions = questions(config, multilingual=True)

    @property
    def label(self) -> str:
        return self.english.label + (f" + {self.multilingual.label}" if self.multilingual else "")

    def classify(self, state: dict) -> tuple[dict, bool, str]:
        """(answers, fluent, model label). fluent=False means the model may not read this language."""
        english = is_english(state)
        if english or self.multilingual is None:
            asked = self.english_questions
            return validate_answers(self.english.predict(state, asked), asked), english, self.english.label
        asked = self.multilingual_questions
        return validate_answers(self.multilingual.predict(state, asked), asked), True, self.multilingual.label

    def close(self) -> None:
        self.english.close()
        if self.multilingual is not None:
            self.multilingual.close()
