"""Remove characters that cannot appear in an XML 1.0 document."""

from dataclasses import fields, replace
import re

from src.core.models import Chapter, Story


_INVALID_XML = re.compile(r'[\x00-\x08\x0b\x0c\x0e-\x1f\ud800-\udfff\ufffe\uffff]')


def _clean(text: str) -> str:
    return _INVALID_XML.sub('', text)


def xml_safe_story(story: Story) -> Story:
    changes = {}
    for field in fields(story):
        value = getattr(story, field.name)
        if isinstance(value, str):
            changes[field.name] = _clean(value)
        elif field.name == 'chapters':
            changes[field.name] = [Chapter(_clean(chapter.title), _clean(chapter.html), _clean(chapter.source_url))
                                   for chapter in value]
        elif isinstance(value, list):
            changes[field.name] = [_clean(text) for text in value]
    return replace(story, **changes)
