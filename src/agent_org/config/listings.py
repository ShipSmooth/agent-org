"""Amazon channel identity: which SKU Amazon holds, and whether it is live.

Zach's internal SKUs are not the SKUs Amazon holds. Amazon holds
auto-generated strings — `05-MN0Y-QNA3` for the black IFAK — and there is
no pattern joining the two. The mapping can only ever be data, and
`config/<entity>/listings.yaml` is that data. Nothing here is ever
inferred from the shape of a SKU.

Two consequences run through the rest of the system:

* **The channel SKU is the join key for sales.** Veeqo keys on Zach's own
  seller-SKU; the ASIN is North American Rescue's, states no colour on the
  C-A-T listings, and is descriptive only. One component may have several
  channel SKUs and several channel SKUs may share one ASIN, so a
  component's velocity is the sum across all of its channel SKUs.
* **An inactive listing is not zero demand.** Zach deactivates a listing
  when he is out of stock. A trailing average cannot tell "nobody wants
  this" from "he could not sell it", so a subject whose listings are all
  inactive is reported as suppressed, never as a zero-demand line.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Any

from agent_org.config.errors import ConfigError, error
from agent_org.config.yamlsource import Loc, YamlMap, load_yaml_file

ACTIVE = "active"
INACTIVE = "inactive"


@dataclass(frozen=True)
class ChannelListing:
    """One SKU on one channel, and whether Amazon is currently showing it."""

    channel: str
    sku: str
    status: str
    asin: str | None
    loc: Loc

    @property
    def is_active(self) -> bool:
        return self.status == ACTIVE


@dataclass(frozen=True)
class ListingSet:
    """Every listing for one kit or one component."""

    subject: str
    sales_asins: tuple[str, ...]
    listings: tuple[ChannelListing, ...]
    loc: Loc

    @property
    def channel_skus(self) -> tuple[str, ...]:
        return tuple(listing.sku for listing in self.listings)

    @property
    def active_listings(self) -> tuple[ChannelListing, ...]:
        return tuple(listing for listing in self.listings if listing.is_active)

    @property
    def demand_is_suppressed(self) -> bool:
        """Listed, but nowhere a customer can buy it.

        A subject with no listing at all is not suppressed: 20-314 has
        never been on Amazon, and zero Amazon sales is the truth about it.
        """
        return bool(self.listings) and not self.active_listings

    def sku_for(self, channel: str) -> str | None:
        for listing in self.listings:
            if listing.channel == channel:
                return listing.sku
        return None


@dataclass(frozen=True)
class UnresolvedListing:
    """A live listing that cannot be attributed to anything Zach sells."""

    asin: str
    sku: str
    note: str
    loc: Loc


@dataclass(frozen=True)
class ListingsConfig:
    source_report: str
    extracted: str
    kits: dict[str, ListingSet]
    components: dict[str, ListingSet]
    unresolved: tuple[UnresolvedListing, ...]

    def for_kit(self, kit_group: str) -> ListingSet | None:
        return self.kits.get(kit_group)

    def for_part(self, part: str) -> ListingSet | None:
        return self.components.get(part)


EMPTY = ListingsConfig(
    source_report="",
    extracted="",
    kits={},
    components={},
    unresolved=(),
)


def _channel_listing(channel: str, value: Any, loc: Loc, subject: str) -> ChannelListing | None:
    """A channel entry is a SKU, a {sku, status} block, or null."""
    if value is None:
        return None
    if isinstance(value, YamlMap):
        sku = value.get("sku")
        if not isinstance(sku, str) or not sku.strip():
            raise ConfigError(
                [
                    error(
                        f"The '{channel}' listing for {subject} has no 'sku'.",
                        value.loc,
                        fix="Write the Amazon SKU, or 'null' if there is no listing.",
                    )
                ]
            )
        status = str(value.get("status", ACTIVE))
        if status not in (ACTIVE, INACTIVE):
            raise ConfigError(
                [
                    error(
                        f"The '{channel}' listing for {subject} has status '{status}'.",
                        value.loc_of("status"),
                        fix=f"Use '{ACTIVE}' or '{INACTIVE}'.",
                    )
                ]
            )
        return ChannelListing(channel=channel, sku=sku, status=status, asin=None, loc=value.loc)
    return ChannelListing(channel=channel, sku=str(value), status=ACTIVE, asin=None, loc=loc)


def load_listings(path: Path) -> ListingsConfig:
    raw = load_yaml_file(path)
    if not isinstance(raw, YamlMap):
        raise ConfigError(
            [error("The listings file must be a block of settings.", Loc(str(path), 1))]
        )

    kits: dict[str, ListingSet] = {}
    kits_block = raw.get("kits")
    if isinstance(kits_block, YamlMap):
        for kit_group, value in kits_block.items():
            entry = value if isinstance(value, YamlMap) else YamlMap()
            loc = kits_block.loc_of(kit_group)
            listings: list[ChannelListing] = []
            for channel, channel_value in entry.items():
                if channel == "sales_asin":
                    continue
                listing = _channel_listing(
                    channel, channel_value, entry.loc_of(channel), f"kit '{kit_group}'"
                )
                if listing is not None:
                    listings.append(listing)
            sales_asin = entry.get("sales_asin")
            kits[str(kit_group)] = ListingSet(
                subject=str(kit_group),
                sales_asins=(str(sales_asin),) if isinstance(sales_asin, str) else (),
                listings=tuple(listings),
                loc=loc,
            )

    components: dict[str, ListingSet] = {}
    asins_block = raw.get("component_sales_asins")
    if isinstance(asins_block, YamlMap):
        for part, value in asins_block.items():
            asins = tuple(str(asin) for asin in value) if isinstance(value, list) else ()
            components[str(part)] = ListingSet(
                subject=str(part),
                sales_asins=asins,
                listings=(),
                loc=asins_block.loc_of(part),
            )

    # The C-A-T colour attribution is the case that forced the channel SKU to
    # become the key: three colourways, three ASINs owned by NAR, no colour in
    # any title. Zach's own seller-SKUs are the only thing that tells them apart.
    attribution = raw.get("cat_gen7_attribution")
    if isinstance(attribution, YamlMap):
        by_sku = attribution.get("by_channel_sku")
        if isinstance(by_sku, YamlMap):
            grouped: dict[str, list[ChannelListing]] = {}
            for sku, value in by_sku.items():
                entry = value if isinstance(value, YamlMap) else YamlMap()
                part = str(entry.get("part"))
                asin = entry.get("asin")
                grouped.setdefault(part, []).append(
                    ChannelListing(
                        channel=str(entry.get("channel", "")),
                        sku=str(sku),
                        status=str(entry.get("status", ACTIVE)),
                        asin=str(asin) if isinstance(asin, str) else None,
                        loc=by_sku.loc_of(str(sku)),
                    )
                )
            for part, listings in grouped.items():
                existing = components.get(part)
                asins = tuple(
                    dict.fromkeys(
                        (existing.sales_asins if existing else ())
                        + tuple(item.asin for item in listings if item.asin is not None)
                    )
                )
                components[part] = ListingSet(
                    subject=part,
                    sales_asins=asins,
                    listings=tuple(listings),
                    loc=existing.loc if existing else by_sku.loc,
                )

    unresolved: list[UnresolvedListing] = []
    for item in raw.get("unresolved_listings", []) or []:
        entry = item if isinstance(item, YamlMap) else YamlMap()
        unresolved.append(
            UnresolvedListing(
                asin=str(entry.get("asin", "")),
                sku=str(entry.get("sku", "")),
                note=str(entry.get("note", "")).strip(),
                loc=entry.loc,
            )
        )

    return ListingsConfig(
        source_report=str(raw.get("source_report", "")),
        extracted=str(raw.get("extracted", "")),
        kits=kits,
        components=components,
        unresolved=tuple(unresolved),
    )


__all__ = [
    "ACTIVE",
    "EMPTY",
    "INACTIVE",
    "ChannelListing",
    "ListingSet",
    "ListingsConfig",
    "UnresolvedListing",
    "load_listings",
]
