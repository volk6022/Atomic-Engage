"""Unit tests for the numeric datacenter-ASN block-list (FR-310, SC-002 helper)."""
from app.services.geo_match import DATACENTER_ASN_NUMBERS, is_datacenter_asn


def test_known_datacenter_asns_blocked():
    assert is_datacenter_asn(16509) is True   # AWS
    assert is_datacenter_asn(24940) is True    # Hetzner
    assert is_datacenter_asn("14061") is True  # DigitalOcean as string


def test_residential_or_unknown_asn_allowed():
    assert is_datacenter_asn(None) is False
    assert is_datacenter_asn(0) is False
    assert is_datacenter_asn("not-a-number") is False
    # A residential ASN (e.g. a Russian ISP) is not on the block-list.
    assert is_datacenter_asn(12389) is False


def test_blocklist_nonempty_and_ints():
    assert len(DATACENTER_ASN_NUMBERS) >= 10
    assert all(isinstance(a, int) for a in DATACENTER_ASN_NUMBERS)
