"""The names Veeqo prints on each channel, now that Zach has read them.

Four are configured, one channel does not exist in Veeqo at all, and
Amazon Canada and Mexico are out of scope by decision. Each of those three
states behaves differently on purpose, and the difference is what these
tests hold: a name that is merely unresolved must still stop a live run,
a channel that is genuinely absent must not, and demand deliberately left
out must be named rather than silently dropped.
"""

from __future__ import annotations

import dataclasses
from datetime import UTC, datetime
from pathlib import Path

import httpx
import psycopg
import pytest

from agent_org.config.loader import load_config
from agent_org.config.models import NOT_CONNECTED, Channel
from agent_org.config.validate import validate
from agent_org.db.connection import entity_session
from agent_org.integrations.reads import ReadFailure
from agent_org.integrations.veeqo import VEEQO_API_KEY_VAR, VeeqoLiveClient
from agent_org.runtime.worker import channel_keys_from, run_replenishment

REPO = Path(__file__).resolve().parents[1]
REAL_CONFIG = REPO / "config"
PREFIX = "ITHRIVE_"
# After the day the hand-counted shelves were counted: a run cannot be
# asked to believe a count taken in its own future.
AFTER_THE_COUNT = datetime(2026, 9, 14, 6, 0, tzinfo=UTC)

EXCLUDED = ("Amazon Canada FBA", "Amazon Canada", "Amazon Mexico FBA", "Amazon Mexico")


