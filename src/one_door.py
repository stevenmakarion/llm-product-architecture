#!/usr/bin/env python3
"""one_door.py - the guarded entry point every model call passes through.

THE STORY THIS CAME FROM

A refusal reached a paying customer. Not a crash, not an error, not a 500: the model
declined, and the product cheerfully rendered the decline as if it were the answer.

The obvious fix is to guard the call site that leaked. That fix is wrong, and it is wrong in
an instructive way: it repairs the instance and leaves the class. So instead of patching the
leak, we counted the doors. Fifteen places in the codebase called the model. Eight had no
guard at all. Nobody had been careless; the guards had simply been added where someone
happened to remember, which is what "remember to guard it" always degrades into.

So the guard moved INSIDE the single function all fifteen called. A new call site now
inherits the protection by existing. Forgetting stopped being possible, which is the only
version of "fixed" worth having.

THE THREE FAILURE MODES, AND WHY THE THIRD IS THE EXPENSIVE ONE

  refusal          loud    a polite apology where structured output belonged
  malformed        loud    prose instead of JSON, or a truncated object
  valid-but-empty  SILENT  parses perfectly, means nothing, required field simply absent

The third one is the reason "it parsed" is never the test. Nothing errors. Every dashboard
stays green. You discover it weeks later when the numbers do not reconcile.

STREAMING

Streaming makes this harder, because you cannot inspect an answer you have not finished
receiving, and buffering the whole thing throws away the latency you streamed for. The
resolution is a small held prefix: emit nothing until enough characters exist to rule out a
refusal, then release and stream freely. In production that threshold is 120 characters.

No dependencies. Drop it in.
"""
import json
import re
from typing import Any, Callable, Iterator, Optional, Tuple

# Deliberately anchored to the START of the response. A refusal announces itself in the first
# breath; matching mid-text would reject a legitimate answer that happens to DISCUSS refusal.
_REFUSAL = re.compile(
    r"^\s*(?:i'?m\s+sorry|i\s+am\s+sorry|i\s+cannot|i\s+can'?t\b|i\s+won'?t\b|"
    r"as\s+an\s+ai|unfortunately,?\s+i|i'?m\s+not\s+able|i\s+apologi[sz]e)",
    re.I,
)

HOLDBACK_CHARS = 120          # enough to classify a refusal, short enough to feel instant


class ModelOutputError(Exception):
    """Raised when output cannot be trusted. Carries WHY so the caller can log or fall back."""

    def __init__(self, reason: str, raw: str = ""):
        super().__init__(reason)
        self.reason = reason
        self.raw = raw


def looks_like_refusal(text: str) -> bool:
    return bool(_REFUSAL.match(text or ""))


def validate(text: str, require: Optional[Tuple[str, ...]] = None) -> Any:
    """The whole guard, in one place.

    require=None            -> prose is expected; only refusal and emptiness are failures.
    require=("verdict",...) -> JSON is expected AND those keys must be present.

    Returns the parsed object when require is given, else the text.
    """
    if text is None or not text.strip():
        raise ModelOutputError("empty response", text or "")

    if looks_like_refusal(text):
        raise ModelOutputError("model refused", text)

    if require is None:
        return text

    # Models like to wrap JSON in prose or fences. Take the outermost object.
    match = re.search(r"\{[\s\S]*\}", text)
    if not match:
        raise ModelOutputError("expected JSON, got prose", text)
    try:
        parsed = json.loads(match.group(0))
    except json.JSONDecodeError as exc:
        raise ModelOutputError(f"malformed JSON: {exc}", text) from exc

    # THE SILENT ONE. It parsed. That is not the same as it being usable.
    missing = [k for k in require if k not in parsed]
    if missing:
        raise ModelOutputError(f"missing required key(s): {', '.join(missing)}", text)

    return parsed


def complete(call: Callable[..., str], *args,
             require: Optional[Tuple[str, ...]] = None,
             fallback: Optional[Any] = None,
             on_reject: Optional[Callable[[str, str], None]] = None,
             **kwargs) -> Any:
    """THE ONE DOOR. Every model call in the product goes through this and nothing else.

    `call` is whatever actually talks to the model. This function does not care which
    provider, which transport, or which model: it cares that nothing untrusted gets past it.

    A `fallback` degrades visibly (a templated human-sounding reply) instead of failing
    silently. Without one, the caller gets the exception and decides.
    """
    raw = ""
    try:
        raw = call(*args, **kwargs)
        return validate(raw, require=require)
    except ModelOutputError as exc:
        if on_reject:
            on_reject(exc.reason, exc.raw)          # log the reason, always
        if fallback is not None:
            return fallback
        raise


def guarded_stream(chunks: Iterator[str],
                   holdback: int = HOLDBACK_CHARS,
                   on_reject: Optional[Callable[[str, str], None]] = None) -> Iterator[str]:
    """Stream, without ever emitting the first character of a refusal.

    Buffer until `holdback` characters exist, classify once, then release and pass through.
    If the stream ends shorter than the threshold, classify what arrived. The cost is a few
    dozen characters of latency; the benefit is that a refusal can never be rendered.
    """
    buf, released = "", False
    for chunk in chunks:
        if released:
            yield chunk
            continue
        buf += chunk
        if len(buf) < holdback:
            continue
        if looks_like_refusal(buf):
            if on_reject:
                on_reject("model refused (stream)", buf)
            return                                   # emit nothing at all
        released = True
        yield buf
    if not released:                                 # stream ended under the threshold
        if buf.strip() and not looks_like_refusal(buf):
            yield buf
        elif on_reject:
            on_reject("refused or empty (short stream)", buf)


if __name__ == "__main__":
    # Demonstration against the three failure modes plus the happy path.
    cases = [
        ("healthy prose",     lambda: "Here is the summary you asked for.",            None),
        ("refusal",           lambda: "I'm sorry, I can't help with that.",            None),
        ("empty",             lambda: "   ",                                           None),
        ("healthy JSON",      lambda: 'sure: {"qualified": true, "score": 80}', ("qualified",)),
        ("prose, no JSON",    lambda: "I think they seem qualified.",           ("qualified",)),
        ("valid but useless", lambda: '{"score": 80}',                          ("qualified",)),
    ]
    print("one_door: the guard every model call passes through\n")
    for name, fn, req in cases:
        try:
            out = complete(fn, require=req)
            print(f"  PASS    {name:20} -> {str(out)[:52]}")
        except ModelOutputError as e:
            print(f"  BLOCKED {name:20} -> {e.reason}")

    print("\nstreaming, a refusal arriving one chunk at a time:")
    refusal = iter(["I'm ", "sorry, ", "I can't ", "help with that, ", "but here is why..."])
    emitted = "".join(guarded_stream(refusal))
    print(f"  characters reaching the customer: {len(emitted)}")

    print("\nstreaming, a healthy answer:")
    good = iter(["The pipeline ", "failed because the ", "webhook returned 200 ",
                 "with an empty body, which the ", "workflow treated as success."])
    print(f"  {''.join(guarded_stream(good))}")
