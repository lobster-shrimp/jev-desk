"""The only place a bot touches the network for a judgement."""
import os

import requests

URL, SECRET = os.environ["JUDGE_URL"], os.environ["DESK_SECRET"]


def judge(question_set: str, state: dict) -> dict:
    r = requests.post(URL, timeout=30,
                      headers={"Authorization": f"Bearer {SECRET}"},
                      json={"question_set": question_set, "state": state})
    if r.status_code == 422:
        raise RuntimeError(f"malformed question set {question_set}: {r.text}")   # never retry
    r.raise_for_status()
    return r.json()
