"""FamilyLinkWithBonusTime: a small, synchronous Family Link client.

Combines what two open-source projects worked out about Google's *unofficial*
Family Link endpoints:

* ``familylink`` on PyPI (tducret): browser-cookie auth, members, apps & usage.
* ``noiwid/HAFamilyLink`` (Home Assistant integration): device lock/unlock,
  time bonuses (including the ChromeOS wire format), applied time limits.

Both were ported rather than imported, so this module only needs::

    pip install httpx browser-cookie3

Nothing here is an official API. Google can change it at any time, and it has
been known to answer HTTP 200 to requests that never take effect, so
``add_bonus_time`` reads the bonus back to confirm it was applied.

Check the upstream projects' licenses before redistributing this file.
"""

import hashlib
import json
import logging
import re
import time
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, List, Optional

import httpx

from familylink_bonustime import helper

_LOGGER = logging.getLogger(__name__)

BASE_URL = "https://kidsmanagement-pa.clients6.google.com/kidsmanagement/v1"
ORIGIN = "https://familylink.google.com"
API_KEY = "AIzaSyAQb1gupaJhY3CXQy2xmTwJMcjmot3M2hw"  # public web-app key, not a secret

# timeLimitOverrides action codes (from the HA integration)
_ACTION_LOCK = 1  # plain lock, emergency calls only
_ACTION_LOCK_KEEP_ALLOWED = 7  # lock, but "always allowed" apps stay reachable
_ACTION_UNLOCK = 4  # bypass the active restriction until the next scheduled event

# ChromeOS device ids are ~91 chars, Android ones ~44. Nothing in the API labels
# the platform, so the HA integration uses id length as the discriminator.
_CHROMEOS_ID_MIN_LEN = 60

_UUID_RE = re.compile(
    r"[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}"
)
_SAFE_ID_RE = re.compile(r"[A-Za-z0-9_\-]+")


class FamilyLinkError(Exception):
    """Any Family Link failure."""


class FamilyLinkAuthError(FamilyLinkError):
    """Cookies missing, expired or rejected. Log in again in the browser."""


class DeviceNotFoundError(FamilyLinkError):
    """No (or more than one) device matches the given name."""


def _dig(obj: Any, *path: int) -> Any:
    """Safe nested list indexing: returns None instead of raising."""
    for i in path:
        if not isinstance(obj, list) or i >= len(obj):
            return None
        obj = obj[i]
    return obj


def _digits(value: Any) -> Optional[int]:
    return int(value) if isinstance(value, str) and value.isdigit() else None


