# Family Link with Bonus Time

A small, unofficial Python client for Google Family Link. It can list supervised
children and their devices, inspect usage and available time, lock or unlock
devices, and grant or cancel bonus time.

This uses undocumented Google endpoints and may stop working if Google changes
them. A successful HTTP response does not always mean that Google applied an
action; bonus time is verified by default.

## Requirements

- Python 3.8 or newer
- `httpx`
- An active Google login to Family Link in a browser

Install the dependency:

```bash
python -m pip install httpx
```

## Authentication

The client needs the cookies from a logged-in Google account that can manage
the family:

1. Open [Family Link](https://familylink.google.com) and sign in.
2. Open the browser's developer tools and inspect a request to Family Link.
3. Copy the request's `Cookie` header value.
4. Provide the cookie as a single string in `config.COOKIES`.

Treat this cookie like a password: do not paste it into source code, commit it,
or share it. Keep it in a secure local environment-variable manager. If it is
exposed, sign out of Google and refresh the session.

If multiple Google accounts are signed into the browser, set
`config.AUTH_USER` to the slot used by Family Link (for example, `0` or
`1`). It defaults to `0`.

## Basic usage

The cookie is parsed into the dictionary expected by the client. This example
lists supervised children and devices, then grants five minutes to a named
child's named device:

```python
import os

from familylink_w_bonustime import FamilyLinkWithBonusTime
from familylink_w_bonustime.helper import get_cookies_dict


with FamilyLinkWithBonusTime(cookies=
get_cookies_dict(config.COOKIES), auth_user=auth_user) as family_link:
    children = family_link.get_children()
    print("Supervised children:", children)

    child_name = "Bobby D. Tables"

    childs_dev_names = family_link.get_device_names(child_name)
    print(f"Devices for {child_name}:", childs_dev_names)

    is_success = family_link.add_bonus_time(
        child_name,
        childs_dev_names[0],
        5, # minutes
        True,
    )
    print("Bonus applied:", is_success)
```


## Available operations

```python
family_link.get_children()
family_link.get_device_names("Bobby D. Tables")
family_link.add_bonus_time("Bobby D. Tables", "Bobby's Chromebook", 5, True)
```

`get_children()` returns dictionaries with each supervised child's `id` and
`name`. `get_device_names(child_name)` returns the device display names for
that child.

`add_bonus_time(child_name, device_name, minutes, verify)` returns `True` when
the bonus is confirmed (or when `verify` is `False` and Google accepts the
request), and `False` if verification indicates it was not applied. `minutes`
must be a positive integer. Verification may wait several seconds while the
client checks Family Link's applied state.

Other device operations are:

```python
family_link.lock_device("Bobby Chromebook")
family_link.unlock_device("Bobby Chromebook")
family_link.is_locked("Bobby Chromebook")
family_link.cancel_bonus_time("Bobby Chromebook")
family_link.get_available_time("Bobby Chromebook")
family_link.get_time_spent_today("Bobby Chromebook")
```

These operations use the account configured by the optional `account_id`
constructor argument; if it is omitted, the client defaults to the first
supervised child. To ensure these operations target a particular child, pass
that child's ID when constructing the client:

```python
with FamilyLinkWithBonusTime(
    account_id="CHILD_GOOGLE_USER_ID",
    cookies=cookies,
    auth_user=auth_user,
) as family_link:
    family_link.lock_device("Bobby Chromebook")
```

Keep the account ID private as well. Unlike `add_bonus_time`, the other
device-operation methods currently take a device name but not a child name.

## Errors and cleanup

The client raises `FamilyLinkAuthError` for missing or rejected cookies,
`DeviceNotFoundError` when a device name cannot be resolved, and
`FamilyLinkError` for other request failures. Using it as a context manager
(`with FamilyLinkWithBonusTime(...) as family_link`) closes the HTTP client
when the block exits.
