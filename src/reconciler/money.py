"""Currency-safe money.

Reconciliation is mostly the art of not mixing up currencies. Rather than
guarding against that with scattered `if` statements, we make it structurally
impossible: `Money` refuses to combine or compare across currencies, and every
amount is a `Decimal` quantised to that currency's real minor unit.

Currencies live in a registry, so supporting a new market is one line of data --
no engine, adapter or report change. The registry ships with the ISO exponents
that actually bite: COP and JPY have no minor unit, KWD has three.
"""

from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal, ROUND_HALF_UP
from typing import Iterable, Iterator


class CurrencyMismatchError(ValueError):
    """Raised when an operation would silently mix two currencies."""


class UnknownCurrencyError(KeyError):
    """Raised when a currency has not been registered."""


@dataclass(frozen=True)
class Currency:
    """An ISO 4217 currency and how it behaves in arithmetic and printing."""

    code: str
    exponent: int  # number of decimal places (COP/JPY: 0, USD: 2, KWD: 3)
    symbol: str = ""
    name: str = ""

    @property
    def quantum(self) -> Decimal:
        return Decimal(1).scaleb(-self.exponent)

    @property
    def minor_units_per_major(self) -> Decimal:
        return Decimal(10) ** self.exponent


class CurrencyRegistry:
    """The set of currencies this deployment understands.

    Adding a market is `registry.register(Currency("PEN", 2, "S/"))` -- there is
    deliberately no hard-coded currency list anywhere else in the codebase.
    """

    def __init__(self, currencies: Iterable[Currency] = ()) -> None:
        self._by_code: dict[str, Currency] = {}
        for currency in currencies:
            self.register(currency)

    def register(self, currency: Currency) -> Currency:
        self._by_code[currency.code.upper()] = currency
        return currency

    def get(self, code: str) -> Currency:
        try:
            return self._by_code[code.strip().upper()]
        except KeyError:
            raise UnknownCurrencyError(
                f"currency {code!r} is not registered; "
                f"known: {', '.join(sorted(self._by_code))}"
            ) from None

    def __contains__(self, code: object) -> bool:
        return isinstance(code, str) and code.strip().upper() in self._by_code

    def __iter__(self) -> Iterator[Currency]:
        return iter(sorted(self._by_code.values(), key=lambda c: c.code))

    def __len__(self) -> int:
        return len(self._by_code)


#: Default registry. CafeTerra trades in BRL/MXN/COP and settles some
#: cross-border volume in USD; the rest are here to show the model generalises
#: past two-decimal assumptions.
REGISTRY = CurrencyRegistry(
    [
        Currency("BRL", 2, "R$", "Brazilian real"),
        Currency("MXN", 2, "MX$", "Mexican peso"),
        Currency("COP", 0, "COL$", "Colombian peso"),
        Currency("USD", 2, "US$", "US dollar"),
        Currency("EUR", 2, "€", "Euro"),
        Currency("CLP", 0, "CLP$", "Chilean peso"),
        Currency("JPY", 0, "¥", "Japanese yen"),
        Currency("KWD", 3, "KD", "Kuwaiti dinar"),
    ]
)