class FamilyLinkWithBonusTime:
    # Seconds to wait before each read-back that confirms a bonus was applied.
    VERIFY_DELAYS = (3, 5)

    def __init__(
            self,
            account_id: Optional[str] = None,
            *,
            browser: str = "firefox",
            cookie_file_path: Optional[Path] = None,
            cookies: Optional[Dict[str, str]] = None,
            auth_user: int = 0,
            lock_keeps_allowed_apps: bool = True,
            timeout: float = 30.0,
            transport: Optional[httpx.BaseTransport] = None,
    ):
        """
        Args:
            account_id: The child's Google user id. Defaults to the first
                supervised member of the family (see ``get_children``).
            browser: Browser to read Google cookies from (any
                ``browser_cookie3`` function name: firefox, chrome, edge...).
                You must be logged in to https://familylink.google.com there.
            cookie_file_path: Optional explicit cookie DB path for that browser.
            cookies: Alternative to ``browser``: a plain ``{name: value}`` dict
                of google.com cookies. Must contain ``SAPISID``. Useful on a
                headless machine; copy them from a logged-in browser.
            auth_user: Google account slot selected in the browser session.
            lock_keeps_allowed_apps: When locking, keep the "always allowed"
                apps reachable from the lock screen (what the app does by default).
            transport: Custom httpx transport (used by tests).
        """
        self.account_id = account_id
        self.lock_keeps_allowed_apps = lock_keeps_allowed_apps
        if not isinstance(auth_user, int) or auth_user < 0:
            raise ValueError("auth_user must be a non-negative integer")
        self.auth_user = auth_user

        self._cookies = cookies if cookies is not None else self._load_browser_cookies(
            browser, cookie_file_path
        )
        sapisid = (self._cookies.get("SAPISID") or "").strip('"')
        if not sapisid:
            raise FamilyLinkAuthError(
                "No SAPISID cookie found. Log in at https://familylink.google.com "
                "in the chosen browser first."
            )
        self._sapisid = sapisid
        self._cookie_header = "; ".join(f"{k}={v}" for k, v in self._cookies.items())
        self._http = httpx.Client(timeout=timeout, transport=transport)
        self._devices: Optional[Dict[str, str]] = None  # device_id -> display name

    # ------------------------------------------------------------------ auth

    def _auth_header(self) -> str:
        # Google rejects stale timestamps, so this is computed per request.
        ts = int(time.time())
        digest = hashlib.sha1(f"{ts} {self._sapisid} {ORIGIN}".encode()).hexdigest()
        return f"SAPISIDHASH {ts}_{digest}"

    def _request(
            self,
            method: str,
            path: str,
            *,
            params: Optional[list] = None,
            body: Any = None,
            content_type: str = "application/json",
    ) -> Any:
        headers = {
            "User-Agent": (
                "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                "(KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
            ),
            "Origin": ORIGIN,
            "Content-Type": content_type,
            "X-Goog-Api-Key": API_KEY,
            "Authorization": self._auth_header(),
            "Cookie": self._cookie_header,
            "X-Goog-AuthUser": str(self.auth_user),
        }
        content = json.dumps(body) if body is not None else None
        try:
            resp = self._http.request(
                method, BASE_URL + path, params=params, content=content, headers=headers
            )
        except httpx.HTTPError as exc:
            raise FamilyLinkError(f"Network error calling {path}: {exc}") from exc

        if resp.status_code == 401:
            raise FamilyLinkAuthError("Session rejected (401). Log in again in the browser.")
        if resp.status_code >= 400:
            raise FamilyLinkError(f"HTTP {resp.status_code} from {path}: {resp.text[:300]}")
        if not resp.content:
            return None
        try:
            return resp.json()
        except ValueError:
            return resp.text

    # --------------------------------------------------------------- account

    def get_children(self) -> List[Dict[str, str]]:
        """Supervised children in the family as ``[{"id": ..., "name": ...}]``."""
        data = self._request("GET", "/families/mine/members")
        children = []
        for m in (data or {}).get("members", []):
            if (m.get("memberSupervisionInfo") or {}).get("isSupervisedMember"):
                children.append(
                    {"id": m["userId"], "name": (m.get("profile") or {}).get("displayName", "")}
                )
        return children

    def _account(self) -> str:
        if not self.account_id:
            children = self.get_children()
            if not children:
                raise FamilyLinkError("No supervised child found in this family.")
            if len(children) > 1:
                _LOGGER.warning(
                    "Several supervised children found; using %r. Pass account_id to choose.",
                    children[0]["name"],
                )
            self.account_id = children[0]["id"]
        if not _SAFE_ID_RE.fullmatch(self.account_id):
            raise ValueError("account_id contains disallowed characters")
        return self.account_id

    def _account_for_child_name(self, child_name: str) -> str:
        wanted = child_name.strip().casefold()
        if not wanted:
            raise ValueError("child_name must not be empty")
        matches = [
            child for child in self.get_children()
            if child["name"].casefold() == wanted
        ]
        if not matches:
            raise FamilyLinkError(f"No supervised child named {child_name!r} found.")
        if len(matches) > 1:
            raise FamilyLinkError(
                f"Several supervised children are named {child_name!r}; "
                "a unique child name is required."
            )
        account_id = matches[0]["id"]
        if not _SAFE_ID_RE.fullmatch(account_id):
            raise ValueError("account_id contains disallowed characters")
        return account_id

    # --------------------------------------------------------------- devices

    @staticmethod
    def _parse_devices(data: dict) -> Dict[str, str]:
        devices: Dict[str, str] = {}
        for dev in data.get("deviceInfo", []):
            dev_id = dev.get("deviceId")
            info = dev.get("displayInfo") or {}
            if dev_id:
                devices[dev_id] = info.get("friendlyName") or info.get("model") or dev_id
        return devices

    def _fetch_apps_and_usage(self, account_id: Optional[str] = None) -> dict:
        cache_devices = account_id is None
        account_id = account_id or self._account()
        data = self._request(
            "GET",
            f"/people/{account_id}/appsandusage",
            params=[
                ("capabilities", "CAPABILITY_APP_USAGE_SESSION"),
                ("capabilities", "CAPABILITY_SUPERVISION_CAPABILITIES"),
            ],
        ) or {}
        if cache_devices:
            self._devices = self._parse_devices(data)
        return data

    def _resolve_device(self, device_name: str, account_id: Optional[str] = None) -> str:
        """Map a friendly name (case-insensitive) or raw device id to a device id."""
        for fresh in (False, True):
            if account_id is not None:
                devices = self._parse_devices(
                    self._fetch_apps_and_usage(account_id=account_id)
                )
            else:
                if self._devices is None or fresh:
                    self._fetch_apps_and_usage()
                assert self._devices is not None
                devices = self._devices
            wanted = device_name.casefold()
            matches = [
                dev_id
                for dev_id, name in devices.items()
                if dev_id == device_name or name.casefold() == wanted
            ]
            if matches:
                break
        if not matches:
            raise DeviceNotFoundError(
                f"No device named {device_name!r}. Available: {sorted(devices.values())}"
            )
        if len(matches) > 1:
            raise DeviceNotFoundError(
                f"{len(matches)} devices are named {device_name!r}; pass the raw device id instead."
            )
        return matches[0]

    def get_device_names(self, child_name: str) -> List[str]:
        """Names of the devices Family Link knows about for the named child."""
        account_id = self._account_for_child_name(child_name)
        data = self._fetch_apps_and_usage(account_id=account_id)
        return list(self._parse_devices(data).values())

    # -------------------------------------------------------- applied limits

    @staticmethod
    def _parse_applied(data: Any) -> Dict[str, Dict[str, Any]]:
        """Parse /appliedTimeLimits (a positional-array response) per device.

        Index meanings come from the HA integration's reverse engineering:
        ``dd[0]`` is the active override (lock/unlock/bonus), ``dd[20]`` the
        used time in ms, and 6-element rows starting with "CAEQ..."/"CAMQ..."/a
        UUID are the weekly daily-limit rows (day, stateFlag, minutes, ...).
        """
        result: Dict[str, Dict[str, Any]] = {}
        if not isinstance(_dig(data, 1), list):
            return result
        today = datetime.now().isoweekday()  # 1=Mon..7=Sun

        for dd in data[1]:
            if not isinstance(dd, list) or len(dd) < 25:
                continue
            head = dd[0] if isinstance(dd[0], list) else None
            if head and len(head) > 3:
                device_id = head[3]
            else:
                device_id = dd[25] if len(dd) > 25 else None
            if not device_id:
                continue

            info: Dict[str, Any] = {
                "locked": False,
                "lock_override": None,
                "bonus_minutes": 0,
                "bonus_override_id": None,
                "used_minutes": 0,
                "daily_limit_enabled": False,
                "daily_limit_minutes": 0,
                "position19_minutes": None,  # unverified: may be time left while a bonus is active
            }

            code = _dig(head, 2)
            if code in (_ACTION_LOCK, _ACTION_UNLOCK, _ACTION_LOCK_KEEP_ALLOWED):
                info["lock_override"] = code
                info["locked"] = code in (_ACTION_LOCK, _ACTION_LOCK_KEEP_ALLOWED)

            # Bonus override: type 10 (Android, seconds) or type 6 (ChromeOS, ms)
            if code in (6, 10):
                info["bonus_override_id"] = head[0] if head and len(head) > 0 else None
                if code == 10:
                    secs = _digits(_dig(head, 13, 0, 0))
                else:
                    ms = _digits(_dig(head, 10, 0))
                    secs = ms // 1000 if ms is not None else None
                if secs is not None:
                    info["bonus_minutes"] = secs // 60

            used_ms = _digits(dd[20])
            if used_ms is not None:
                info["used_minutes"] = used_ms // 60000
            p19 = _digits(dd[19])
            if p19 is not None:
                info["position19_minutes"] = p19 // 60000

            best_priority = -1
            for idx, item in enumerate(dd):
                if not (isinstance(item, list) and len(item) == 6 and isinstance(item[0], str)):
                    continue
                key = item[0]
                known = key.startswith(("CAEQ", "CAMQ")) or (len(key) == 36 and key.count("-") == 4)
                day, flag, minutes = item[1], item[2], item[3]
                if not (known and day == today
                        and all(isinstance(v, int) for v in (flag, minutes))):
                    continue
                active = idx < 10  # rows further down are config/historical copies
                priority = 2 if active else 1
                if priority >= best_priority:
                    best_priority = priority
                    info["daily_limit_enabled"] = active and flag == 2
                    info["daily_limit_minutes"] = minutes

            # Remaining time, using the HA integration's model: an active bonus
            # REPLACES the normal allowance; otherwise it's daily limit - used.
            remaining: Optional[int] = None
            if info["daily_limit_enabled"] and info["daily_limit_minutes"] > 0:
                if info["bonus_minutes"] > 0:
                    remaining = info["bonus_minutes"]
                else:
                    remaining = max(0, info["daily_limit_minutes"] - info["used_minutes"])
            info["remaining_minutes"] = remaining

            result[device_id] = info
        return result

    def _applied_state(
            self, account_id: Optional[str] = None
    ) -> Dict[str, Dict[str, Any]]:
        account_id = account_id or self._account()
        data = self._request(
            "GET",
            f"/people/{account_id}/appliedTimeLimits",
            params=[("capabilities", "TIME_LIMIT_CLIENT_CAPABILITY_SCHOOLTIME")],
            content_type="application/json+protobuf",
        )
        return self._parse_applied(data)

    # ------------------------------------------------------------ lock/unlock

    def _post_override(self, override: list, account_id: Optional[str] = None) -> Any:
        account = account_id or self._account()
        return self._request(
            "POST",
            f"/people/{account}/timeLimitOverrides:batchCreate",
            body=[None, account, [override], [1]],
            content_type="application/json+protobuf",
        )

    def lock_device(self, device_name: str) -> None:
        """Lock the device now."""
        device_id = self._resolve_device(device_name)
        code = _ACTION_LOCK_KEEP_ALLOWED if self.lock_keeps_allowed_apps else _ACTION_LOCK
        self._post_override([None, None, code, device_id])

    def unlock_device(self, device_name: str) -> None:
        """Unlock the device. This is an override of the active restriction until
        the next scheduled event; it does not edit bedtime or daily-limit rules."""
        device_id = self._resolve_device(device_name)
        self._post_override([None, None, _ACTION_UNLOCK, device_id])

    def is_locked(self, device_name: str) -> bool:
        """True if a manual lock override is currently applied to the device."""
        device_id = self._resolve_device(device_name)
        return bool(self._applied_state().get(device_id, {}).get("locked", False))

    # ------------------------------------------------------------- bonus time

    @staticmethod
    def _bonus_override(shape: str, device_id: str, seconds: int) -> list:
        if shape == "chromeos":  # type 6, milliseconds at index 10
            return [None, None, 6, device_id, None, None, None, None, None, None,
                    [str(seconds * 1000)]]
        # android: type 10, seconds at index 13
        return [None, None, 10, device_id, None, None, None, None, None, None, None, None, None,
                [[str(seconds), 0]]]

    def _bonus_visible(self, device_id: str, account_id: Optional[str] = None) -> bool:
        """Poll the applied state until the bonus shows up (Google acks with 200 regardless)."""
        try:
            for delay in self.VERIFY_DELAYS:
                time.sleep(delay)
                state = self._applied_state(account_id=account_id).get(device_id, {})
                if state.get("bonus_override_id"):
                    return True
            return False
        except FamilyLinkError as exc:
            # Can't tell; don't trigger a fallback (and a second bonus) on unknown state.
            _LOGGER.warning("Could not verify bonus: %s", exc)
            return True

    def _delete_override(
            self, override_id: str, account_id: Optional[str] = None
    ) -> None:
        if not _SAFE_ID_RE.fullmatch(override_id):
            raise ValueError("override id contains disallowed characters")
        self._request(
            "POST",
            f"/people/{account_id or self._account()}/timeLimitOverride/{override_id}?$httpMethod=DELETE",
            content_type="application/json+protobuf",
        )

    def add_bonus_time(
            self, child_name: str, device_name: str, minutes: int, verify: bool
    ) -> bool:
        """Grant a time bonus on the device.

        Returns True if the bonus was confirmed in Family Link's applied state
        (or ``verify=False`` and Google accepted the request), False if Google
        accepted it but it never showed up. Per the HA integration's findings,
        a bonus *replaces* the day's normal allowance rather than adding to it.
        """
        if not isinstance(minutes, int) or minutes <= 0:
            raise ValueError("minutes must be a positive integer")
        account_id = self._account_for_child_name(child_name)
        device_id = self._resolve_device(device_name, account_id=account_id)
        seconds = minutes * 60
        shape = "chromeos" if len(device_id) >= _CHROMEOS_ID_MIN_LEN else "android"

        resp = self._post_override(
            self._bonus_override(shape, device_id, seconds), account_id=account_id
        )
        if not verify:
            return True
        if self._bonus_visible(device_id, account_id=account_id):
            return True

        if shape == "chromeos":
            # The ChromeOS shape came from a single capture. If it was inert,
            # remove it and fall back to the Android shape (device at least unlocks).
            created = _UUID_RE.search(json.dumps(resp))
            if created:
                try:
                    self._delete_override(created.group(0), account_id=account_id)
                except FamilyLinkError as exc:
                    _LOGGER.warning("Could not remove inert override: %s", exc)
            _LOGGER.warning("ChromeOS-shape bonus not applied; retrying with Android shape")
            self._post_override(
                self._bonus_override("android", device_id, seconds), account_id=account_id
            )
            return self._bonus_visible(device_id, account_id=account_id)
        return False

    def cancel_bonus_time(self, device_name: str) -> bool:
        """Cancel the active bonus on the device. False if there isn't one."""
        device_id = self._resolve_device(device_name)
        override_id = self._applied_state().get(device_id, {}).get("bonus_override_id")
        if not override_id:
            return False
        self._delete_override(override_id)
        return True

    # ------------------------------------------------------------- time stats

    def get_available_time(self, device_name: str) -> Optional[int]:
        """Minutes left today, or None if no daily limit applies (unlimited).

        Uses the HA integration's model: an active bonus counts as the time
        left (it replaces the normal allowance); otherwise daily limit - used.
        """
        device_id = self._resolve_device(device_name)
        return self._applied_state().get(device_id, {}).get("remaining_minutes")

    def get_time_spent_today(self, device_name: str) -> int:
        """Whole minutes of screen time on the device today (from app usage sessions)."""
        data = self._fetch_apps_and_usage()  # also refreshes the device cache
        device_id = self._resolve_device(device_name)
        assert self._devices is not None

        app_devices = {
            a["packageName"]: a["deviceIds"]
            for a in data.get("apps", [])
            if a.get("packageName") and a.get("deviceIds")
        }
        now = datetime.now()
        total = 0.0
        for s in data.get("appUsageSessions", []):
            d = s.get("date") or {}
            if (d.get("year"), d.get("month"), d.get("day")) != (now.year, now.month, now.day):
                continue
            session_device = s.get("deviceMudId")
            if not session_device:
                pkg = (s.get("appId") or {}).get("androidAppPackageName")
                ids = app_devices.get(pkg, [])
                if len(ids) == 1:
                    session_device = ids[0]
                elif len(self._devices) == 1:
                    session_device = next(iter(self._devices))
            if session_device != device_id:
                continue
            try:
                total += float(str(s.get("usage", "0s")).rstrip("s"))
            except ValueError:
                continue
        return int(total // 60)

    # ---------------------------------------------------------------- cleanup

    def close(self) -> None:
        self._http.close()

    def __enter__(self) -> "FamilyLinkWithBonusTime":
        return self

    def __exit__(self, *exc: Any) -> None:
        self.close()


if __name__ == "__main__":
    import config

    logging.basicConfig(
        level=logging.INFO
    )
    '''
    Log in at https://familylink.google.com, then copy the Cookie header
    from a request in the browser's developer tools into config.COOKIES.
    The supervised child's account ID is discovered automatically.
    
     If your browser had multiple google accounts logged in, then use this AUTH_USER index to tell which one we want.
       
    '''
    with FamilyLinkWithBonusTime(
            cookies=helper.get_cookies_dict(config.COOKIES),
            auth_user=getattr(config, "AUTH_USER", 0),
    ) as fl:
        children = fl.get_children()
        print(children)
        child = children[3]

        childs_dev_names = fl.get_device_names(child['name'])
        print(f"Devices for {child['name']}:", childs_dev_names)

        # fl.add_bonus_time("Linden Miller", "Linden Chromebook", 5, True)
