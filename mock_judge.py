"""
Mock TypeSafe client for keyless testing (JUDGE_MOCK=1) and unit tests.

Answers are deterministic functions of the state, shaped exactly like Jev's wire format,
so the funnel, thresholds, pick and book can be exercised end to end. They carry no
information about any real token. Never run the desk live against this.
"""
import hashlib
import json
from types import SimpleNamespace


class _Answer(SimpleNamespace):
    def model_dump(self):
        return dict(self.__dict__)


class _Usage(SimpleNamespace):
    def model_dump(self):
        return dict(self.__dict__)


def _seed(state, name) -> float:
    """Stable 0..1 from the state and the question name."""
    h = hashlib.sha256((json.dumps(state, sort_keys=True, default=str) + name).encode()).digest()
    return int.from_bytes(h[:4], "big") / 2**32


def _dist(keys, favoured, sharp=0.75):
    keys = list(keys)
    rest = (1 - sharp) / max(len(keys) - 1, 1)
    return {k: (sharp if k == favoured else rest) for k in keys}


def answer(name, q, state) -> _Answer:
    qtype = q.type if hasattr(q, "type") else q["type"]
    r = _seed(state, name)
    if qtype == "noul":
        # bias a few well-known questions so the mock funnel lets some tokens through
        bias = {"liquidity_fits_ticket": 0.35, "account_is_the_project": 0.4,
                "audience_is_real": 0.3, "sellable_by_evidence": 0.35,
                "worth_trading_at_all": 0.4}.get(name, 0.0)
        penalty = {"momentum_already_spent": 0.25, "concentration_is_exit_risk": 0.25,
                   "recycled_account": 0.3, "dev_still_loaded": 0.3}.get(name, 0.0)
        v = min(1.0, max(0.0, r * (1 - bias - penalty) + bias))
        return _Answer(type="noul", noul=round(v, 3))
    criteria = q.criteria if hasattr(q, "criteria") else q["criteria"]
    if qtype == "choice":
        keys = list(criteria)
        # favour the "good" label two times out of three so the funnel is exercised
        good = next((k for k in ("crowd", "renounced", "clean", "indexed") if k in keys), None)
        favoured = good if (good and r < 0.66) else keys[int(r * len(keys)) % len(keys)]
        probs = _dist(keys, favoured, sharp=0.55 + 0.4 * r)
        return _Answer(type="choice", choice=favoured, confidence=round(probs[favoured], 3),
                       probabilities={k: round(v, 3) for k, v in probs.items()})
    if qtype == "score":
        levels = list(range(len(criteria)))
        favoured = min(len(levels) - 1, int(r * len(levels)) + (1 if r > 0.3 else 0))
        probs = _dist(levels, favoured, sharp=0.6)
        score = sum(k * v for k, v in probs.items())
        return _Answer(type="score", score=round(score, 3), confidence=round(probs[favoured], 3),
                       legend={i: c for i, c in enumerate(criteria)},
                       probabilities={k: round(v, 3) for k, v in probs.items()})
    raise ValueError(f"unknown question type {qtype}")


class MockTypeSafeClient:
    model = "jev-mock-0.0.0"

    async def system_one(self, state, questions):
        answers = {name: answer(name, q, state) for name, q in questions.items()}
        n = len(json.dumps(state, default=str)) // 4
        return SimpleNamespace(model=self.model, answers=answers,
                               usage=_Usage(input_tokens=n, output_tokens=len(answers) * 8))

    def system_one_sync(self, state, questions):
        import asyncio
        return asyncio.run(self.system_one(state, questions))


def mock_judge_fn():
    """A `judge(question_set, state) -> dict` callable with the same contract as
       judge_client.judge, for running main.run_once without the HTTP service."""
    from questions import SETS
    client = MockTypeSafeClient()

    def judge(question_set, state):
        if question_set not in SETS:
            raise RuntimeError(f"malformed question set {question_set}")
        qs = SETS[question_set]
        qs = qs(state) if callable(qs) else qs
        r = client.system_one_sync(state, qs)
        return {"model": r.model,
                "answers": {k: v.model_dump() for k, v in r.answers.items()},
                "usage": r.usage.model_dump()}
    return judge
