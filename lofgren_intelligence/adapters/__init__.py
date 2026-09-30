from .base import Adapter, AdapterRegistry, GatherResult
from .documents import DocumentAdapter, WebPageAdapter
from .physical import ImageryCatalogAdapter, OrbitalPassAdapter, SensorAdapter

__all__ = [
    "Adapter", "AdapterRegistry", "DocumentAdapter", "GatherResult", "ImageryCatalogAdapter",
    "OrbitalPassAdapter", "SensorAdapter", "WebPageAdapter",
]
