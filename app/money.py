"""Money is always an int of Colombian pesos. Never a float, never from the model."""


class MoneyError(ValueError):
    pass


def pesos(value) -> int:
    """Coerce to int pesos, refusing anything that is not a whole number."""
    if isinstance(value, bool):
        raise MoneyError("bool is not money")
    if isinstance(value, int):
        return value
    if isinstance(value, float):
        raise MoneyError("floats are not allowed as money")
    if isinstance(value, str):
        cleaned = value.replace(".", "").replace(",", "").replace("$", "").strip()
        if not cleaned.isdigit():
            raise MoneyError(f"not a money value: {value!r}")
        return int(cleaned)
    raise MoneyError(f"not a money value: {value!r}")


def total_of(line_amounts, fee: int = 0) -> int:
    """The only place a total is ever produced."""
    running = 0
    for amount in line_amounts:
        if not isinstance(amount, int) or isinstance(amount, bool):
            raise MoneyError("line amounts must be int pesos")
        running += amount
    if not isinstance(fee, int) or isinstance(fee, bool):
        raise MoneyError("fee must be int pesos")
    return running + fee


def fmt(amount: int) -> str:
    """$32.000 — Colombian style."""
    if not isinstance(amount, int) or isinstance(amount, bool):
        raise MoneyError("can only format int pesos")
    return "$" + f"{amount:,}".replace(",", ".")
