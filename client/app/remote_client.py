"""HTTP client for talking to a usbip-web server's REST API."""
from __future__ import annotations

import httpx

from common.procutil import validate_busid, validate_hostname, validate_port_number


class RemoteError(Exception):
    pass


def _base_url(host: str, api_port: int) -> str:
    validate_hostname(host)
    api_port = validate_port_number(api_port)
    return f"http://{host}:{api_port}"


async def fetch_devices(host: str, api_port: int, token: str) -> list[dict]:
    url = f"{_base_url(host, api_port)}/api/devices"
    try:
        async with httpx.AsyncClient(timeout=8) as client:
            resp = await client.get(url, headers={"Authorization": f"Bearer {token}"})
    except httpx.RequestError as e:
        raise RemoteError(f"could not reach server: {e}") from e
    if resp.status_code == 401:
        raise RemoteError("server rejected token (unauthorized)")
    if resp.status_code != 200:
        raise RemoteError(f"server returned HTTP {resp.status_code}")
    return resp.json()["devices"]


async def request_share(host: str, api_port: int, token: str, busid: str) -> None:
    """Ask the server to share a device we can see but that isn't shared
    yet, so our own "Attach" click can be one step instead of requiring the
    admin to separately visit the server's dashboard first."""
    validate_busid(busid)
    url = f"{_base_url(host, api_port)}/api/devices/{busid}/request-share"
    try:
        async with httpx.AsyncClient(timeout=10) as client:
            resp = await client.post(url, headers={"Authorization": f"Bearer {token}"})
    except httpx.RequestError as e:
        raise RemoteError(f"could not reach server: {e}") from e
    if resp.status_code == 401:
        raise RemoteError("server rejected token (unauthorized)")
    if resp.status_code != 200:
        detail = ""
        try:
            detail = resp.json().get("detail", "")
        except Exception:
            pass
        raise RemoteError(f"server returned HTTP {resp.status_code}" + (f": {detail}" if detail else ""))


async def register_wireguard(host: str, api_port: int, token: str, pubkey: str) -> dict:
    """Joins that server's WireGuard tunnel using our own public key - see
    common/wireguard_helper.py's module docstring for the overall design.
    Returns {server_pubkey, endpoint, assigned_ip, server_wg_ip, subnet}."""
    url = f"{_base_url(host, api_port)}/api/wireguard/register"
    try:
        async with httpx.AsyncClient(timeout=10) as client:
            resp = await client.post(
                url, headers={"Authorization": f"Bearer {token}"}, json={"pubkey": pubkey}
            )
    except httpx.RequestError as e:
        raise RemoteError(f"could not reach server: {e}") from e
    if resp.status_code == 401:
        raise RemoteError("server rejected token (unauthorized)")
    if resp.status_code != 200:
        detail = ""
        try:
            detail = resp.json().get("detail", "")
        except Exception:
            pass
        raise RemoteError(f"server returned HTTP {resp.status_code}" + (f": {detail}" if detail else ""))
    return resp.json()


async def fetch_info(host: str, api_port: int) -> dict:
    url = f"{_base_url(host, api_port)}/api/info"
    try:
        async with httpx.AsyncClient(timeout=5) as client:
            resp = await client.get(url)
    except httpx.RequestError as e:
        raise RemoteError(f"could not reach server: {e}") from e
    if resp.status_code != 200:
        raise RemoteError(f"server returned HTTP {resp.status_code}")
    return resp.json()


async def check_health(host: str, api_port: int) -> bool:
    try:
        url = f"{_base_url(host, api_port)}/api/health"
        async with httpx.AsyncClient(timeout=4) as client:
            resp = await client.get(url)
        return resp.status_code == 200
    except Exception:
        return False
