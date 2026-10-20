from collections.abc import MutableMapping, MutableSet
from time import monotonic
from typing import TypeVar

K = TypeVar('K')
V = TypeVar('V')


class ExpiringDict(MutableMapping[K, V]):
    def __init__(self, *, ttl_seconds=1800, max_entries=10000):
        self.ttl_seconds = ttl_seconds
        self.max_entries = max_entries
        self._entries: dict[K, tuple[V, float]] = {}

    def expire(self):
        now = monotonic()
        expired = [key for key, (_, deadline) in self._entries.items() if deadline <= now]
        for key in expired:
            del self._entries[key]
        return expired

    def __getitem__(self, key):
        value, deadline = self._entries[key]
        if deadline <= monotonic():
            del self._entries[key]
            raise KeyError(key)
        self._entries[key] = (value, monotonic() + self.ttl_seconds)
        return value

    def __setitem__(self, key, value):
        self.expire()
        if key not in self._entries and len(self._entries) >= self.max_entries:
            oldest = min(self._entries, key=lambda item: self._entries[item][1])
            del self._entries[oldest]
        self._entries[key] = (value, monotonic() + self.ttl_seconds)

    def __delitem__(self, key):
        del self._entries[key]

    def __iter__(self):
        self.expire()
        return iter(self._entries)

    def __len__(self):
        self.expire()
        return len(self._entries)

    def clear(self):
        self._entries.clear()


class ExpiringSet(MutableSet[K]):
    def __init__(self, **kwargs):
        self._values: ExpiringDict[K, bool] = ExpiringDict(**kwargs)

    def add(self, value):
        self._values[value] = True

    def discard(self, value):
        self._values.pop(value, None)

    def __contains__(self, value):
        return value in self._values

    def __iter__(self):
        return iter(self._values)

    def __len__(self):
        return len(self._values)

    def expire(self):
        return self._values.expire()

    def clear(self):
        self._values.clear()