@dataclass(frozen=True)
class Money:
    """An amount in a single currency, quantised to that currency's minor unit."""

    amount: Decimal
    currency: str

    def __post_init__(self) -> None:
        spec = REGISTRY.get(self.currency)
        object.__setattr__(self, "currency", spec.code)
        object.__setattr__(
            self,
            "amount",
            Decimal(self.amount).quantize(spec.quantum, rounding=ROUND_HALF_UP),
        )

    # -- construction ----------------------------------------------------

    @classmethod
    def zero(cls, currency: str) -> "Money":
        return cls(Decimal(0), currency)

    @classmethod
    def from_minor(cls, units: int | str | Decimal, currency: str) -> "Money":
        """Build from minor units (cents/centavos), as several APIs report."""
        spec = REGISTRY.get(currency)
        return cls(Decimal(str(units)) / spec.minor_units_per_major, spec.code)

    @classmethod
    def parse(cls, raw: object, currency: str) -> "Money":
        """Build from whatever a processor put in its file.

        Real settlement reports contain thousands separators, currency symbols,
        parenthesised negatives and both en-US and pt-BR decimal conventions.
        Normalising here keeps every adapter free of string-cleaning code.
        """
        if isinstance(raw, Money):
            return raw
        if isinstance(raw, str):
            raw = _normalise_decimal_string(raw, REGISTRY.get(currency).exponent)
        return cls(Decimal(str(raw)), currency)

    # -- arithmetic ------------------------------------------------------

    def _same(self, other: "Money") -> "Money":
        if self.currency != other.currency:
            raise CurrencyMismatchError(
                f"cannot combine {self.currency} with {other.currency}"
            )
        return other

    def __add__(self, other: "Money") -> "Money":
        return Money(self.amount + self._same(other).amount, self.currency)

    def __sub__(self, other: "Money") -> "Money":
        return Money(self.amount - self._same(other).amount, self.currency)

    def __neg__(self) -> "Money":
        return Money(-self.amount, self.currency)

    def __abs__(self) -> "Money":
        return Money(abs(self.amount), self.currency)

    def __lt__(self, other: "Money") -> bool:
        return self.amount < self._same(other).amount

    def __le__(self, other: "Money") -> bool:
        return self.amount <= self._same(other).amount

    def __gt__(self, other: "Money") -> bool:
        return self.amount > self._same(other).amount

    def __ge__(self, other: "Money") -> bool:
        return self.amount >= self._same(other).amount

    def scaled(self, factor: Decimal | str | int) -> "Money":
        """Multiply by a scalar, e.g. a percentage fee rate."""
        return Money(self.amount * Decimal(str(factor)), self.currency)

    @property
    def is_zero(self) -> bool:
        return self.amount == 0

    @property
    def spec(self) -> Currency:
        return REGISTRY.get(self.currency)

    # -- presentation ----------------------------------------------------

    def __str__(self) -> str:
        spec = self.spec
        return f"{spec.symbol}{self.amount:,.{spec.exponent}f} {spec.code}"

    def to_json(self) -> dict:
        return {"amount": str(self.amount), "currency": self.currency}


def _normalise_decimal_string(raw: str, exponent: int) -> str:
    cleaned = raw.strip().replace(" ", "").replace(" ", "")
    negative = cleaned.startswith("(") and cleaned.endswith(")")
    if negative:
        cleaned = cleaned[1:-1]
    for token in ("R$", "MX$", "COL$", "CLP$", "US$", "$", "€", "¥", "KD"):
        cleaned = cleaned.replace(token, "")
    if exponent == 0:
        # No minor unit, so every separator can only be grouping thousands.
        cleaned = cleaned.replace(".", "").replace(",", "")
    elif "," in cleaned and "." in cleaned:
        # Whichever separator comes last is the decimal point.
        decimal_sep = "," if cleaned.rfind(",") > cleaned.rfind(".") else "."
        thousands_sep = "." if decimal_sep == "," else ","
        cleaned = cleaned.replace(thousands_sep, "").replace(decimal_sep, ".")
    elif cleaned.count(",") > 1 or cleaned.count(".") > 1:
        # A repeated separator groups thousands; it cannot be a decimal point.
        cleaned = cleaned.replace(",", "").replace(".", "")
    elif "," in cleaned:
        cleaned = cleaned.replace(",", ".")
    cleaned = cleaned or "0"
    return f"-{cleaned}" if negative and not cleaned.startswith("-") else cleaned


def total(amounts: Iterable[Money], currency: str) -> Money:
    """Sum a homogeneous iterable of Money, returning zero when it is empty."""
    result = Money.zero(currency)
    for amount in amounts:
        result = result + amount
    return result
