from dataclasses import dataclass
from enum import StrEnum
from typing import Optional
import geoip2.database
import phonenumbers


class RiskLevel(StrEnum):
    CRITICAL = "CRITICAL"
    HIGH = "HIGH"
    WARNING = "WARNING"
    OK = "OK"


@dataclass
class GeoMatchResult:
    risk: RiskLevel
    proxy_country: str
    asn_org: Optional[str] = None
    tz_offset_seconds: int = 0


DATACENTER_ASNS = {
    "AWS",
    "GCP",
    "Azure",
    "DigitalOcean",
    "Hetzner",
    "OVH",
    "Linode",
    "Vultr",
    "ConoHa",
    "Alibaba",
    "Tencent",
    "Oracle",
}


# Numeric ASN block-list (feature 003, FR-310). The Proxy model stores the ASN as an
# integer, so enforcement at task-prepare time needs numbers, not org strings. These
# are well-known datacenter/cloud ASNs (AWS, GCP, Azure, Hetzner, OVH, DigitalOcean,
# Linode, Vultr, Alibaba, Tencent, Oracle). Account-facing actions on these are
# rejected; this is the runtime gate the audit found "declared but unenforced".
DATACENTER_ASN_NUMBERS = {
    16509, 14618, 8987,          # AWS
    15169, 19527, 396982,        # Google / GCP
    8075, 8068,                  # Microsoft / Azure
    24940,                       # Hetzner
    16276,                       # OVH
    14061,                       # DigitalOcean
    63949,                       # Linode/Akamai
    20473,                       # Vultr / Choopa
    45102, 37963,                # Alibaba
    132203,                      # Tencent
    31898,                       # Oracle Cloud
}


def is_datacenter_asn(asn) -> bool:
    """True if ``asn`` (int or numeric str) is a known datacenter/cloud ASN."""
    if asn is None:
        return False
    try:
        return int(asn) in DATACENTER_ASN_NUMBERS
    except (TypeError, ValueError):
        return False


class GeoMatchValidator:
    def __init__(self, city_db_path: str = "", asn_db_path: str = ""):
        self.city_reader = None
        self.asn_reader = None

        if city_db_path:
            try:
                self.city_reader = geoip2.database.Reader(city_db_path)
            except Exception:
                pass

        if asn_db_path:
            try:
                self.asn_reader = geoip2.database.Reader(asn_db_path)
            except Exception:
                pass

    def validate(
        self,
        phone_country: str,
        proxy_url: str = "",
        proxy_country: str = "",
        asn_org: str = "",
    ) -> GeoMatchResult:
        if asn_org and asn_org in DATACENTER_ASNS:
            return GeoMatchResult(
                risk=RiskLevel.HIGH, proxy_country=proxy_country, asn_org=asn_org
            )

        if proxy_country and phone_country != proxy_country:
            return GeoMatchResult(risk=RiskLevel.CRITICAL, proxy_country=proxy_country)

        return GeoMatchResult(risk=RiskLevel.OK, proxy_country=proxy_country)

    def extract_phone_country(self, phone_e164: str) -> Optional[str]:
        try:
            parsed = phonenumbers.parse(phone_e164)
            return phonenumbers.region_code_for_number(parsed)
        except Exception:
            return None

    def get_proxy_info(
        self, proxy_host: str
    ) -> tuple[Optional[str], Optional[int], int]:
        country = "XX"
        asn_org = None
        tz_offset = 0

        if self.city_reader:
            try:
                response = self.city_reader.country(proxy_host)
                country = response.country.iso_code
                tz_offset = response.location.time_zone_offset
            except Exception:
                pass

        if self.asn_reader:
            try:
                response = self.asn_reader.asn(proxy_host)
                asn_org = response.autonomous_system_organization
            except Exception:
                pass

        return country, asn_org, tz_offset
