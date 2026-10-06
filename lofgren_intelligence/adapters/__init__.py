from .base import Adapter, AdapterRegistry, GatherResult
from .clinicaltrials import ClinicalTrialsAdapter
from .documents import DocumentAdapter, WebPageAdapter
from .europepmc import EuropePMCAdapter
from .manifest import ManifestError, OperatorSourcesAdapter, SourceManifest, load_manifest
from .public_api import OUTCOMES, ProviderUnavailable, PublicHTTPClient
from .physical import ImageryCatalogAdapter, OrbitalPassAdapter, SensorAdapter, normalize_scene, normalize_unit
from .search import (
    BraveSearchProvider,
    SearchProvider,
    SearchResult,
    WebSearchAdapter,
    canonical_url,
    classify_source,
)

__all__ = [
    "Adapter", "AdapterRegistry", "BraveSearchProvider", "ClinicalTrialsAdapter", "DocumentAdapter",
    "EuropePMCAdapter", "GatherResult", "ManifestError", "OUTCOMES", "OperatorSourcesAdapter",
    "ProviderUnavailable", "PublicHTTPClient", "SourceManifest", "load_manifest",
    "ImageryCatalogAdapter", "OrbitalPassAdapter", "SearchProvider", "SearchResult", "SensorAdapter",
    "WebPageAdapter", "WebSearchAdapter", "canonical_url", "classify_source", "normalize_scene", "normalize_unit",
]
