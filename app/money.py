"""Money is always an int of Colombian pesos. Never a float, never from the model."""

import re


class MoneyError(ValueError):
    pass


# What staff type: plain digits, or groups of exactly three digits with ONE kind of
# separator ("26.000", "1.250.000", "47,000"). Anything else is refused, never guessed:
# "26000.5" must not become 260005.
_PLAIN = re.compile(r"[0-9]+")
_GROUPED = re.compile(r"[0-9]{1,3}([.,])[0-9]{3}(\1[0-9]{3})*")


def pesos(value) -> int:
    """Coerce to int pesos, refusing anything that is not a whole number."""
    if isinstance(value, bool):
        raise MoneyError("El monto debe ser un número de pesos.")
    if isinstance(value, int):
        return value
    if isinstance(value, float):
        raise MoneyError("El monto debe ser en pesos enteros, sin decimales.")
    if isinstance(value, str):
        cleaned = re.sub(r"[$\s ]", "", value)
        if _PLAIN.fullmatch(cleaned):
            return int(cleaned)
        if _GROUPED.fullmatch(cleaned):
            return int(re.sub(r"[.,]", "", cleaned))
        raise MoneyError(
            f"El monto «{value}» no es válido: escriba solo pesos enteros, sin decimales "
            "(por ejemplo 26000 o 26.000)."
        )
    raise MoneyError("El monto debe ser un número de pesos.")


def total_of(line_amounts, fee: int = 0) -> int:
    """The only place a total is ever produced."""
    running = 0
    for amount in line_amounts:
        if not isinstance(amount, int) or isinstance(amount, bool):
            raise MoneyError("Los valores de las líneas deben ser pesos enteros.")
        running += amount
    if not isinstance(fee, int) or isinstance(fee, bool):
        raise MoneyError("El domicilio debe ser en pesos enteros.")
    return running + fee


def fmt(amount: int) -> str:
    """$32.000 — Colombian style."""
    if not isinstance(amount, int) or isinstance(amount, bool):
        raise MoneyError("Solo se pueden mostrar pesos enteros.")
    return "$" + f"{amount:,}".replace(",", ".")
