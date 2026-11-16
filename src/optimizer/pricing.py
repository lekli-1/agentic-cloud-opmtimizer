import math
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from urllib.parse import urlparse

import httpx2
from kubernetes import client

from optimizer.config import Settings
from optimizer.models import NodeCosts, NodePrice

RETAIL_URL = "https://prices.azure.com/api/retail/prices"


@dataclass
class CachedPrice:
    hourly_usd: float
    checked_at: datetime
    expires_at: float


class AzurePrices:
    """Read Linux on-demand VM prices"""

    def __init__(
        self, settings: Settings, core: client.CoreV1Api, http: httpx2.Client
    ) -> None:
        self.settings = settings
        self.core = core
        self.http = http
        self.cache: dict[tuple[str, str], CachedPrice] = {}

    def lookup(self, region: str, vm_size: str) -> CachedPrice:
        key = (region, vm_size)
        cached = self.cache.get(key)
        if cached is not None and time.monotonic() < cached.expires_at:
            return cached
        # Quote label values as OData data, never as filter expressions.
        region_filter = region.replace("'", "''")
        size_filter = vm_size.replace("'", "''")
        params: dict[str, str] | None = {
            "currencyCode": "USD",
            "$filter": (
                "serviceName eq 'Virtual Machines' and "
                f"armRegionName eq '{region_filter}' and armSkuName eq '{size_filter}' "
                "and priceType eq 'Consumption'"
            ),
        }
        rates: set[float] = set()
        url: str | None = RETAIL_URL
        visited: set[str] = set()
        while url is not None:
            parsed = urlparse(url)
            if parsed.scheme != "https" or parsed.netloc != "prices.azure.com":
                raise ValueError("Azure returned an unexpected pricing pagination URL")
            if url in visited:
                raise ValueError("Azure pricing pagination repeated a page")
            visited.add(url)
            response = self.http.get(url, params=params)
            response.raise_for_status()
            payload = response.json()
            if not isinstance(payload, dict) or not isinstance(payload.get("Items"), list):
                raise ValueError("Azure pricing response has no Items array")
            for item in payload["Items"]:
                if not isinstance(item, dict):
                    raise ValueError("Azure pricing response contains an invalid item")
                item_region = item.get("armRegionName")
                if not isinstance(item_region, str):
                    raise ValueError("Azure pricing item has no region")
                labels = " ".join(
                    str(item.get(field, "")) for field in ("productName", "skuName", "meterName")
                ).lower()
                if (
                    item_region.lower() != region
                    or item.get("armSkuName") != vm_size
                    or item.get("type") != "Consumption"
                    or item.get("currencyCode") != "USD"
                    or item.get("unitOfMeasure") != "1 Hour"
                    or any(label in labels for label in ("windows", "spot", "low priority", "lowpriority"))
                ):
                    continue
                # Non-primary regional meters can be the valid Linux price.
                rate = item.get("retailPrice")
                if (
                    isinstance(rate, bool) or not isinstance(rate, (float, int))
                    or not math.isfinite(rate) or rate < 0
                ):
                    raise ValueError(f"Azure returned an invalid VM price for {vm_size}")
                rates.add(float(rate))
            next_page = payload.get("NextPageLink")
            if next_page is not None and not isinstance(next_page, str):
                raise ValueError("Azure returned an invalid pricing pagination URL")
            url = next_page or None
            params = None
        if len(rates) != 1:
            raise ValueError(
                f"Expected one Linux on-demand USD price for {vm_size} in {region}; "
                f"found {len(rates)} distinct prices"
            )
        result = CachedPrice(
            hourly_usd=rates.pop(), checked_at=datetime.now(timezone.utc),
            expires_at=time.monotonic() + self.settings.azure_price_cache_seconds,
        )
        self.cache[key] = result
        return result

    def collect(self) -> NodeCosts:
        nodes = self.core.list_node(
            _request_timeout=self.settings.azure_price_timeout_seconds
        ).items
        prices = []
        for node in sorted(nodes, key=lambda item: item.metadata.name):
            labels = node.metadata.labels or {}
            size = labels.get("node.kubernetes.io/instance-type")
            region = labels.get("topology.kubernetes.io/region")
            if not size or not region or labels.get("kubernetes.io/os") != "linux":
                raise ValueError(f"{node.metadata.name}: missing Linux VM size/region labels")
            region = region.lower()
            quote = self.lookup(region, size)
            prices.append(NodePrice(
                name=node.metadata.name, vm_size=size, region=region,
                hourly_usd=quote.hourly_usd, checked_at=quote.checked_at,
            ))
        if not prices:
            raise ValueError("No nodes available for Azure pricing")
        return NodeCosts(nodes=prices, hourly_usd=sum(item.hourly_usd for item in prices))

    def close(self) -> None:
        self.http.close()