def _key(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv(f"{PREFIX}{VEEQO_API_KEY_VAR}", "not-a-real-key")


def _orders(*channels: str) -> list[dict[str, object]]:
    return [
        {
            "created_at": "2026-08-01T10:00:00Z",
            "status": "shipped",
            "channel": {"name": name},
            "line_items": [{"quantity": 10, "sellable": {"sku_code": "5G-AP1S-TUE4"}}],
        }
        for name in channels
    ]


def _transport(orders: list[dict[str, object]]) -> httpx.MockTransport:
    def handler(request: httpx.Request) -> httpx.Response:
        page = int(request.url.params.get("page", "1"))
        return httpx.Response(200, json=orders if page == 1 else [])

    return httpx.MockTransport(handler)


def test_the_four_connected_channels_carry_the_names_veeqo_prints() -> None:
    config, _ = load_config(REAL_CONFIG, "ithrive")
    assert channel_keys_from(config) == {
        "Amazon FBA": "amazon_fba",
        "Amazon": "amazon_fbm",
        "Shopify": "shopify",
        "ithrive": "walmart_sf",
    }


def test_a_channel_that_is_not_in_veeqo_does_not_stop_the_run() -> None:
    """Walmart WFS has no Veeqo channel, which is a fact, not a gap.

    A TBD placeholder means nobody has looked and must still stop a live
    run; `not_connected` means someone looked and there is nothing there.
    Veeqo cannot report an order on a channel it does not have, so the
    distinction costs no demand.
    """
    config, _ = load_config(REAL_CONFIG, "ithrive")
    wfs = config.entity.channel("walmart_wfs")
    assert wfs is not None
    assert wfs.veeqo_channel == NOT_CONNECTED
    assert "walmart_wfs" not in channel_keys_from(config).values()


def test_a_placeholder_still_stops_a_live_run() -> None:
    config, _ = load_config(REAL_CONFIG, "ithrive")
    channels = tuple(
        dataclasses.replace(channel, veeqo_channel="TBD-VEEQO-CHANNEL-SHOPIFY")
        if channel.key == "shopify"
        else channel
        for channel in config.entity.channels
    )
    entity = dataclasses.replace(config.entity, channels=channels)
    with pytest.raises(ReadFailure) as failure:
        channel_keys_from(dataclasses.replace(config, entity=entity))
    assert "shopify" in str(failure.value)


def test_merchant_amazon_does_not_swallow_fba(monkeypatch: pytest.MonkeyPatch) -> None:
    """'Amazon' and 'Amazon FBA' are matched whole, not by prefix.

    Getting this wrong would file every FBA sale as merchant-fulfilled,
    which is the split that decides whether stock is sent to Amazon.
    """
    _key(monkeypatch)
    client = VeeqoLiveClient(
        channel_keys={"Amazon FBA": "amazon_fba", "Amazon": "amazon_fbm"},
        credentials_prefix=PREFIX,
        transport=_transport(_orders("Amazon", "Amazon FBA", "Amazon FBA")),
    )
    velocity = client.read_velocity(90)
    assert velocity["5G-AP1S-TUE4"].by_channel == {"amazon_fbm": 10, "amazon_fba": 20}


def test_the_live_config_excludes_the_four_foreign_amazon_channels() -> None:
    config, _ = load_config(REAL_CONFIG, "ithrive")
    assert config.entity.excluded_veeqo_channels == EXCLUDED


def test_canadian_and_mexican_sales_do_not_count_towards_demand(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """US-only demand, by Zach's decision — and named, not guessed at.

    All four foreign channels are live and selling. Left unnamed they
    would be unknown channels and stop the first live run, which is right
    for a channel nobody has decided about and wrong for one that was
    decided in August.
    """
    _key(monkeypatch)
    config, _ = load_config(REAL_CONFIG, "ithrive")
    client = VeeqoLiveClient(
        channel_keys=channel_keys_from(config),
        excluded_channels=frozenset(config.entity.excluded_veeqo_channels),
        credentials_prefix=PREFIX,
        transport=_transport(_orders("Amazon", *EXCLUDED)),
    )
    velocity = client.read_velocity(90)
    assert velocity["5G-AP1S-TUE4"].units_sold == 10
    assert velocity["5G-AP1S-TUE4"].by_channel == {"amazon_fbm": 10}


def test_an_unnamed_foreign_channel_stops_the_run(monkeypatch: pytest.MonkeyPatch) -> None:
    """A channel nobody has decided about is unknown, and unknown stops.

    Excluding demand is a decision somebody makes in writing. Canada and
    Mexico have that decision; a fifth marketplace opening tomorrow does
    not, and must not be dropped by inheriting theirs.
    """
    _key(monkeypatch)
    client = VeeqoLiveClient(
        channel_keys={"Amazon": "amazon_fbm"},
        credentials_prefix=PREFIX,
        transport=_transport(_orders("Amazon", "Amazon Japan")),
    )
    with pytest.raises(ReadFailure) as failure:
        client.read_velocity(90)
    assert "Amazon Japan" in str(failure.value)


def test_two_channels_cannot_claim_the_same_veeqo_name() -> None:
    config, findings = load_config(REAL_CONFIG, "ithrive")
    channels = tuple(
        dataclasses.replace(channel, veeqo_channel="Amazon")
        if channel.key == "shopify"
        else channel
        for channel in config.entity.channels
    )
    result = validate(
        dataclasses.replace(config, entity=dataclasses.replace(config.entity, channels=channels)),
        findings,
    )
    assert any("both claim the Veeqo channel" in f.render() for f in result.errors)


def test_a_channel_with_history_may_not_be_marked_not_connected() -> None:
    config, findings = load_config(REAL_CONFIG, "ithrive")
    channels = tuple(
        dataclasses.replace(channel, veeqo_channel=NOT_CONNECTED)
        if channel.key == "shopify"
        else channel
        for channel in config.entity.channels
    )
    result = validate(
        dataclasses.replace(config, entity=dataclasses.replace(config.entity, channels=channels)),
        findings,
    )
    assert any("has_history: true" in f.render() for f in result.errors)


def test_a_channel_cannot_be_counted_and_excluded_at_once() -> None:
    config, findings = load_config(REAL_CONFIG, "ithrive")
    entity = dataclasses.replace(config.entity, excluded_veeqo_channels=("Amazon",))
    result = validate(dataclasses.replace(config, entity=entity), findings)
    assert any("cannot be both counted and not counted" in f.render() for f in result.errors)


def test_the_report_names_demand_left_out_on_purpose(
    app_conn: psycopg.Connection[tuple[object, ...]],
    entity_id: str,
    tmp_path: Path,
) -> None:
    """Excluded sales are printed, because demand left out is still demand.

    A decision that lives only in a config file is a decision nobody
    reading the report can see.
    """
    config, _ = load_config(REAL_CONFIG, "ithrive")
    with entity_session(app_conn, entity_id) as conn:
        summary = run_replenishment(
            conn=conn,
            config=config,
            fixtures=Path(__file__).parent / "fixtures" / "ithrive-sample",
            output_dir=tmp_path,
            now=AFTER_THE_COUNT,
        )
    assert summary.error is None, summary.error
    assert summary.report_path is not None
    body = Path(summary.report_path).read_text(encoding="utf-8")
    assert (
        "Not counted towards demand, by decision: Amazon Canada, Amazon Canada FBA, "
        "Amazon Mexico, Amazon Mexico FBA" in body
    )
    assert "reorder demand is US only" in body


def test_the_channel_model_knows_what_not_connected_means() -> None:
    absent = Channel(name="x", key="x", fulfillment="wfs", has_history=False)
    assert dataclasses.replace(absent, veeqo_channel=NOT_CONNECTED).in_veeqo is False
    assert dataclasses.replace(absent, veeqo_channel="Shopify").in_veeqo is True
