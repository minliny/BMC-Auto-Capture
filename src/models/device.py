"""
Device model — immutable after construction.
"""


from __future__ import annotations
from dataclasses import dataclass, field


@dataclass(frozen=True)
class Device:
    row_index: int
    device_name: str
    device_group: str
    bmc_ip: str
    bmc_username: str
    bmc_password: str
    inband_ip: str = ""
    inband_username: str = ""
    inband_password: str = ""
    enabled: bool = True
    tags: tuple[str, ...] = ()
    device_model: str = ""

    @property
    def bmc_port(self) -> int:
        return 443

    @property
    def ssh_port(self) -> int:
        return 22
