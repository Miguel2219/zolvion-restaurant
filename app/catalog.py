"""The restaurant config, loaded once. The single source of truth for prices."""

import json
import os
import unicodedata
from pathlib import Path

CONFIG_PATH = Path(
    os.environ.get(
        "RESTAURANT_CONFIG",
        Path(__file__).resolve().parent.parent / "config" / "restaurant.json",
    )
)


def _fold(text: str) -> str:
    """lowercase, strip accents — so 'Cañaveral' matches 'canaveral'."""
    norm = unicodedata.normalize("NFD", text.lower())
    return "".join(c for c in norm if unicodedata.category(c) != "Mn").strip()


MASK_CHARS = "*•#xX"


class Catalog:
    def __init__(self, data: dict):
        self.raw = data
        self.business = data["business"]
        self.hours = data["hours"]
        self.fulfilment = data["fulfilment"]
        self.payments = data["payments"]
        self._items = {}
        self._item_category = {}
        for category in data["menu"]["categories"]:
            for item in category["items"]:
                self._items[item["id"]] = item
                self._item_category[item["id"]] = category["id"]
        self._modifiers = {m["id"]: m for m in data["menu"].get("modifiers", [])}

    # ---- items -------------------------------------------------------

    def item(self, item_id: str):
        return self._items.get(item_id)

    def price_of(self, item_id: str) -> int:
        item = self._items.get(item_id)
        if item is None:
            raise KeyError(f"unknown item: {item_id}")
        return int(item["price"])

    def is_available(self, item_id: str) -> bool:
        item = self._items.get(item_id)
        return bool(item and item.get("available", True))

    def modifier(self, modifier_id: str):
        return self._modifiers.get(modifier_id)

    def modifier_price(self, modifier_id: str) -> int:
        modifier = self._modifiers.get(modifier_id)
        if modifier is None:
            raise KeyError(f"unknown modifier: {modifier_id}")
        return int(modifier["price"])

    def modifier_allowed(self, modifier_id: str, item_id: str) -> bool:
        modifier = self._modifiers.get(modifier_id)
        if modifier is None:
            return False
        applies = modifier.get("applies_to")
        if not applies:
            return True
        return self._item_category.get(item_id) in applies

    # ---- delivery ----------------------------------------------------

    def delivery_fee_for(self, zone_name: str | None) -> tuple[int, str | None]:
        """Returns (fee, matched_zone_name). Unknown zone -> default fee."""
        zones = self.fulfilment.get("delivery_zones", [])
        if zone_name:
            target = _fold(zone_name)
            for zone in zones:
                if _fold(zone["name"]) == target:
                    return int(zone["fee"]), zone["name"]
            for zone in zones:
                if target in _fold(zone["name"]) or _fold(zone["name"]) in target:
                    return int(zone["fee"]), zone["name"]
        return int(self.fulfilment.get("default_delivery_fee", 0)), None

    # ---- payments ----------------------------------------------------

    def accounts(self):
        return self.payments.get("accounts", [])

    def timezone(self):
        """The restaurant's own zone. Without tzdata (common on Windows) zoneinfo
        raises, so fall back to a fixed Colombian offset rather than silently UTC."""
        try:
            from zoneinfo import ZoneInfo
            return ZoneInfo(self.business.get("timezone", "America/Bogota"))
        except Exception:  # noqa: BLE001
            from datetime import timedelta, timezone
            return timezone(timedelta(hours=-5))

    def owns_account(self, number: str | None) -> bool:
        """Does this account number belong to the restaurant?

        Exact on digits. A masked value (300****7389) is matched on the visible
        prefix AND the last four, never on the last four alone - otherwise any
        stranger's account sharing the final digits is accepted."""
        if not number:
            return False
        text = str(number)
        mask_at = [i for i, ch in enumerate(text) if ch in MASK_CHARS]
        for account in self.accounts():
            mine = "".join(ch for ch in str(account["number"]) if ch.isdigit())
            if not mask_at:
                target = "".join(ch for ch in text if ch.isdigit())
                if target and mine == target:
                    return True
                continue
            prefix = "".join(ch for ch in text[:mask_at[0]] if ch.isdigit())
            suffix = "".join(ch for ch in text[mask_at[-1] + 1:] if ch.isdigit())
            if prefix and suffix and mine.startswith(prefix) and mine.endswith(suffix):
                return True
        return False

    # ---- prompt text -------------------------------------------------

    def menu_for_prompt(self) -> str:
        lines = []
        for category in self.raw["menu"]["categories"]:
            lines.append(f"\n## {category['name']}")
            for item in category["items"]:
                mark = "" if item.get("available", True) else "  [AGOTADO HOY]"
                lines.append(
                    f"- id={item['id']} | {item['name']} | {item['price']} COP"
                    f" | {item.get('description', '')}{mark}"
                )
        if self._modifiers:
            lines.append("\n## Adiciones")
            for modifier in self._modifiers.values():
                lines.append(
                    f"- id={modifier['id']} | {modifier['name']} | {modifier['price']} COP"
                )
        return "\n".join(lines)

    def accounts_for_prompt(self) -> str:
        return "\n".join(
            f"- {a['provider']}: {a['number']} ({a['holder']})" for a in self.accounts()
        )


def load(path: Path | str | None = None) -> Catalog:
    target = Path(path) if path else CONFIG_PATH
    with open(target, encoding="utf-8") as handle:
        return Catalog(json.load(handle))


CATALOG = load()


def reload() -> Catalog:
    global CATALOG
    CATALOG = load()
    return CATALOG
