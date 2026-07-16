import pytest

from app.services.geo_match import GeoMatchValidator, RiskLevel


@pytest.fixture
def validator():
    return GeoMatchValidator()


def test_geo_match_critical_country_mismatch(validator):
    result = validator.validate(phone_country="RU", proxy_country="US")
    assert result.risk == RiskLevel.CRITICAL


def test_geo_match_high_datacenter_asn(validator):
    result = validator.validate(phone_country="RU", proxy_country="RU", asn_org="AWS")
    assert result.risk == RiskLevel.HIGH


def test_geo_match_critical_residential_foreign(validator):
    result = validator.validate(phone_country="RU", proxy_country="UA")
    assert result.risk == RiskLevel.CRITICAL


def test_geo_match_ok_matching_country(validator):
    result = validator.validate(phone_country="RU", proxy_country="RU")
    assert result.risk == RiskLevel.OK


def test_geo_match_phone_country_extraction_e164(validator):
    country = validator.extract_phone_country("+79031234567")
    assert country == "RU"
