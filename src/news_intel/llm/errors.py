class LLMUnavailableError(Exception):
    """Raised when the LLM cannot produce valid structured output after the
    single automatic repair attempt. Callers must fall back deterministically
    (design doc section 8.4) rather than block the job."""
