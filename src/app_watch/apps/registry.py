"""Explicit built-in adapters; no dynamic plugin loading or implicit fallback."""

from app_watch.apps.base import AppAdapter
from app_watch.apps.bisq import BisqAdapter
from app_watch.apps.electrum import ElectrumAdapter
from app_watch.apps.monero import MoneroAdapter
from app_watch.apps.wasabi import WasabiAdapter

ADAPTERS: dict[str, AppAdapter] = {
    "bisq": BisqAdapter(),
    "electrum": ElectrumAdapter(),
    "wasabi": WasabiAdapter(),
    "monero": MoneroAdapter(),
}


def get_adapter(name: str) -> AppAdapter:
    try:
        return ADAPTERS[name]
    except KeyError:
        raise ValueError(
            f"Unsupported application {name!r}; choose {', '.join(ADAPTERS)}"
        ) from None
