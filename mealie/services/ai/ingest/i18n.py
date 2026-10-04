"""
The fork's server-side texts in the user's language (docs/ai/PHASE2.md §7, §8, §14), falling back to en-US.

Only en-US is edited here: the other locales come from upstream through Crowdin and gain the fork's `recipe-ingest`
texts later, if ever. Mealie's `JsonProvider` answers a key its locale file doesn't have with the key itself, so every
fork text rendered on the server (error messages, notifications, and what commit writes into a recipe: the "From"
note, the card asset names, "(unreadable)") goes through `FallbackTranslator`, never a bare locale provider.
"""

from typing import Any

from mealie.lang.providers import Translator, get_locale_provider

DEFAULT_LOCALE = "en-US"


class FallbackTranslator:
    """
    The request's language, falling back to en-US for a text that language doesn't have yet: only en-US is edited
    here, and the other locales gain the fork's texts later through Crowdin (a missing key would show as the key).
    """

    def __init__(self, primary: Translator, fallback: Translator) -> None:
        self.primary = primary
        self.fallback = fallback

    def t(self, key: str, default: Any = None, **kwargs: Any) -> str:
        text = self.primary.t(key, default, **kwargs)
        if text == key and self.fallback is not self.primary:
            return self.fallback.t(key, default, **kwargs)
        return text


def with_fallback(translator: Translator) -> Translator:
    """`translator`, falling back to en-US for a text its language doesn't have"""
    if isinstance(translator, FallbackTranslator):
        return translator
    return FallbackTranslator(translator, get_locale_provider(DEFAULT_LOCALE))


def translator_for(locale: str | None) -> Translator:
    """The translator of a stored locale (a job's or a batch's; en-US when there's none), falling back to en-US"""
    return with_fallback(get_locale_provider(locale or DEFAULT_LOCALE))
