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
    tags: str = ""

    @property
    def bmc_port(self) -> int:
        return 443

    @property
    def ssh_port(self) -> int:
        return 22

    # --- Lock URI helpers ---

    @property
    def ssh_type(self) -> str:
        """Derive SSH sub-type from device_group. L1/L2 -> VRP, else Linux."""
        group = (self.device_group or "").upper().strip()
        if group in ("L1", "L2"):
            return "SSH_VRP"
        return "SSH_LINUX"

    @property
    def lock_uri_bmc(self) -> str:
        """bmc://{oob_ip} — raises if oob_ip is empty."""
        ip = (self.bmc_ip or "").strip()
        if not ip:
            raise ValueError(
                f"Device {self.device_name}: bmc_ip is empty, "
                f"cannot derive bmc:// lock_uri"
            )
        return f"bmc://{ip}"

    @property
    def lock_uri_ssh(self) -> str:
        """ssh:// or ssh-vrp:// or ssh-linux://{inband_ip} — raises if inband_ip is empty."""
        ip = (self.inband_ip or "").strip()
        if not ip:
            raise ValueError(
                f"Device {self.device_name}: inband_ip is empty, "
                f"cannot derive ssh:// lock_uri"
            )
        st = self.ssh_type
        if st == "SSH_VRP":
            return f"ssh-vrp://{ip}"
        if st == "SSH_LINUX":
            return f"ssh-linux://{ip}"
        return f"ssh://{ip}"
