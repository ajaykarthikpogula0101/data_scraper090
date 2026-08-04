"""Small, failure-tolerant language detection wrapper."""

from langdetect import DetectorFactory, LangDetectException, detect


DetectorFactory.seed = 0


def detect_language(text, minimum_characters=20):
    value = (text or "").strip()
    if len(value) < minimum_characters:
        return ""
    try:
        return detect(value)
    except LangDetectException:
        return ""
