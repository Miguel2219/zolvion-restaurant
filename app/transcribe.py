"""Voice notes to text (Task E addendum): ElevenLabs Scribe v2.

Chosen 2026-10-21 for the lowest word error rate in the Artificial Analysis benchmark,
for taking WhatsApp's OGG/Opus file as it is (no conversion), for keyterms built from the
menu, and for the signals code uses to refuse a note it did not really hear (no words,
only noise, an unlikely language). Claude does not take audio. Without
ELEVENLABS_API_KEY nothing is sent and the bot asks the customer to write instead.

What comes back is the customer's own words, never an instruction: the worker hands it
to the bot like typed text. Code, not the transcriber, decides whether a note was clear."""

import os
import traceback
from dataclasses import dataclass

import httpx

from .catalog import CATALOG

URL = "https://api.elevenlabs.io/v1/speech-to-text"
MODEL = "scribe_v2"
LANGUAGE = "spa"
TIMEOUT_SECONDS = 30
# Below this the transcriber itself doubts the note is Spanish speech: tuned with
# Miguel's real notes in the hand test.
MIN_LANGUAGE_PROBABILITY = 0.5
MAX_KEYTERMS = 100
KEYTERM_FORBIDDEN = set("<>{}[]\\")


@dataclass
class Heard:
    text: str
    clear: bool
    reason: str | None = None        # no_key | too_big | error | unclear


def api_key() -> str:
    return os.environ.get("ELEVENLABS_API_KEY", "").strip()


def enabled() -> bool:
    return bool(api_key())


def max_bytes() -> int:
    """VOICE_MAX_MB (default 2: several minutes of WhatsApp's Opus audio)."""
    try:
        megabytes = float(os.environ.get("VOICE_MAX_MB", "").strip() or 2)
    except ValueError:
        megabytes = 2.0
    return int(max(megabytes, 0.1) * 1024 * 1024)


def keyterms() -> list[str]:
    """The menu's own words, so "Caserita Madurita" is not heard as something else:
    item, modifier and zone names, within the service's limits."""
    names = [item["name"] for category in CATALOG.raw["menu"]["categories"]
             for item in category["items"]]
    names += [m["name"] for m in CATALOG.raw["menu"].get("modifiers", [])]
    names += [z["name"] for z in CATALOG.fulfilment.get("delivery_zones", [])]
    terms: list[str] = []
    for name in names:
        name = " ".join(str(name).split())
        if (name and name not in terms and len(name) < 50 and len(name.split()) <= 5
                and not KEYTERM_FORBIDDEN & set(name)):
            terms.append(name)
    return terms[:MAX_KEYTERMS]


def transcribe(raw: bytes, mime: str | None) -> Heard:
    """Never raises: a failure is a Heard that is not clear, with its reason."""
    if not enabled():
        return Heard("", False, "no_key")
    if len(raw) > max_bytes():
        return Heard("", False, "too_big")
    try:
        response = httpx.post(
            URL,
            headers={"xi-api-key": api_key()},
            data={"model_id": MODEL, "language_code": LANGUAGE, "tag_audio_events": "true",
                  "keyterms": keyterms()},
            files={"file": ("nota-de-voz.ogg", raw, mime or "audio/ogg")},
            timeout=TIMEOUT_SECONDS,
        )
        if response.status_code >= 300:
            print(f"!! ElevenLabs respondió {response.status_code}: {response.text[:200]}", flush=True)
            return Heard("", False, "error")
        data = response.json()
    except Exception:  # noqa: BLE001 - a note that can't be heard must not stop the worker
        traceback.print_exc()
        return Heard("", False, "error")
    return heard_from(data if isinstance(data, dict) else {})


def heard_from(data: dict) -> Heard:
    """Code's verdict on the transcriber's answer: spoken words, in Spanish."""
    text = " ".join(str(data.get("text") or "").split())
    spoken = [w for w in data.get("words") or []
              if isinstance(w, dict) and w.get("type") == "word" and str(w.get("text") or "").strip()]
    probability = data.get("language_probability")
    if not text or not spoken:
        return Heard(text, False, "unclear")
    if isinstance(probability, (int, float)) and probability < MIN_LANGUAGE_PROBABILITY:
        return Heard(text, False, "unclear")
    return Heard(text, True)
